"""Regression tests for the v0.1.0 release check: the DRY_RUN lock cancels an armed live window, old resume
buttons cannot undo a newer stop, /unmute, strict rules.json parsing, the wizard's typed max, the race count,
the run lock between `run`, `once` and CLI `doctor`, and the user-facing wording."""

import asyncio
import json
from dataclasses import replace

import pytest
from aiogram import Bot
from helpers import ADMIN, LS, Clock, L, demo_engine, settings
from test_wizard import Driver, FakeSession

from tonnel_autobuy.bot import (BotContext, build_dispatcher, gate_action, mute_key, resume_prompt, rule_action,
                                go_live_button)
from tonnel_autobuy.callbacks import GateCB
from tonnel_autobuy.client import DEMO_RULES, MockClient
from tonnel_autobuy.config import Settings
from tonnel_autobuy.doctor import DoctorDeps, check_bot, check_engine, check_market_auth, check_session
from tonnel_autobuy.engine import Engine
from tonnel_autobuy.ledger import races_line, races_lost
from tonnel_autobuy.models import plural
from tonnel_autobuy.notifier import MemoryNotifier
from tonnel_autobuy.rules import BuyRule, RuleParseError, load_rules_file, rule_from_dict
from tonnel_autobuy.safety import safety_text
from tonnel_autobuy.storage import Storage

SNAKE = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="demo")]


def _gate(kb) -> GateCB:
    """The GateCB of a keyboard's first button ([Confirm resume] / a breaker's [Resume])."""
    return GateCB.unpack(kb.inline_keyboard[0][0].callback_data)


async def _live(rules=SNAKE, **over):
    clock = Clock()
    engine, storage, client, notifier, _ = await demo_engine(settings(DRY_RUN=False, **over), rules=rules,
                                                             force_dry=False, clock=clock,
                                                             client=MockClient("demo", now=clock()))
    return engine, storage, client, notifier, clock


# --- DRY_RUN=true cancels an armed live window ------------------------------------------------------------


def test_dry_run_lock_cancels_the_live_window_for_good(tmp_path):
    """Arm LIVE, restart with DRY_RUN=true, restart with DRY_RUN=false: practice until a new /dry off."""
    db, clock = str(tmp_path / "t.sqlite3"), Clock()

    async def boot(dry_run: bool):
        storage = await Storage(db).open()
        client = MockClient("demo", now=clock())
        client.next_cycle()
        engine = Engine(settings(DRY_RUN=dry_run), client, storage, MemoryNotifier(), clock=clock)
        await engine.start_in_practice()
        return engine, storage, client

    async def go():
        engine, storage, _ = await boot(False)
        await storage.add_rule(replace(DEMO_RULES[0], id=None))
        assert await engine.arm_live() is None and not await engine.is_dry()
        await engine.issue_gate_nonce()  # an unused [Go live] button from a /dry off report
        await storage.close()

        clock.advance(3600)
        engine, storage, _ = await boot(True)  # the user sets DRY_RUN=true to stop it
        assert await engine.is_dry()
        assert await engine.live_until() is None and await storage.get_setting("gate_nonce") is None
        await storage.close()

        clock.advance(4 * 3600)  # still inside the old 24 h window
        engine, storage, client = await boot(False)
        assert await engine.is_dry()  # no new /dry off: practice
        await engine.run_cycle()
        assert client.bought == []
        await storage.close()

    asyncio.run(go())


def test_dry_run_lock_voids_the_window_even_without_a_restart():
    async def go():
        engine, storage, client, _, clock = await _live()
        try:
            assert await engine.arm_live() is None
            locked = Engine(settings(DRY_RUN=True), client, storage, MemoryNotifier(), clock=clock)
            assert await locked.is_dry()  # any check under the lock cancels the stored window
            assert await engine.is_dry() and await engine.live_until() is None
        finally:
            await storage.close()

    asyncio.run(go())


# --- old buttons can never undo a newer stop --------------------------------------------------------------


