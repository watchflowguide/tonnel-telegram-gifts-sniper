"""CLI: python -m tonnel_autobuy {login,run,once,mock,floors,doctor}."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

from .config import ConfigError, Settings, get_settings, mask
from . import updater as _updater
from .engine import CycleReport, Engine
from .models import plural
from .notifier import Notifier
from .rules import RuleParseError, load_rules_file
from .storage import Storage

log = logging.getLogger("tonnel_autobuy")

PKG_DIR = Path(__file__).resolve().parent
FIXTURES = PKG_DIR.parent / "tests" / "fixtures"
MOCK_ADMIN_ID = 1
SNAPSHOT_NAME = "floors_tonnel.json"
RUN_LOCK_NAME = "run.lock"


class RunLock:
    """DATA_DIR/run.lock, held by `run` for its whole life (an OS file lock, released even on a crash).

    One process at a time spends from the database (the spend lock is per process) and uses the Telegram
    session file (two Telethon clients lock it). `once` refuses while the service holds it, and CLI `doctor`
    then checks without opening the session. Without fcntl (Windows) there is no lock and nothing is refused."""

    def __init__(self, settings: Settings) -> None:
        self.path = Path(settings.DATA_DIR) / RUN_LOCK_NAME
        self._fh = None

    def acquire(self) -> bool:
        """True when this process now holds the lock, False when another process does."""
        try:
            import fcntl
        except ImportError:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()  # closing the file drops the lock
            self._fh = None


def service_running(settings: Settings) -> bool:
    """True while a `run` process holds DATA_DIR/run.lock."""
    lock = RunLock(settings)
    if lock.acquire():
        lock.release()
        return False
    return True


def without_session(settings: Settings) -> Settings:
    """Settings that can never open the Telegram session (the running service holds it). TONNEL_INIT_DATA,
    if set, still works because it needs no session."""
    return settings.model_copy(update={"TG_API_ID": 0, "TG_API_HASH": ""})


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.INFO)


async def open_storage(settings: Settings, path: str | None = None) -> Storage:
    settings.ensure_data_dir()
    storage = await Storage(path or settings.DB_PATH).open()
    await storage.prune(time.time(), obs_days=settings.OBS_TTL_DAYS, snap_days=settings.SNAPSHOT_TTL_DAYS,
                        decision_days=settings.DECISIONS_TTL_DAYS)
    return storage


async def load_rules(settings: Settings, storage: Storage) -> None:
    try:
        rules = load_rules_file(settings.RULES_FILE)
    except (RuleParseError, ValueError) as exc:
        raise ConfigError(f"RULES_FILE {settings.RULES_FILE}: {exc}") from exc
    if rules:
        # New file rules start in shadow (practice-only even when LIVE) like /addrule and the wizard.
        await storage.replace_rules_from_source("file", rules,
                                                new_state="shadow" if settings.SHADOW_NEW_RULES else "live")
        log.info("rules.json: %s loaded (ids kept across restarts)", plural(len(rules), "rule"))


def print_report(n: int, report: CycleReport, fee_pct: float, max_rejects: int = 10) -> None:
    print(f"--- cycle {n}: {report.summary()}", flush=True)
    for d in report.matched:
        disc = f"{d.result.discount_pct:.1f}%" if d.result.discount_pct is not None else "n/a"
        floor = f"{d.floor:.2f}" if d.floor is not None else "n/a"
        print(f"  MATCH  {d.listing.title()} {d.listing.price_ton:g} TON "
              f"(cost {d.listing.cost_ton(fee_pct):.2f}) floor={floor} discount={disc} "
              f"rule #{d.rule.id}")
    for line in report.rejections[:max_rejects]:
        print(f"  {line}")
    if len(report.rejections) > max_rejects:
        print(f"  ... and {len(report.rejections) - max_rejects} more rejected")
    if report.reasons:
        print(f"  skip reasons: {report.top_reasons(10)}")


def telegram_session(settings: Settings):
    if not settings.has_tg_credentials:
        return None
    from .tg_auth import TelegramSession

    return TelegramSession(settings)


def _make_real_client(settings: Settings, session=None):
    from .client import TonnelClient
    from .tg_auth import InitDataProvider

    auth = InitDataProvider(settings, session)
    return TonnelClient(settings, auth), auth


async def _prepare_auth(settings: Settings, auth) -> None:
    if not settings.DRY_RUN and not auth.configured:
        raise ConfigError("DRY_RUN=false needs Tonnel auth: set TG_API_ID and TG_API_HASH "
                          "(then run `python -m tonnel_autobuy login`) or TONNEL_INIT_DATA")
    if auth.configured:
        await auth.get()  # fail fast on a missing/unauthorized session
    else:
        log.warning("No Tonnel auth (TG_API_ID/TG_API_HASH or TONNEL_INIT_DATA): scanning the "
                    "public listings only; API floors and /balance are unavailable")


def _make_notifier(settings: Settings, use_bot: bool = True):
    bot = None
    if use_bot and settings.BOT_TOKEN:
        from .bot import create_bot

        if not settings.admin_ids:
            raise ConfigError("ADMIN_IDS is empty in .env: set your Telegram user id(s)")
        bot = create_bot(settings)
    elif use_bot:
        log.warning("BOT_TOKEN is empty: notifications go to stdout, admin bot disabled")
    return Notifier(bot, settings.admin_ids if bot else []), bot


def build_peers(settings: Settings, session=None, clock=time.time):
    from .fx import TonRate
    from .peers import Peers, PortalsPublic, TelethonTgValue, TgValueService

    fetch = TelethonTgValue(session) if session is not None and settings.TG_VALUE_ENABLED else None
    tg = TgValueService(fetch, ttl=settings.TG_VALUE_TTL, max_per_min=settings.TG_VALUE_MAX_PER_MIN, clock=clock)
    rate = TonRate(settings.TON_RATE_URL, settings.TON_RATE_TTL, clock=clock)
    portals = PortalsPublic(settings.PORTALS_PUBLIC_URL, settings.PORTALS_PUBLIC_TTL, clock=clock) \
        if settings.PEER_PORTALS_PUBLIC else None
    from .tg_market import build_tg_market

    tg_market = build_tg_market(settings, session, rate=rate, clock=clock)
    return Peers(settings, "tonnel", tg=tg, rate=rate, portals=portals, clock=clock, tg_market=tg_market)


def make_engine(settings: Settings, client, storage: Storage, notifier, session=None) -> Engine:
    from .events import EventLog

    return Engine(
        settings, client, storage, notifier,
        peers=build_peers(settings, session),
        events=EventLog(settings.EVENTS_FILE, settings.EVENTS_MAX_MB),
        snapshot_path=Path(settings.DATA_DIR) / SNAPSHOT_NAME,
    )


async def _cleanup(storage, client, bot, session=None) -> None:
    # Always close: an open aiosqlite connection keeps a non-daemon thread alive.
    if client is not None:
        await client.close()
    if session is not None:
        await session.close()
    if storage is not None:
        await storage.close()
    if bot is not None:
        await bot.session.close()


# ---------------------------------------------------------------- commands


async def cmd_login(settings: Settings, args) -> int:
    from .tg_auth import login, request_init_data

    await login(settings)
    if settings.session_file.exists():
        settings.session_file.chmod(0o600)  # /doctor checks this
    init_data = await request_init_data(settings)
    print(f"OK: got Tonnel initData {mask(init_data, 24)}")
    return 0


async def cmd_run(settings: Settings, args) -> int:
    storage = client = bot = session = None
    lock = RunLock(settings)
    if not lock.acquire():
        raise ConfigError(f"another `run` already uses {lock.path.parent}/ (it holds {lock.path}): stop it first")
    try:
        storage = await open_storage(settings)
        session = telegram_session(settings)
        client, auth = _make_real_client(settings, session)
        await load_rules(settings, storage)
        await _prepare_auth(settings, auth)
        notifier, bot = _make_notifier(settings)
        engine = make_engine(settings, client, storage, notifier, session)
        await engine.start_in_practice()  # LIVE survives a restart only while its 24 h gate is still open

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        tasks = {asyncio.create_task(engine.run_forever(stop), name="engine")}
        dp = None
        if bot is not None:
            from .bot import BotContext, build_dispatcher, set_bot_commands
            from .doctor import make_tg_probe
            from .tg_auth import lookup_gift

            gift_lookup = (lambda slug: lookup_gift(session, slug)) if session is not None else None
            ctx = BotContext(settings, storage, engine, client, peers=engine.peers, tg_session=session,
                             gift_lookup=gift_lookup, tg_probe=make_tg_probe(settings, session))
            dp = build_dispatcher(ctx)
            await set_bot_commands(bot)
            tasks.add(asyncio.create_task(
                dp.start_polling(bot, handle_signals=False), name="bot"))

        mode = "PRACTICE" if await engine.is_dry() else "LIVE"
        rules = await storage.list_rules(enabled_only=True)
        await notifier.send(f"Tonnel autobuy started: {mode}, {len(rules)} active rules. "
                            "/safety for limits, /status for details.")

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        # Graceful stop: no new buy starts, a buy in flight (loop or tap) gets its answer and its purchase
        # row, and the engine ends its cycle on its own; only then is anything still running cancelled.
        # A buy cut off anyway stays recorded as 'unknown' (written before the request) and keeps counting.
        grace = settings.HTTP_TIMEOUT + 5
        if not await engine.drain(grace):
            log.warning("a buy was still in flight after %.0fs; it stays recorded as 'unknown'", grace)
        if dp is not None:
            try:
                await dp.stop_polling()
            except RuntimeError:
                pass
        for t in pending:
            try:
                await asyncio.wait_for(t, timeout=grace)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                t.cancel()
        for t in done:
            t.result()  # re-raise errors (e.g. ConfigError)
        return 0
    finally:
        await _cleanup(storage, client, bot, session)
        lock.release()


async def cmd_once(settings: Settings, args) -> int:
    storage = client = bot = None
    lock = RunLock(settings)
    if not lock.acquire():
        raise ConfigError(f"the bot is running ({lock.path} is held). `once` would open a second Telegram "
                          "client on the same session and buy outside the running bot's spend lock. Stop the "
                          "service first, or use the bot's commands")
    try:
        storage = await open_storage(settings)
        client, auth = _make_real_client(settings)
        await load_rules(settings, storage)
        await _prepare_auth(settings, auth)
        notifier, bot = _make_notifier(settings)
        engine = make_engine(settings, client, storage, notifier)
        await engine.start_in_practice()
        if not await storage.list_rules(enabled_only=True):
            print("No active rules: add them via the bot (/addrule or /hunt) or rules.json")
        print_report(1, await engine.run_cycle(), engine.fees.buy_pct)
        print(f"mode: {'PRACTICE' if await engine.is_dry() else 'LIVE'}")
        return 0
    finally:
        await _cleanup(storage, client, bot)
        lock.release()


async def cmd_floors(settings: Settings, args) -> int:
    from .client import SearchFilters

    if args.json:
        snap = Path(settings.DATA_DIR) / SNAPSHOT_NAME
        if snap.exists():
            print(snap.read_text(encoding="utf-8"))
            return 0
    client, auth = _make_real_client(settings)
    try:
        if args.json:
            from .peers import build_snapshot
            from .pricing import fee_model_from_settings

            if not auth.configured:
                raise ConfigError("`floors --json` needs Tonnel auth when the bot has not written "
                                  f"{SNAPSHOT_NAME} yet: set TG_API_ID and TG_API_HASH (or TONNEL_INIT_DATA)")
            await auth.get()
            table = await client.collection_floors()
            data = {table.display.get(c, c): {"floor": f, "n": table.counts.get(c, 0),
                                              "models": {m: mf for (cc, m), mf in table.models.items() if cc == c}}
                    for c, f in table.collections.items()}
            print(json.dumps(build_snapshot("tonnel", fee_model_from_settings(settings), data, time.time()),
                             indent=2, ensure_ascii=False))
            return 0
        if auth.configured:
            await auth.get()
            table = await client.collection_floors()
            if args.collection:
                c = args.collection.casefold().strip()
                api = table.collections.get(c)
                print(f"{args.collection}: {api:g} TON" if api is not None else f"{args.collection}: n/a")
                for (coll, model), floor in sorted(table.models.items(), key=lambda kv: kv[1]):
                    if coll == c:
                        print(f"  {model}: {floor:g}")
            else:
                for c, floor in sorted(table.collections.items()):
                    print(f"{table.display.get(c, c)}: {floor:g} TON ({table.counts.get(c, 0)} listed)")
            return 0
        if not args.collection:
            raise ConfigError("`floors` without a collection needs Tonnel auth: set TG_API_ID and "
                              "TG_API_HASH (or TONNEL_INIT_DATA), or pass a collection name")
        cheapest = await client.search_listings(
            SearchFilters(collection=args.collection, sort="price_asc", limit=5))
        print(f"{args.collection}: cheapest listings (raw price, fee {settings.BUY_FEE_PCT:g}% extra)")
        for x in cheapest:
            print(f"  {x.price_ton:g} TON - {x.title()}")
        return 0
    finally:
        await client.close()


async def cmd_doctor(settings: Settings, args) -> int:
    from .bot import BotContext, doctor_text
    from .doctor import make_tg_probe

    storage = client = None
    running = service_running(settings)
    # While the service runs it holds the Telegram session: check without opening a second client on it.
    local = without_session(settings) if running else settings
    try:
        storage = await open_storage(settings)
        client, _ = _make_real_client(local)
        engine = make_engine(local, client, storage, Notifier())
        ctx = BotContext(settings, storage, engine, client, peers=engine.peers,
                         tg_probe=None if running else make_tg_probe(settings))
        text = await doctor_text(ctx, cli=True, service_running=running)
        print(text)
        return 1 if "✗" in text else 0
    finally:
        await _cleanup(storage, client, None)


# ---------------------------------------------------------------- mock


class MockClock:
    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _mock_settings(settings: Settings, args, mock_dir: Path, now: float) -> Settings:
    """Write the peer fixtures (ts rewritten to now) into the mock dir and point the settings at them."""
    from .peers import load_fixture_snapshot, write_snapshot

    peer_dir = mock_dir / "peers"
    files = []
    for name in ("floors_portals.json", "floors_mrkt.json"):
        src = FIXTURES / "peers" / name
        if src.exists():
            dst = peer_dir / name
            write_snapshot(dst, load_fixture_snapshot(src, now))
            files.append(str(dst))
    update = {
        "PEER_FLOOR_FILES": ",".join(files),
        "EVENTS_FILE": str(Path(settings.DATA_DIR) / "mock-events.jsonl"),
        "POLL_INTERVAL": max(settings.POLL_INTERVAL, 1.0),
    }
    if args.shadow_hours is not None:
        update.update(SHADOW_MIN_HOURS=args.shadow_hours, SHADOW_MAX_HOURS=args.shadow_hours)
    return settings.model_copy(update=update)


def _tg_fixture(now: float) -> dict:
    path = FIXTURES / "tg_value.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for slug, info in data.items():
        if slug.startswith("_"):
            continue
        info = dict(info)
        days = info.pop("last_sale_days_ago", None)
        if days is not None:
            info["last_sale_date"] = now - days * 86400
        out[slug] = info
    return out


def _tg_resale_fixture() -> dict:
    path = FIXTURES / "tg_resale.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


async def _seed(storage: Storage, now: float, fee_pct: float) -> None:
    """Seed 8 practice buys over 6 days with drifting floors (5 up, 3 down) and a 24 h observation tape."""
    from .models import Listing

    def gift(lid: str, model: str, number: int, price: float) -> Listing:
        return Listing(lid, "Lunar Snake", model, "Onyx Black (1%)", "Star (0.3%)", number, price, None,
                       raw={"seed": True, "asset": "TON"})

    albino = "lunar snake|albino||"
    cobra = "lunar snake|cobra||"
    for i, hours_ago in enumerate((144, 120, 96, 72, 48, 24, 12, 2)):
        ts = now - hours_ago * 3600
        await storage.add_snapshot(ts, albino, round(1.30 + 0.25 * i / 7, 3), 4, "cluster")
        await storage.add_snapshot(ts, cobra, round(1.30 - 0.30 * i / 7, 3), 4, "cluster")
    seeds = [(1, "Albino (1.2%)", 1.05, 140), (1, "Albino (1.2%)", 1.10, 118), (1, "Albino (1.2%)", 1.15, 95),
             (1, "Albino (1.2%)", 1.20, 70), (1, "Albino (1.2%)", 1.25, 47), (2, "Cobra (2%)", 0.90, 130),
             (2, "Cobra (2%)", 1.00, 100), (2, "Cobra (2%)", 1.10, 60)]
    for i, (rule_id, model, price, hours_ago) in enumerate(seeds):
        listing = gift(f"seed-{i}", model, 4300 + i, price)
        await storage.add_purchase(listing, cost_ton=round(price * (1 + fee_pct / 100), 6), floor_ton=None,
                                   rule_id=rule_id, dry=True, status="dry", message="seeded practice",
                                   created_at=now - hours_ago * 3600)
    tape = [("tape-1", "Albino (1.2%)", 1.00, 20), ("tape-2", "Albino (1.2%)", 1.65, 18),
            ("tape-3", "Cobra (2%)", 0.90, 16), ("tape-4", "Albino (1.2%)", 1.10, 11),
            ("tape-5", "Albino (1.2%)", 1.62, 9), ("tape-6", "Cobra (2%)", 0.88, 8),
            ("tape-7", "Albino (1.2%)", 0.98, 6), ("tape-8", "Albino (1.2%)", 0.85, 4)]
    for lid, model, price, hours_ago in tape:
        await storage.upsert_observations([gift(lid, model, 5000 + int(hours_ago), price)], now - hours_ago * 3600)
    await storage.add_decision(now - 4 * 3600, "tape-8", 0.85, None, "skip", "LOCKED",
                               {"text": "locked: export_at: unlocks in 5 d", "title": "Lunar Snake #5004"})


async def _fake_tap(engine: Engine, action: str, now: float, tokens: list[str] | None = None) -> None:
    """Simulated taps: run the real tap handler on every pending card, then try one double tap."""
    if tokens is None:
        tokens = [c.token for c in await engine.storage.list_pending(("pending",))]
    first = None
    for token in tokens:
        card = await engine.storage.get_pending(token)
        result = await engine.tap(action[0], token, MOCK_ADMIN_ID, now)
        if card is not None and not result.ok and "Stopped" in result.text:
            print(f"[tap {action}] {card.payload.get('title')}: {result.text}", flush=True)
            continue
        print(f"[tap {action}] {result.text}", flush=True)
        first = first or (card.token if card else None)
    if first and action == "buy":
        again = await engine.tap("b", first, MOCK_ADMIN_ID, now)
        print(f"[tap {action}] double tap on the same card: {again.text}", flush=True)


def _section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


async def cmd_mock(settings: Settings, args) -> int:
    from collections import Counter

    from .bot import (BotContext, compare_text, digest_action, doctor_text, dry_off_gate, gate_action, peers_text,
                      pnl_text, resume_prompt, safety_for, score_text, why_for)
    from .callbacks import GateCB
    from .client import DEMO_RULES, MockClient
    from .digest import digest_card, hint_keyboard
    from .engine import DAY
    from .events import EventLog, read_events, validate_event
    from .fx import TonRate
    from .ledger import scoreboard
    from .peers import FakeTgValue, Peers, TgValueService, read_snapshot
    from .tg_market import MockTgMarket, TgMarket
    from .shadow import fill_line, fills_summary_line
    from .rules import parse_addrule_args
    from .wizard import preview_for

    start = time.time()
    clock = MockClock(start)
    mock_dir = Path(settings.DATA_DIR) / "mock"
    s = _mock_settings(settings, args, mock_dir, start)
    events_path = Path(s.EVENTS_FILE)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")

    storage = await open_storage(s, ":memory:")  # fresh state on every mock run
    notifier, bot = _make_notifier(s, use_bot=args.telegram)
    client = MockClient("demo", now=start)
    tg = TgValueService(FakeTgValue(_tg_fixture(start)), ttl=s.TG_VALUE_TTL, max_per_min=s.TG_VALUE_MAX_PER_MIN,
                        clock=clock)
    rate = TonRate(s.TON_RATE_URL, fixed=1.25)
    # Telegram's own resale market from fixtures: no Telethon call in mock mode.
    tg_market = TgMarket(MockTgMarket(_tg_resale_fixture()), limit=s.TG_MARKET_LIMIT, min_interval=0,
                         stars_usd=s.STARS_USD_RATE, rate=rate, clock=clock)
    peers = Peers(s, "tonnel", tg=tg, rate=rate, clock=clock, tg_market=tg_market)
    try:
        for rule in DEMO_RULES:
            await storage.add_rule(replace(rule, id=None))
        if args.shadow_hours is not None:
            await storage.update_rule(1, state="shadow")
            await storage.set_setting("shadow_since:1", start - 6 * DAY)
        engine = Engine(s, client, storage, notifier, force_dry=True, clock=clock, peers=peers,
                        events=EventLog(events_path, s.EVENTS_MAX_MB), snapshot_path=mock_dir / SNAPSHOT_NAME)
        await _seed(storage, start, engine.fees.buy_pct)
        engine.admin_ids.add(MOCK_ADMIN_ID)
        ctx = BotContext(s, storage, engine, client, peers=peers)
        rules = await storage.list_rules()
        print(f"MOCK mode: fake Tonnel client, {plural(len(rules), 'demo rule')}, {plural(args.cycles, 'cycle')}, "
              f"PRACTICE forced (DRY_RUN lock), tap={args.tap}")
        for rule in rules:
            print(f"  rule {rule.describe()}")
        print(f"  {engine.fees.describe(s.FEES_VERIFIED)}")

        totals: Counter = Counter()
        doctor_report = ""
        for i in range(1, args.cycles + 1):
            _section(f"cycle {i}")
            client.next_cycle()
            before = client.requests
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"cycle {i}: {report.summary()} · requests={client.requests - before}")
            for line in report.rejections:
                print(f"  {line}")
            tokens = None
            if args.panic and i == 1:
                _section("/panic")
                tokens = [c.token for c in await storage.list_pending(("pending",))]
                _, older_resume = await resume_prompt(ctx)  # a [Confirm resume] sent before the /panic
                await engine.panic()
                print(await safety_for(ctx))
            if args.tap != "none":
                if i == 1 and not args.panic:
                    client.reprice("823002", 2.40)  # someone repriced the confirm card's listing before the tap
                await _fake_tap(engine, args.tap, clock(), tokens)
            if i == args.cycles and args.doctor:
                doctor_report = await doctor_text(ctx)
            clock.advance(120)
            await engine.tick(marks_search=False)

        if args.crash:
            _section("crash cycle")
            client.crash("Lunar Snake", "Cobra", 0.7)
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"crash cycle: {report.summary()}")
            clock.advance(120)

        if args.panic:
            _section("/resume (after /panic)")

            async def tap_gate(kb) -> str:
                data = GateCB.unpack(kb.inline_keyboard[0][0].callback_data)
                return await gate_action(ctx, data.action, data.t, data.n)

            text, kb = await resume_prompt(ctx)
            print(text)
            print(f"[buttons: {' | '.join(b.text for row in kb.inline_keyboard for b in row)}]")
            print(f"[tap an older Confirm resume, sent before /panic] {await tap_gate(older_resume)}")
            print(f"[tap Confirm resume] {await tap_gate(kb)}")
            print(f"[tap Confirm resume again] {await tap_gate(kb)}")

        now = clock()
        await engine.run_marks(now, search=False)
        _section("/pnl")
        print(await pnl_text(ctx, 7, html_mode=False))
        _section("/score")
        print(await score_text(ctx))
        _section("shadow fills (net of fees)")
        fills = await storage.shadow_fills()
        for fill in fills:
            print(fill_line(fill))
        print(fills_summary_line(fills) if fills else "Shadow fills: none (no rule in shadow; try --shadow-hours 0)")
        _section("/dry off (gate, as it would answer with DRY_RUN=false)")
        gate_text, gate_kb = await dry_off_gate(ctx, False, preview=True)
        print(gate_text.split("\n\n")[-1])
        print(f"[buttons: {' | '.join(b.text for row in gate_kb.inline_keyboard for b in row)}]")
        print((await dry_off_gate(ctx, False))[0].split("\n")[0])
        _section("/scoreboard")
        print(await scoreboard(storage, s, await storage.list_rules(enabled_only=True), now, engine.fees))
        _section("/why 814412 (the listing skipped because it loses after fees)")
        print(await why_for(ctx, "814412"))
        _section("digest preview")
        digest, hint = await digest_card(engine, now - DAY, now + 1, title="Digest, last 24 h")
        print(digest)
        kb = hint_keyboard(hint, now)
        if kb is not None:
            print(f"[buttons: {' | '.join(b.text for row in kb.inline_keyboard for b in row)}]")
            ask, confirm_kb = await digest_action(ctx, "ask", hint.rule_id, int(round(hint.new_max * 100)))
            print(f"[tap Raise] {ask}")
            print(f"[buttons: {' | '.join(b.text for row in confirm_kb.inline_keyboard for b in row)}]")
            print(f"[tap Cancel] {(await digest_action(ctx, 'keep', hint.rule_id, 0))[0]}")
        _section("/safety")
        print(await safety_for(ctx))
        _section("/peers")
        print(await peers_text(ctx))
        _section("/compare Spy Agaric")
        print(await compare_text(ctx, "Spy Agaric", None))

        _section("files")
        snap = read_snapshot(str(mock_dir / SNAPSHOT_NAME), "other", now, 10**9)
        print(f"own snapshot {mock_dir / SNAPSHOT_NAME}: {snap.status}, "
              f"{plural(len((snap.data or {}).get('collections', {})), 'collection')}")
        tg_snap = read_snapshot(str(mock_dir / "floors_telegram.json"), "tonnel", now, 10**9)
        print(f"telegram snapshot {mock_dir / 'floors_telegram.json'}: {tg_snap.status}, "
              f"{plural(len((tg_snap.data or {}).get('collections', {})), 'collection')} "
              "(fixture data, no Telethon call)")
        events = read_events(events_path)
        bad = sum(1 for e in events if validate_event(e))
        print(f"events {events_path}: {plural(len(events), 'line')}, {bad} invalid")

        if args.preview:
            _section(f"preview: {args.preview}")
            rule = parse_addrule_args(args.preview)
            print(f"Rule: {rule.describe()}")
            print((await preview_for(ctx, rule, now)).text)
        if args.doctor:
            _section("doctor (mock, right after the last cycle)")
            print(doctor_report or await doctor_text(ctx))

        _section("summary")
        purchases = await storage.recent_purchases(100)
        fresh = [p for p in purchases if p["message"] != "seeded practice"]
        print(f"recorded purchases: {len(purchases)} ({len(purchases) - len(fresh)} seeded)")
        for p in reversed(fresh):
            floor = f" (floor {p['floor_ton']:.2f})" if p["floor_ton"] else ""
            num = f" #{p['number']}" if p["number"] is not None else ""
            print(f"  #{p['id']} {'PRACTICE' if p['dry'] else p['status']} {p['collection']}{num} "
                  f"{p['price_ton']:g} TON, {p['cost_ton']:.2f} TON all-in{floor}")
        print(f"client.buy() calls: {len(client.bought)} (practice never buys)")
        print("skip reasons: " + " · ".join(f"{k} {v}" for k, v in totals.most_common()))
        return 0
    finally:
        await _cleanup(storage, None, bot)


COMMANDS = {"login": cmd_login, "run": cmd_run, "once": cmd_once, "mock": cmd_mock,
            "floors": cmd_floors, "doctor": cmd_doctor}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m tonnel_autobuy",
        description="Tonnel (Telegram gifts) autobuy sniper with net-of-fees deal math. "
                    "Practice mode (DRY_RUN=true) by default.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="interactive Telegram login (creates the Telethon session)")
    sub.add_parser("run", help="poll loop + admin bot")
    sub.add_parser("once", help="run a single scan cycle with the real API and exit")
    m = sub.add_parser("mock", help="offline run with a fake client and fixtures (forced practice)")
    m.add_argument("--cycles", type=int, default=2, help="number of cycles (default 2)")
    m.add_argument("--telegram", action="store_true",
                   help="send notifications via BOT_TOKEN/ADMIN_IDS instead of stdout")
    m.add_argument("--tap", choices=("none", "buy", "skip"), default="none",
                   help="tap this button on every pending card after each cycle (simulated taps)")
    m.add_argument("--crash", action="store_true", help="add a cycle where a floor falls 30%%")
    m.add_argument("--panic", action="store_true", help="press /panic after cycle 1")
    m.add_argument("--preview", metavar="RULE",
                   help='preview a rule on the 24 h tape, e.g. "Lunar Snake profit=0.3 max=3"')
    m.add_argument("--doctor", action="store_true", help="print /doctor against the mock client")
    m.add_argument("--shadow-hours", type=float, default=None, metavar="H",
                   help="start rule #1 in shadow and print its hindsight card after H hours (0 = now)")
    f = sub.add_parser("floors", help="print collection floors")
    f.add_argument("collection", nargs="?", help="collection name, e.g. 'Toy Bear'")
    f.add_argument("--json", action="store_true",
                   help="print this bot's floors snapshot (the JSON format other bots read, see README)")
    sub.add_parser("doctor", help="self-check: config, credentials, API, DB, clock; exit 1 on any failure")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "win32" and os.environ.get("ALLOW_UNSUPPORTED_OS") != "1":
        print("This build is Windows-only. Install it on Windows 10/11, or set ALLOW_UNSUPPORTED_OS=1 to run it anyway (unsupported).", file=sys.stderr)
        return 1
    _updater.startup()
    try:
        settings = get_settings()
        setup_logging(settings.LOG_LEVEL)
        return asyncio.run(COMMANDS[args.cmd](settings, args))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
