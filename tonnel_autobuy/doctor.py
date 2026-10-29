"""/doctor and `python -m tonnel_autobuy doctor`: a self-check with a fix hint per failing item.

Each check is a small async function returning (status, text, fix). Checks run concurrently, each
bounded by a 5 s timeout, so the whole report finishes in well under 10 s. Nothing here ever
prints the phone number or a secret.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .models import norm, plural

if TYPE_CHECKING:
    from .config import Settings
    from .engine import Engine

CHECK_TIMEOUT = 5.0
SYMBOL = {"ok": "✓", "warn": "!", "fail": "✗"}
WORD = {"ok": "PASS", "warn": "WARN", "fail": "FAIL"}

TgProbe = Callable[[], Awaitable[tuple[bool, bool | None]]]


@dataclass(slots=True)
class Check:
    name: str
    status: str  # ok | warn | fail
    text: str
    fix: str = ""

    def line(self) -> str:
        text = f"{SYMBOL[self.status]} {self.name}: {self.text}"
        return text + (f" → {self.fix}" if self.fix and self.status != "ok" else "")


@dataclass(slots=True)
class DoctorDeps:
    settings: "Settings"
    engine: "Engine | None" = None
    tg_probe: TgProbe | None = None  # (authorized, has_password); None = Telethon not configured
    session_file: Path | None = None
    peers_status: Callable[[], Awaitable[list[str]]] | None = None
    now: float = field(default_factory=time.time)
    pkg: str = "tonnel_autobuy"
    cli: bool = False  # `python -m ... doctor`: the engine loop does not run in this process
    service_running: bool = False  # CLI only: a `run` process holds DATA_DIR/run.lock and the Telegram session


IN_SERVICE = "not checked here: the running bot holds the Telegram session"
IN_SERVICE_FIX = "Send /doctor to the bot for this check"


def _c(name: str, status: str, text: str, fix: str = "") -> Check:
    return Check(name, status, text, fix)


async def check_bot(d: DoctorDeps) -> Check:
    s = d.settings
    try:
        admins = s.admin_ids
    except Exception:
        return _c("Bot config", "fail", "ADMIN_IDS is not a list of integers", "Fix ADMIN_IDS in .env (comma-separated ids)")
    if s.BOT_TOKEN and admins:
        return _c("Bot config", "ok", f"BOT_TOKEN set, {plural(len(admins), 'admin')}")
    missing = [k for k, v in (("BOT_TOKEN", s.BOT_TOKEN), ("ADMIN_IDS", admins)) if not v]
    return _c("Bot config", "fail", f"{' and '.join(missing)} not set",
              f"Add {'it' if len(missing) == 1 else 'them'} to .env (see .env.example)")


async def check_session(d: DoctorDeps) -> Check:
    login = f"python -m {d.pkg} login"
    if d.service_running and d.settings.has_tg_credentials:
        return _c("Telegram session", "warn", IN_SERVICE, IN_SERVICE_FIX)
    if d.tg_probe is None:
        if d.settings.TONNEL_INIT_DATA:
            return _c("Telegram session", "warn", "not configured; running on TONNEL_INIT_DATA until it expires", login)
        return _c("Telegram session", "fail", "not logged in (TG_API_ID / TG_API_HASH missing)", login)
    if d.session_file is not None and not d.session_file.exists():
        return _c("Telegram session", "fail", "not logged in (no session file)", login)
    authorized, _ = await d.tg_probe()
    return _c("Telegram session", "ok", "authorized") if authorized else _c("Telegram session", "fail", "not logged in", login)


async def check_2fa(d: DoctorDeps) -> Check:
    fix = "Telegram → Settings → Privacy and Security → Two-Step Verification"
    if d.service_running and d.settings.has_tg_credentials:
        return _c("Two-step verification", "warn", IN_SERVICE, IN_SERVICE_FIX)
    if d.tg_probe is None or (d.session_file is not None and not d.session_file.exists()):
        return _c("Two-step verification", "warn", "cannot check without a Telegram session", fix)
    authorized, has_password = await d.tg_probe()
    if not authorized:
        return _c("Two-step verification", "warn", "cannot check: session not authorized", fix)
    if has_password:
        return _c("Two-step verification", "ok", "on")
    return _c("Two-step verification", "fail", "off: a stolen session file is enough to take the account", fix)


async def check_session_mode(d: DoctorDeps) -> Check:
    fix = "chmod 600 data/*.session"
    if d.session_file is None or not d.session_file.exists():
        return _c("Session file permissions", "warn", "no session file yet", "")
    if os.name == "nt":
        # POSIX modes are not meaningful on Windows; the profile folder is per-user by default.
        return _c("Session file permissions", "ok", "private to your user profile")
    mode = os.stat(d.session_file).st_mode & 0o777
    if mode == 0o600:
        return _c("Session file permissions", "ok", "0600")
    return _c("Session file permissions", "warn", f"{mode:04o}, other users may read it", fix)


async def check_market_auth(d: DoctorDeps) -> Check:
    e = d.engine
    if e is None:
        return _c("Market auth", "warn", "engine not running", "")
    if d.service_running and not d.settings.TONNEL_INIT_DATA and d.settings.has_tg_credentials:
        return _c("Market auth", "warn", IN_SERVICE, IN_SERVICE_FIX)
    if not getattr(e.client, "has_auth", True):
        fix = f"Set TG_API_ID/TG_API_HASH and run python -m {d.pkg} login (or set TONNEL_INIT_DATA)"
        if await e.is_dry(d.now):
            return _c("Market auth", "warn", "no Tonnel auth: practice on public listings only (no API floors, "
                                             "no /balance)", fix)
        return _c("Market auth", "fail", "no Tonnel auth: live buying is impossible", fix)
    try:
        started = time.perf_counter()
        balance = await e.client.balance()
        ms = (time.perf_counter() - started) * 1000
    except Exception as exc:
        return _c("Market auth", "fail", f"balance request failed ({type(exc).__name__})",
                  f"python -m {d.pkg} login, or set a fresh TONNEL_INIT_DATA")
    provider = getattr(e.client, "_auth", None)
    age = getattr(provider, "age", None)
    extra = f", initData {age:.0f}s old of INIT_DATA_TTL {d.settings.INIT_DATA_TTL}s" if age is not None else ""
    return _c("Market auth", "ok", f"balance {balance:.2f} TON ({ms:.0f} ms){extra}")


async def check_reachable(d: DoctorDeps) -> Check:
    e = d.engine
    if e is None:
        return _c("Market reachable", "warn", "engine not running", "")
    try:
        started = time.perf_counter()
        if getattr(e.client, "has_auth", True):
            floors = await e.client.collection_floors()  # filterStats (needs initData)
            count = len(floors)
            what = f"{plural(count, 'collection')} (filterStats)"
        else:  # pageGifts works without a Telegram account
            from .client import SearchFilters

            rules = await e.storage.list_rules(enabled_only=True)
            probe = rules[0].collection if rules else "Toy Bear"
            await e.client.search_listings(SearchFilters(collection=probe, sort="price_asc", limit=1))
            count, what = 1, f"public listings answer (pageGifts, {probe})"
        ms = getattr(e.client, "last_latency_ms", None) or (time.perf_counter() - started) * 1000
    except Exception as exc:
        return _c("Market reachable", "fail", f"Tonnel request failed ({type(exc).__name__})",
                  "Check PROXY / network (Tonnel is behind Cloudflare)")
    if not count:
        return _c("Market reachable", "fail", "Tonnel returned no collections", f"python -m {d.pkg} login")
    return _c(f"Market reachable ({ms:.0f} ms)", "ok", what)


async def check_clock(d: DoctorDeps) -> Check:
    """Runs after the reachability check (see run_doctor), so it reads the Date header of that fresh answer."""
    e = d.engine
    server = getattr(e.client, "last_server_date", None) if e else None
    local = getattr(e.client, "last_response_at", None) if e else None
    if server is None or local is None:
        return _c("Clock", "warn", "not checked: no market Date header seen",
                  "Make sure the market is reachable, enable NTP (timedatectl set-ntp true), then /doctor again")
    skew = local - server
    if abs(skew) > 30:
        return _c("Clock", "warn", f"skew {skew:+.0f} s against the market", "Enable NTP (timedatectl set-ntp true)")
    return _c("Clock", "ok", f"skew {skew:+.0f} s")


async def check_engine(d: DoctorDeps) -> Check:
    e = d.engine
    if d.cli:  # this process never runs the loop: report the service instead of its own idle engine
        if d.service_running:
            return _c("Engine", "ok", "the bot is running in another process (run.lock held); /doctor in the "
                                      "bot shows its cycles")
        return _c("Engine", "warn", "the bot is not running", f"Start it: python -m {d.pkg} run (or the service)")
    if e is None:
        return _c("Engine", "warn", "not running", "")
    last = e.last_ok_at
    limit = 3 * max(d.settings.POLL_INTERVAL, 1.0)
    errs = e.consecutive_errors
    if await e.is_paused():
        return _c("Engine", "warn", "paused: no buying until resumed", "/resume")
    if last is None:
        return _c("Engine", "warn", f"no successful cycle yet, {plural(errs, 'error')} in a row",
                  "Wait a few seconds, then check the log" if not errs else str(e.last_error))
    age = d.now - last
    if age > limit or errs:
        return _c("Engine", "warn", f"last good cycle {age:.0f}s ago, {plural(errs, 'error')} in a row",
                  e.last_error or "Check the log")
    return _c("Engine", "ok", f"last good cycle {age:.0f}s ago")


async def check_rules(d: DoctorDeps) -> Check:
    e = d.engine
    if e is None:
        return _c("Rules", "warn", "storage not open", "")
    rules = await e.storage.list_rules(enabled_only=True)
    if not rules:
        return _c("Rules", "warn", "no active rules", "/hunt or /addrule")
    known = set(e._floors.collections) if e._floors else set()
    unknown = [r.collection for r in rules if known and norm(r.collection) not in known]
    if unknown:
        return _c("Rules", "warn", f"{len(rules)} active, unknown collections: {', '.join(sorted(set(unknown)))}",
                  "Check the names with /floor or /hunt")
    return _c("Rules", "ok", f"{len(rules)} active")


async def check_fees(d: DoctorDeps) -> Check:
    s = d.settings
    text = f"buy {s.BUY_FEE_PCT:g}% · sell {s.SELL_FEE_PCT:g}% · withdraw {s.WITHDRAW_FEE_TON:g} TON"
    if s.FEES_VERIFIED:
        return _c("Fees", "ok", text)
    return _c("Fees are assumptions", "warn", text, "Check Tonnel's fee terms, then set FEES_VERIFIED=true")


async def check_peers(d: DoctorDeps) -> Check:
    if d.peers_status is None:
        return _c("Peers / Telegram value", "ok", "off")
    lines = await d.peers_status()
    bad = [l for l in lines if any(w in l for w in ("stale", "missing", "unreadable", "FloodWait"))]
    text = "; ".join(lines)
    return _c("Peers / Telegram value", "warn" if bad else "ok", text, "See /peers" if bad else "")


async def check_mode(d: DoctorDeps) -> Check:
    e = d.engine
    if e is None:
        return _c("Mode", "warn", "unknown", "")
    dry = await e.is_dry(d.now)
    until = await e.live_until()
    if await e.is_panic():
        return _c("Mode", "warn", "STOPPED by /panic", "/resume, then /dry off")
    if e.live_locked:
        return _c("Mode", "warn", "PRACTICE (DRY_RUN=true in .env)", "Set DRY_RUN=false to allow /dry off")
    if not dry and until:
        return _c("Mode", "ok", f"LIVE for {(until - d.now) / 3600:.1f} h more")
    return _c("Mode", "ok", "PRACTICE (/dry off shows the report; its confirm step arms LIVE)")


async def check_db(d: DoctorDeps) -> Check:
    e = d.engine
    if e is None:
        return _c("Database", "warn", "not open", "")
    try:
        await e.storage.set_setting("doctor_probe", d.now)
        await e.storage.delete_setting("doctor_probe")
    except Exception as exc:
        return _c("Database", "fail", f"not writable ({type(exc).__name__})", f"Check permissions of {d.settings.DB_PATH}")
    return _c("Database", "ok", "writable")


async def check_limits(d: DoctorDeps) -> Check:
    s = d.settings
    if s.MAX_PRICE_TON > s.DAILY_BUDGET_TON:
        return _c("Limits", "fail", f"MAX_PRICE_TON {s.MAX_PRICE_TON:g} > DAILY_BUDGET_TON {s.DAILY_BUDGET_TON:g}",
                  "Lower MAX_PRICE_TON or raise DAILY_BUDGET_TON")
    e = d.engine
    if e is not None and not await e.is_dry(d.now):
        try:
            balance = await e.client.balance()
        except Exception:
            balance = None
        if balance is not None and balance > s.HOT_WALLET_WARN_X * s.DAILY_BUDGET_TON:
            return _c("Limits", "warn", f"balance {balance:.1f} TON is over {s.HOT_WALLET_WARN_X:g}x the daily budget",
                      "Keep only what the bot may spend on the market balance")
        if balance is not None and balance < s.DAILY_BUDGET_TON:
            return _c("Limits", "warn", f"balance {balance:.1f} TON < DAILY_BUDGET_TON {s.DAILY_BUDGET_TON:g}", "")
    return _c("Limits", "ok", f"max {s.MAX_PRICE_TON:g} TON/gift, {s.DAILY_BUDGET_TON:g} TON/day (UTC), "
                              f"{s.MAX_BUYS_PER_HOUR}/h")


CHECKS: list[tuple[str, Callable[[DoctorDeps], Awaitable[Check]]]] = [
    ("Bot config", check_bot),
    ("Telegram session", check_session),
    ("Two-step verification", check_2fa),
    ("Session file permissions", check_session_mode),
    ("Market auth", check_market_auth),
    ("Market reachable", check_reachable),
    ("Clock", check_clock),
    ("Engine", check_engine),
    ("Rules", check_rules),
    ("Fees", check_fees),
    ("Peers / Telegram value", check_peers),
    ("Mode", check_mode),
    ("Database", check_db),
    ("Limits", check_limits),
]


async def _guard(name: str, fn: Callable[[DoctorDeps], Awaitable[Check]], d: DoctorDeps) -> Check:
    try:
        return await asyncio.wait_for(fn(d), CHECK_TIMEOUT)
    except TimeoutError:
        return _c(name, "fail", f"no answer within {CHECK_TIMEOUT:g} s", "Check the network / Telegram session")
    except Exception as exc:  # a broken check must never crash /doctor
        return _c(name, "fail", f"check crashed ({type(exc).__name__})", "See the log")


# Checks that read what another check fetched: they start only after that check has finished.
RUN_AFTER = {"Clock": "Market reachable"}


async def run_doctor(d: DoctorDeps) -> list[Check]:
    """Every check concurrently, except the ones in RUN_AFTER, which wait for their source check (the clock
    needs the Date header of the reachability request). Still well under 10 s: one 5 s timeout each."""
    tasks: dict[str, asyncio.Task] = {}

    async def run(name: str, fn: Callable[[DoctorDeps], Awaitable[Check]]) -> Check:
        before = RUN_AFTER.get(name)
        if before in tasks:
            await asyncio.wait({tasks[before]})
        return await _guard(name, fn, d)

    for name, fn in CHECKS:
        tasks[name] = asyncio.create_task(run(name, fn))
    return [await tasks[name] for name, _ in CHECKS]


def format_doctor(checks: list[Check]) -> str:
    counts = {k: sum(1 for c in checks if c.status == k) for k in ("ok", "warn", "fail")}
    head = f"Doctor: {counts['ok']} {WORD['ok']} · {counts['warn']} {WORD['warn']} · {counts['fail']} {WORD['fail']}"
    return "\n".join([head] + [c.line() for c in checks])


PROBE_TTL_SEC = 30.0  # one /doctor run shares a probe; the next /doctor asks Telegram again


def make_tg_probe(settings: "Settings", session: Any = None) -> TgProbe | None:
    """Real probe through Telethon, cached for PROBE_TTL_SEC (one doctor run), so a long-running bot shows
    a fresh 2FA/authorization state on every /doctor. None when Telethon is not configured."""
    if not settings.has_tg_credentials:
        return None
    cache: dict[str, tuple[float, tuple[bool, bool | None]]] = {}
    lock = asyncio.Lock()

    async def probe() -> tuple[bool, bool | None]:
        async with lock:
            hit = cache.get("r")
            if hit is None or time.monotonic() - hit[0] > PROBE_TTL_SEC:
                from .tg_auth import probe_session

                result = await probe_session(settings, session, timeout=CHECK_TIMEOUT - 0.5)
                cache["r"] = (time.monotonic(), result)
            return cache["r"][1]

    return probe