def test_old_confirm_resume_cannot_undo_a_newer_breaker_within_its_ttl():
    async def go():
        engine, storage, client, notifier, clock = await _live()
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            assert await engine.arm_live() is None
            await engine.set_paused(True)  # /pause
            _, old_kb = await resume_prompt(ctx)  # /resume: [Confirm resume], not tapped yet
            clock.advance(120)
            await engine._balance_guard(100.0, clock())
            assert await engine._balance_guard(80.0, clock())  # a newer breaker pause
            clock.advance(60)
            old = _gate(old_kb)
            text = await gate_action(ctx, old.action, old.t, old.n)
            assert "out of date" in text and "Still paused" in text and await engine.is_paused()
            await engine.run_cycle()
            assert client.bought == []

            breaker = _gate(notifier.sent[-1]["keyboard"])  # the breaker's own [Resume] still works
            assert (await gate_action(ctx, breaker.action, breaker.t, breaker.n)).startswith("Resumed")
            assert not await engine.is_paused()
            assert "out of date" in await gate_action(ctx, breaker.action, breaker.t, breaker.n)  # once only
        finally:
            await storage.close()

    asyncio.run(go())


def test_old_confirm_resume_cannot_undo_a_newer_panic_within_its_ttl():
    async def go():
        engine, storage, client, _, clock = await _live()
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            _, old_kb = await resume_prompt(ctx)
            clock.advance(60)
            await engine.panic()
            clock.advance(60)
            old = _gate(old_kb)
            assert "out of date" in await gate_action(ctx, old.action, old.t, old.n)
            assert await engine.is_panic() and await engine.is_paused()
            assert "out of date" in await gate_action(ctx, "resume", int(clock()))  # no nonce: stale
            _, kb = await resume_prompt(ctx)
            new = _gate(kb)
            assert (await gate_action(ctx, new.action, new.t, new.n)).startswith("Resumed")
            assert not await engine.is_panic()
        finally:
            await storage.close()

    asyncio.run(go())


def test_losing_streak_voids_an_unused_go_live_button():
    async def go():
        engine, storage, client, _, clock = await _live()
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            assert await engine.arm_live() is None
            nonce = await engine.issue_gate_nonce()  # [Go live] sent just before the streak
            for i in range(3):
                pid = await storage.add_purchase(L(LS(4500 + i), 1.0), cost_ton=1.1, floor_ton=1.6, rule_id=1,
                                                 dry=False, status="ok", created_at=clock() - 100 + i)
                await storage.add_mark(pid, clock(), 0.8, 0.8, "high")
            await engine._losing_streak(clock())
            assert await engine.is_dry() and await storage.get_setting("gate_nonce") is None
            assert "out of date" in await go_live_button(ctx, nonce)
            assert await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


def test_crash_resume_button_cannot_undo_a_newer_pause_of_the_same_rule():
    async def go():
        engine, storage, client, _, clock = await _live(CRASH_PAUSE_MIN=120)
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            sent = int(clock())
            await storage.set_setting("crash_alert:1", clock())
            await storage.set_setting("rule_paused_until:1", clock() + 7200)
            await storage.delete_setting("rule_paused_until:1")  # resumed early with a fresh button
            clock.advance(3700)  # a newer crash pause, still inside the old button's 120 min TTL
            await storage.set_setting("crash_alert:1", clock())
            await storage.set_setting("rule_paused_until:1", clock() + 7200)
            assert "out of date" in await rule_action(ctx, "resume", 1, sent)
            assert await storage.get_float("rule_paused_until:1") is not None
            assert await rule_action(ctx, "resume", 1, int(clock())) == "Rule #1 resumed"
        finally:
            await storage.close()

    asyncio.run(go())


# --- /mute and /unmute -------------------------------------------------------------------------------------


def test_unmute_accepts_the_spelling_that_muted():
    assert mute_key("Toy Bear") == mute_key("toy bear (1%)") == "toy bear"
    assert mute_key(" Toy Bear : Wizard (1.5%) ") == "toy bear:wizard"

    async def go():
        engine, storage, _, _, _ = await demo_engine()
        session = FakeSession()
        d = Driver(build_dispatcher(BotContext(engine.settings, storage, engine, engine.client)),
                   Bot(token="42:TEST", session=session), session)
        try:
            await d.text("/mute Toy Bear")
            assert d.last_text().startswith("Muted toy bear for 24 h")
            await d.text("/unmute Toy Bear")
            assert d.last_text() == "Unmuted toy bear."
            assert await storage.list_mutes(engine.clock()) == []
        finally:
            await storage.close()

    asyncio.run(go())


# --- rules.json -------------------------------------------------------------------------------------------


def test_rules_json_short_keys_mean_the_same_as_addrule():
    r = rule_from_dict({"collection": "Toy Bear", "max": 5, "discount": 20, "profit": 0.5, "profit%": 10,
                        "conf": "high", "locked": 3, "enabled": "false", "_note": "a comment"})
    assert (r.max_price_ton, r.min_discount_pct, r.min_profit_ton, r.min_profit_pct) == (5, 20, 0.5, 10)
    assert (r.min_confidence, r.allow_locked_days, r.enabled) == ("high", 3, False)
    assert rule_from_dict({"collection": "Toy Bear", "max": 5, "enabled": "yes"}).enabled is True
    assert rule_from_dict({"collection": "Toy Bear", "max": 5, "enabled": 0}).enabled is False


@pytest.mark.parametrize("extra", [
    {"min_discont_pct": 20},            # a typo would silently drop the filter
    {"max_price_ton": 4},               # both spellings of one key
    {"enabled": "maybe"},
    {"profit": -1},
    {"discount": "lots"},
    {"discount": 100},
    {"locked": float("nan")},
    {"conf": "very"},
])
def test_rules_json_never_loosens_a_rule_silently(extra, tmp_path):
    entry = {"collection": "Toy Bear", "max": 5, **extra}
    with pytest.raises(RuleParseError):
        rule_from_dict(entry)
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    with pytest.raises(RuleParseError):
        load_rules_file(path)


def test_rules_example_file_still_loads():
    rules = load_rules_file("rules.example.json")
    assert len(rules) == 3 and rules[2].enabled is False and rules[1].min_confidence == "high"


# --- /hunt typed max ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("typed", ["nan", "inf", "-inf", "0", "-2"])
def test_wizard_rejects_a_typed_max_that_is_not_a_positive_number(typed):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from tonnel_autobuy.wizard import Hunt, build_wizard_router

    class Msg:
        def __init__(self, text):
            self.text, self.out = text, []

        async def answer(self, text, **kw):
            self.out.append(text)

    async def go():
        engine, storage, client, _, _ = await demo_engine(rules=[])
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            router = build_wizard_router(lambda *_: True, lambda *_: True)
            on_max_text = next(h.callback for h in router.message.handlers if h.callback.__name__ == "on_max_text")
            state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
            await state.set_state(Hunt.typing_max)
            await state.update_data(collection="Lunar Snake", model="Albino", goal=("discount", 10.0))
            m = Msg(typed)
            await on_max_text(m, state=state, ctx=ctx)
            assert m.out == ["The max must be a number above 0, e.g. 2.5"]
            assert await state.get_state() == Hunt.typing_max and "max" not in await state.get_data()
        finally:
            await storage.close()

    asyncio.run(go())


# --- races probably lost -----------------------------------------------------------------------------------


def test_races_lost_counts_only_listings_that_vanished_while_the_bot_kept_polling():
    async def go():
        engine, storage, client, _, clock = await demo_engine(rules=SNAKE)
        try:
            await engine.run_cycle()  # a practice buy of Lunar Snake #4410
            positions = await storage.positions(dry=True)
            assert len(positions) == 1
            later = clock() + 600
            assert await races_lost(storage, positions, 120, later) == (0, 0)  # paused/stopped: no evidence
            other = L(LS(4999), 2.0, number=4999)
            await storage.upsert_observations([other], clock() + 60)  # the next poll no longer lists #4410
            assert await races_lost(storage, positions, 120, later) == (1, 1)
        finally:
            await storage.close()

    asyncio.run(go())
    assert races_line(0, 0, 120) == "Races probably lost: none to check yet"
    assert races_line(1, 1, 120) == "Races probably lost: 1 of 1 practice buy (listing gone within 2 min)"
    assert races_line(0, 2, 120) == "Races probably lost: 0 of 2 practice buys (listing gone within 2 min)"


# --- one `run` per data directory; CLI doctor never opens a second Telegram client ------------------------


def test_run_lock_blocks_once_and_a_second_run(tmp_path, monkeypatch):
    pytest.importorskip("fcntl")
    from tonnel_autobuy.__main__ import RunLock, cmd_once, cmd_run, service_running, without_session
    from tonnel_autobuy.config import ConfigError

    s = settings(DATA_DIR=str(tmp_path), DB_PATH=str(tmp_path / "db.sqlite3"), TG_API_ID=1, TG_API_HASH="h")
    assert not service_running(s)
    lock = RunLock(s)
    assert lock.acquire()
    try:
        assert service_running(s)
        with pytest.raises(ConfigError, match="the bot is running"):
            asyncio.run(cmd_once(s, None))
        with pytest.raises(ConfigError, match="another `run`"):
            asyncio.run(cmd_run(s, None))
    finally:
        lock.release()
    assert not service_running(s)
    assert not without_session(s).has_tg_credentials and s.has_tg_credentials


def test_cli_doctor_reports_the_service_instead_of_its_own_idle_engine():
    async def go():
        engine, storage, _, _, _ = await demo_engine(settings(TG_API_ID=1, TG_API_HASH="h", BOT_TOKEN="1:x"))
        try:
            s = engine.settings
            check = await check_engine(DoctorDeps(s, engine, cli=True, service_running=True))
            assert check.status == "ok" and "running in another process" in check.text
            check = await check_engine(DoctorDeps(s, engine, cli=True))
            assert check.status == "warn" and "not running" in check.text and "run" in check.fix
            for fn in (check_session, check_market_auth):  # no second Telethon client on the session file
                check = await fn(DoctorDeps(s, engine, cli=True, service_running=True))
                assert check.status == "warn" and "running bot holds the Telegram session" in check.text
            check = await check_bot(DoctorDeps(settings(BOT_TOKEN="", ADMIN_IDS="42"), engine))
            assert check.line() == "✗ Bot config: BOT_TOKEN not set → Add it to .env (see .env.example)"
        finally:
            await storage.close()

    asyncio.run(go())


# --- wording ----------------------------------------------------------------------------------------------


def test_counts_and_nouns_agree():
    assert [plural(n, "buy") for n in (0, 1, 2)] == ["0 buys", "1 buy", "2 buys"]
    assert plural(1, "comparable listing") == "1 comparable listing"


def test_one_comparable_card_reads_in_the_singular():
    async def go():
        engine, storage, client, notifier, _ = await demo_engine(settings(MAX_BUYS_PER_HOUR=2))
        try:
            client.next_cycle()
            await engine.run_cycle()
            text = "\n".join(notifier.messages)
            assert "1 comparables" not in text and "1 buys" not in text
            assert "(low, 1 comparable)" in text
        finally:
            await storage.close()

    asyncio.run(go())


def test_practice_mode_says_how_going_live_really_works():
    from tonnel_autobuy.bot import HELP
    from tonnel_autobuy.safety import SafetyState

    s = settings(DRY_RUN=False)
    st = SafetyState(live=False, locked=False, live_until=None, panic=False, paused=False, spent_today=0,
                     buys_last_hour=0, max_price=10, daily_budget=50, max_per_hour=3)
    first = safety_text(st, s).splitlines()[0]
    assert "/dry off confirm" in first and "arms LIVE" not in first
    assert "/dry off confirm" in HELP and "arms LIVE for 24 h" not in HELP
    assert "sibling" not in HELP.lower()


def test_utc_is_the_default_time_zone():
    assert Settings(_env_file=None).TZ_OFFSET_HOURS == 0
    assert not hasattr(Settings(_env_file=None), "PORTALS_BOT_USERNAME")


def test_mock_panic_shows_the_resume_confirm_step(capsys, tmp_path, monkeypatch):
    from tonnel_autobuy.__main__ import main
    from tonnel_autobuy.config import get_settings

    monkeypatch.chdir(tmp_path)  # no .env here; the mock writes only below ./data
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    try:
        assert main(["mock", "--cycles", "2", "--panic"]) == 0
    finally:
        get_settings.cache_clear()
    out = capsys.readouterr().out
    assert "--- /resume (after /panic) ---" in out and "[buttons: Confirm resume | Keep paused]" in out
    assert "[tap an older Confirm resume, sent before /panic] This button is out of date." in out
    assert "[tap Confirm resume] Resumed. Mode stays practice until a new /dry off." in out
    assert "Races probably lost: none to check yet" in out  # it stopped polling after /panic
    assert "F2" not in out and "Sibling" not in out
