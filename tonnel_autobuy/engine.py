"""Polling engine: fetch -> dedupe -> robust floor -> net deal math -> match -> card -> (practice) buy.

Safety: every spend (auto-buy or tap, practice or live) goes through Engine._spend, which holds one
lock while it re-reads every switch (panic, pause, practice, rule pauses), checks the limits on all-in
cost (MAX_PRICE_TON, DAILY_BUDGET_TON per UTC day, MAX_BUYS_PER_HOUR) against fresh totals, buys and
records the purchase, so concurrent taps and the engine loop can never jointly exceed a limit. Live buys
re-read the switches once more right before client.buy(), after the balance call. DRY_RUN=true in .env
is a hard lock no command can lift; LIVE is armed for LIVE_ARM_HOURS only; /panic stops everything
until /resume + /dry off.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import secrets
import time
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from .client import AuthError, MarketClient, RateLimited, SearchFilters
from .config import ConfigError, Settings
from .events import BOT_NAME, Event, EventLog
from .explain import explain_more, llm_available, severity, suite_code, why_text
from .models import BuyResult, FloorTable, Listing, norm, plural
from .notifier import (
    DealView,
    Notifier,
    fill_status,
    format_buy_failed,
    format_buy_ok,
    format_skip,
    render_deal_card,
    render_near_miss,
)
from .peers import Peers, PeerView, best_exit, build_snapshot, guards, write_snapshot
from .pricing import (
    DealMath,
    FeeModel,
    FloorEstimate,
    assess,
    classify,
    estimate_floor,
    fee_model_from_settings,
    too_good_flag,
)
from .rules import BuyRule, MatchResult, match, near_miss, stricter_mode
from . import shadow
from .safety import SafetyState, balance_mismatch, floor_crash, losing_streak
from .storage import PendingCard, Storage, utc_day_start

log = logging.getLogger(__name__)

DAY = 86400.0
HOUR = 3600.0
MARKET = "tonnel"
MARKET_TITLE = "Tonnel"
MUTE_SEC = 24 * HOUR
GATE_TTL_SEC = 600.0  # a [Go live for 24 h] button works once, within 10 min of its /dry off report
RESUME_TTL_SEC = 600.0  # [Confirm resume] / a breaker's [Resume]: once, within 10 min, until the next stop
GATE_SLACK_SEC = 300.0  # a stored live_until further ahead than LIVE_ARM_HOURS plus this is not a valid gate


# ---------------------------------------------------------------- pure helpers


@dataclass(slots=True, frozen=True)
class Limits:
    max_price_ton: float
    daily_budget_ton: float
    max_buys_per_hour: int

    @classmethod
    def from_settings(cls, s: Settings) -> "Limits":
        return cls(s.MAX_PRICE_TON, s.DAILY_BUDGET_TON, s.MAX_BUYS_PER_HOUR)


def check_limits_detail(cost: float, *, max_price: float, daily_budget: float, spent_today: float,
                        max_per_hour: int, buys_last_hour: int) -> tuple[str, str] | None:
    """(reason code, text) when a buy costing `cost` all-in must be blocked, else None. Fails closed: a cost
    that is not a positive finite number (NaN passes every `>` check) is blocked."""
    if not math.isfinite(cost) or cost <= 0:
        return "BAD_PRICE", f"cost {cost!r} is not a usable amount"
    if cost > max_price + 1e-9:
        return "LIMIT_MAX_PRICE", f"cost {cost:.2f} > MAX_PRICE_TON {max_price:g}"
    if spent_today + cost > daily_budget + 1e-9:
        return "LIMIT_DAILY_BUDGET", f"daily budget: spent {spent_today:.2f} + {cost:.2f} > {daily_budget:g}"
    if buys_last_hour >= max_per_hour:
        return "LIMIT_HOURLY", f"hourly limit: {buys_last_hour} >= MAX_BUYS_PER_HOUR {max_per_hour}"
    return None


def check_limits(cost: float, *, max_price: float, daily_budget: float, spent_today: float,
                 max_per_hour: int, buys_last_hour: int) -> str | None:
    """Return a skip reason, or None when the buy is within all hard limits. Pass the all-in cost."""
    hit = check_limits_detail(cost, max_price=max_price, daily_budget=daily_budget, spent_today=spent_today,
                              max_per_hour=max_per_hour, buys_last_hour=buys_last_hour)
    return hit[1] if hit else None


def backoff_delay(errors: int, base: float, cap: float, jitter: bool = True) -> float:
    if errors <= 0:
        return base
    delay = min(cap, max(base, 1.0) * (2 ** min(errors, 10)))
    if jitter:
        delay *= random.uniform(0.8, 1.2)
    return min(delay, cap)


def group_rules(rules: list[BuyRule]) -> dict[tuple, list[BuyRule]]:
    groups: dict[tuple, list[BuyRule]] = {}
    for r in rules:
        groups.setdefault(r.group_key(), []).append(r)
    return groups


def mute_keys(collection: str, model: str | None) -> list[str]:
    keys = [norm(collection)]
    if model:
        keys.append(f"{norm(collection)}:{norm(model)}")
    return keys


def is_floor_pool_member(listing: Listing, now: float, asset: str = "TON", export_locks: bool = True) -> bool:
    """Traps and locked gifts are not comparables: they would drag the floor down."""
    return not any(f.blocking or f.code == "LOCKED"
                   for f in classify(listing, MARKET, now, asset=asset, export_locks=export_locks))


def api_floor_for(floors: FloorTable | None, rule: BuyRule) -> float | None:
    """filterStats floor that fits the rule: collection floor for collection-only rules, the model floor
    for rules pinning only a model (Tonnel reports model floors), none when backdrop/symbol are pinned."""
    if floors is None:
        return None
    if rule.backdrop or rule.symbol:
        return None
    if rule.model:
        return floors.models.get((norm(rule.collection), norm(rule.model)))
    return floors.collections.get(norm(rule.collection))


# ---------------------------------------------------------------- engine


@dataclass
class Decision:
    listing: Listing
    floor: float | None
    rule: BuyRule | None
    result: MatchResult


@dataclass
class CycleReport:
    started: float = field(default_factory=time.time)
    paused: bool = False
    rules: int = 0
    fetched: int = 0
    new: int = 0
    matched: list[Decision] = field(default_factory=list)
    rejected: list[Decision] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    bought: int = 0
    dry_bought: int = 0
    skipped: int = 0
    failed: int = 0
    alerts: int = 0
    rejections: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)

    @property
    def matches(self) -> int:
        return len(self.matched)

    def summary(self) -> str:
        if self.paused:
            return "paused"
        return (f"rules={self.rules} fetched={self.fetched} new={self.new} "
                f"matched={len(self.matched)} rejected={len(self.rejected)} "
                f"dry_buys={self.dry_bought} buys={self.bought} cards={self.alerts} "
                f"skipped={self.skipped} failed={self.failed}")

    def top_reasons(self, n: int = 3) -> str:
        return " · ".join(f"{k} {v}" for k, v in self.reasons.most_common(n)) or "none"


@dataclass(slots=True)
class TapResult:
    ok: bool
    text: str
    status: str | None = None
    reply: str | None = None  # longer text for a chat reply (Why?, Explain more)


@dataclass(slots=True)
class Spend:
    """What Engine._spend did with one buy attempt.
    kind: refused (a switch or breaker said no) | blocked (a limit) | practice | bought | failed | unknown."""

    kind: str
    code: str = "OK"
    text: str = ""
    practice: bool = False
    elapsed: float = 0.0


class Engine:
    def __init__(self, settings: Settings, client: MarketClient, storage: Storage,
                 notifier: Notifier, *, force_dry: bool = False, clock: Callable[[], float] = time.time,
                 peers: Peers | None = None, events: EventLog | None = None,
                 snapshot_path: str | Path | None = None):
        self.s = settings
        self.client = client
        self.storage = storage
        self.notifier = notifier
        self.force_dry = force_dry
        self.clock = clock
        self.peers = peers
        self.events = events
        self.fees = fee_model_from_settings(settings)
        # The CLI passes DATA_DIR/floors_tonnel.json; engines built in tests write no snapshot file.
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        try:
            self.admin_ids: set[int] = set(settings.admin_ids)
        except ConfigError:
            self.admin_ids = set()
        self._floors: FloorTable | None = None
        self._floors_ts = 0.0
        self.cycles = 0
        self.last_cycle_ts: float | None = None
        self.last_ok_at: float | None = None
        self.last_error: str | None = None
        self.consecutive_errors = 0
        self.api_errors = 0
        self.current_delay = settings.POLL_INTERVAL
        self._bg: set[asyncio.Task] = set()
        self._snap_written: dict[str, float] = {}
        self._near_times: deque[float] = deque()
        self._last_snapshot_file = -1e18
        self.consecutive_buy_errors = 0
        # One lock around switch re-check + limit check + buy + record, for the loop and every tap alike.
        self._spend_lock = asyncio.Lock()
        self._closing = False  # set by drain(): no new spend starts, the one in flight finishes
        self._peers_deadline: float | None = None  # monotonic; one PEERS_TIMEOUT_SEC budget per cycle
        self._rate_task: asyncio.Task | None = None
        self._stale_warned = False
        self.last_mark_at = -1e18
        self.last_tick_at = -1e18

    @property
    def settings(self) -> Settings:
        return self.s

    # ------------------------------------------------------------ runtime switches

    async def is_paused(self) -> bool:
        return await self.storage.is_paused()

    async def set_paused(self, value: bool) -> None:
        await self.storage.set_paused(value)
        if value:  # a new stop (/pause, /panic, a breaker) voids every older [Confirm resume] / [Resume] button
            await self.storage.delete_setting("resume_nonce")

    @property
    def live_locked(self) -> bool:
        """DRY_RUN=true in .env (or mock mode) is a hard lock: no command or stored setting can enable live."""
        return self.force_dry or self.s.DRY_RUN

    async def is_panic(self) -> bool:
        return await self.storage.is_panic()  # kv.panic, persisted across restarts

    async def live_until(self) -> float | None:
        return await self.storage.get_float("live_until")

    async def is_dry(self, now: float | None = None) -> bool:
        """Practice unless: no .env lock, no panic, the stored dry flag is off AND the LIVE gate is armed
        with a valid live_until in the future.

        Fails closed: a missing or unreadable live_until, a non-finite one, or one further ahead than
        LIVE_ARM_HOURS (a clock jump, a hand-edited row) is practice, and so is a missing `dry` flag. An
        Engine on a fresh database with DRY_RUN=false is therefore in practice until arm_live().

        The .env lock cancels a stored live window instead of only hiding it, so after DRY_RUN=false is set
        again LIVE still needs a new /dry off."""
        if self.live_locked:
            if await self.live_until() is not None or await self.storage.get_setting("gate_nonce"):
                await self._void_live_window()
            return True
        if await self.is_panic():
            return True
        now = self.clock() if now is None else now
        until = await self.live_until()
        if until is None:  # LIVE only ever exists as an armed, expiring gate (arm_live)
            return True
        if not math.isfinite(until) or until - now > self.s.LIVE_ARM_HOURS * HOUR + GATE_SLACK_SEC:
            log.warning("live_until %r is not a valid live gate: back to practice", until)
            await self._end_live(now, "Live mode ended: the stored live gate was not valid")
            return True
        if now > until:
            await self._end_live(now)
            return True
        return await self.storage.get_dry()

    async def _void_live_window(self) -> None:
        """Practice on; the armed LIVE window and any unused [Go live] button are gone for good."""
        await self.storage.set_dry(True)
        await self.storage.delete_setting("live_until")
        await self.storage.delete_setting("gate_nonce")

    async def _end_live(self, now: float, why: str | None = None) -> None:
        await self.storage.set_dry(True)
        await self.storage.delete_setting("live_until")
        hours = self.s.LIVE_ARM_HOURS
        await self.notifier.send((why or f"Live mode ended after {hours:g} h") +
                                 "; back to practice. /dry off to renew.")
        self._emit_state("pause", now, "live gate invalid" if why else "live gate expired")

    async def set_dry(self, value: bool) -> str | None:
        """Returns an error message when the change is not allowed. Going live always arms the gate."""
        if value:
            await self._void_live_window()
            return None
        return await self.arm_live()

    async def _issue_nonce(self, key: str, ttl: float, now: float | None = None) -> str:
        now = self.clock() if now is None else now
        nonce = secrets.token_urlsafe(6)
        await self.storage.set_setting(key, f"{nonce}:{now + ttl}")
        return nonce

    async def _consume_nonce(self, key: str, nonce: str, now: float | None = None) -> bool:
        """True once for the newest, unexpired token stored under `key`."""
        now = self.clock() if now is None else now
        stored = await self.storage.get_setting(key)
        if not stored or not nonce:
            return False
        value, _, until = stored.rpartition(":")
        if value != nonce or not await self.storage.take_setting(key, stored):
            return False
        try:
            return now <= float(until)
        except ValueError:
            return False

    async def issue_gate_nonce(self, now: float | None = None) -> str:
        """One-time token for the [Go live for 24 h] button of the /dry off report just sent."""
        return await self._issue_nonce("gate_nonce", GATE_TTL_SEC, now)

    async def consume_gate_nonce(self, nonce: str, now: float | None = None) -> bool:
        """True once for the newest, unexpired go-live button; /panic, /dry on, the losing-streak breaker
        and DRY_RUN=true void it."""
        return await self._consume_nonce("gate_nonce", nonce, now)

    async def issue_resume_nonce(self, now: float | None = None) -> str:
        """One-time token for a [Confirm resume] (/resume) or a breaker's [Resume] button. Issue it after
        the stop it belongs to: any later stop voids it (set_paused)."""
        return await self._issue_nonce("resume_nonce", RESUME_TTL_SEC, now)

    async def resume_with(self, nonce: str, now: float | None = None) -> bool:
        """A resume button: resumes only for the newest, unexpired one. A later /pause, /panic or breaker
        voided it, so an old button can never undo a newer stop."""
        now = self.clock() if now is None else now
        if not await self._consume_nonce("resume_nonce", nonce, now):
            return False
        await self.resume(now)
        return True

    async def arm_live(self, now: float | None = None) -> str | None:
        if self.live_locked:
            return "Live mode is locked: DRY_RUN=true in .env. Set DRY_RUN=false and restart to allow /dry off."
        if await self.is_panic():
            return "Everything is stopped by /panic. Use /resume first, then /dry off."
        now = self.clock() if now is None else now
        # The gate expiry is written first: if the second write fails, the bot stays in practice.
        await self.storage.set_setting("live_until", now + self.s.LIVE_ARM_HOURS * HOUR)
        await self.storage.set_dry(False)
        # Buy buttons on cards rendered as PRACTICE must never spend real TON: they stop working now.
        await self._expire_practice_cards("Expired: live mode started, this was a practice card")
        return None

    async def _expire_practice_cards(self, line: str) -> int:
        """Expire every pending card that was rendered (and labelled) PRACTICE. A card without the flag
        (older payload) is treated as practice."""
        n = 0
        for card in await self.storage.list_pending(("pending",)):
            if not card.payload.get("practice", True):
                continue
            if await self.storage.set_pending_status(card.token, "expired", only_from=("pending",)):
                await self._edit_card(card, line)
                n += 1
        return n

    async def start_in_practice(self, now: float | None = None) -> None:
        """Process start: LIVE only survives a restart while its 24 h gate is still open and DRY_RUN=false.
        A start with DRY_RUN=true cancels the window for good: going live again needs a new /dry off."""
        now = self.clock() if now is None else now
        until = await self.live_until()
        if self.live_locked or until is None or now > until:
            await self._void_live_window()

    async def panic(self, now: float | None = None) -> int:
        """Stop all buying at once: pause, practice on, live gate cleared, pending cards expired.
        Taps already executing are flagged too, so they refuse at their last check before buy()."""
        now = self.clock() if now is None else now
        await self.storage.set_panic(True)
        await self.set_paused(True)  # also voids every older resume button
        await self._void_live_window()
        await self.storage.cancel_executing()
        cards = await self.storage.expire_pending(None)
        for card in cards:
            await self._edit_card(card, "Expired")
        await self.notifier.send("Everything stopped. Practice mode is forced until /resume (with confirm) "
                                 "and a new /dry off.")
        self._emit_state("pause", now, "panic")
        return len(cards)

    async def resume(self, now: float | None = None) -> None:
        """Confirmed /resume: clears panic and pause. Mode stays practice until /dry off."""
        now = self.clock() if now is None else now
        await self.storage.set_panic(False)
        await self.set_paused(False)
        self._emit_state("resume", now, "resume")

    async def effective_limits(self) -> Limits:
        """.env limits, tightened by a /preset if one is stored (a preset can never raise them)."""
        overrides = await self.storage.settings_with_prefix("limit:")

        def val(key: str, env: float) -> float:
            try:
                return min(env, float(overrides[f"limit:{key}"])) if f"limit:{key}" in overrides else env
            except ValueError:
                return env

        return Limits(
            val("MAX_PRICE_TON", self.s.MAX_PRICE_TON),
            val("DAILY_BUDGET_TON", self.s.DAILY_BUDGET_TON),
            int(val("MAX_BUYS_PER_HOUR", self.s.MAX_BUYS_PER_HOUR)),
        )

    async def spent_today(self, now: float, dry: bool) -> float:
        return await self.storage.spent_since(utc_day_start(now), dry)

    async def _limits_left(self, now: float, practice: bool) -> tuple[float, int]:
        """(TON left today, buys left this hour) for the practice or the live ledger."""
        limits = await self.effective_limits()
        spent = await self.spent_today(now, practice)
        buys = await self.storage.count_since(now - HOUR, practice)
        return max(0.0, limits.daily_budget_ton - spent), max(0, limits.max_buys_per_hour - buys)

    async def safety_state(self, now: float | None = None) -> SafetyState:
        now = self.clock() if now is None else now
        dry = await self.is_dry(now)
        limits = await self.effective_limits()
        return SafetyState(
            live=not dry,
            locked=self.live_locked,
            live_until=await self.live_until(),
            panic=await self.is_panic(),
            paused=await self.is_paused(),
            spent_today=await self.spent_today(now, dry),
            buys_last_hour=await self.storage.count_since(now - HOUR, dry),
            max_price=limits.max_price_ton,
            daily_budget=limits.daily_budget_ton,
            max_per_hour=limits.max_buys_per_hour,
        )

    # ------------------------------------------------------------ fees and floors

    def fees_for(self, collection: str) -> FeeModel:
        """Tonnel has one fee schedule for every collection (unlike MRKT, nothing is learned per collection)."""
        return self.fees

    async def api_floors(self, force: bool = False) -> FloorTable | None:
        if not self.client.has_auth:
            return None
        if force or self._floors is None or time.time() - self._floors_ts > self.s.FLOOR_CACHE_TTL:
            try:
                self._floors = await self.client.collection_floors()
                self._floors_ts = time.time()
            except ConfigError:
                raise
            except Exception as exc:  # noqa: BLE001 - floors are optional, keep the old cache
                log.warning("collection floors unavailable: %s", exc)
                self._floors_ts = time.time()  # do not retry on every cycle
        return self._floors

    def collection_names(self) -> dict[str, str]:
        return dict(self._floors.display) if self._floors is not None else {}

    def _classify(self, listing: Listing, now: float):
        return classify(listing, MARKET, now, asset=self.s.TONNEL_ASSET, export_locks=self.s.EXPORT_AT_LOCKS)

    # ------------------------------------------------------------ one cycle

    async def run_cycle(self) -> CycleReport:
        report = CycleReport()
        now = self.clock()
        if await self.is_paused():
            report.paused = True
            return report
        rules = [r for r in await self.storage.list_rules(enabled_only=True) if r.state != "paused"]
        preset_min = await self.storage.get_float("limit:MIN_DISCOUNT_PCT")
        if preset_min:  # a /preset sets a global minimum discount on top of every rule
            rules = [replace(r, min_discount_pct=max(r.min_discount_pct, preset_min)) for r in rules]
        report.rules = len(rules)
        if not rules:
            self._cycle_done(now)
            return report
        floors = await self.api_floors()

        # Same requests as the scaffold: the newest and the cheapest page per rule group.
        cheapest: dict[tuple, list[Listing]] = {}
        candidates: dict[str, Listing] = {}
        for key, group in group_rules(rules).items():
            base = group[0]
            f = dict(collection=base.collection, model=base.model, backdrop=base.backdrop,
                     symbol=base.symbol, limit=self.s.PAGE_LIMIT)
            newest, cheap = await asyncio.gather(
                self.client.search_listings(SearchFilters(sort="latest", **f)),
                self.client.search_listings(SearchFilters(sort="price_asc", **f)),
            )
            cheapest[key] = cheap
            for x in [*cheap, *newest]:
                candidates.setdefault(x.key, x)
        report.fetched = len(candidates)
        everything = list(candidates.values())
        for pool in cheapest.values():
            everything.extend(pool)
        await self.storage.upsert_observations(everything, now)  # no extra requests: data we already have
        pools = {k: [x for x in v if is_floor_pool_member(x, now, self.s.TONNEL_ASSET, self.s.EXPORT_AT_LOCKS)]
                 for k, v in cheapest.items()}
        snaps = await self._snapshot_floors(rules, pools, floors, now)
        paused_rules = await self._crash_breaker(rules, snaps, now)
        await self._write_own_snapshot(floors, pools, now)
        cycle_mode = "dry" if await self.is_dry(now) else "live"  # for events.jsonl
        self._refresh_rate()

        # Phase 1: evaluate every new listing (pure math, decisions logged); phase 2: act on the matches,
        # best deals first (largest discount, then cheapest).
        matches: list[tuple[Listing, BuyRule, DealMath, MatchResult]] = []
        for listing in sorted(candidates.values(), key=lambda x: x.price_ton):
            if not await self.storage.mark_seen_one(listing):
                continue
            report.new += 1
            hit = await self._evaluate(listing, rules, pools, floors, snaps, paused_rules, report, now, cycle_mode)
            if hit is not None:
                matches.append(hit)
        matches.sort(key=lambda m: (-(m[3].discount_pct or 0), m[0].price_ton))
        self._peers_deadline = time.monotonic() + self.s.PEERS_TIMEOUT_SEC
        try:
            for i, (listing, rule, deal, result) in enumerate(matches):
                if await self.is_paused():  # /pause or a breaker fired mid-cycle: the rest wait for /resume
                    log.info("paused mid-cycle: %d match(es) wait for /resume", len(matches) - i)
                    for rest in matches[i:]:
                        await self.storage.unmark_seen(rest[0])
                    break
                report.matched.append(Decision(listing, deal.floor, rule, result))
                action = await self._handle_match(rule, listing, deal, report, now)
                if action:
                    report.actions.append(action)
        finally:
            self._peers_deadline = None
        self._cycle_done(now)
        if self.cycles % 1000 == 0:
            await self.storage.prune_seen()
        return report

    def _cycle_done(self, now: float) -> None:
        self.cycles += 1
        self.last_cycle_ts = now
        self.last_ok_at = now

    async def _snapshot_floors(self, rules: list[BuyRule], pools: dict[tuple, list[Listing]],
                               floors: FloorTable | None, now: float) -> dict[str, list[tuple[float, float]]]:
        """Write one floor_snapshots row per rule filter key at most once per FLOORS_TTL.
        Returns the last hour of snapshots per key (for the jump downgrade and the crash breaker)."""
        out: dict[str, list[tuple[float, float]]] = {}
        for rule in rules:
            key = rule.filter_key_str()
            if key in out:
                continue
            last = self._snap_written.get(key)
            if last is None:
                snap = await self.storage.latest_snapshot(key)
                last = snap.ts if snap else -1e18
            if now - last >= self.s.floors_ttl:
                est = estimate_floor(None, pools.get(rule.group_key(), []), api_floor_for(floors, rule), [], now,
                                     k=self.s.CLUSTER_K, band=self.s.CLUSTER_BAND_PCT)
                if est.value is not None:
                    await self.storage.add_snapshot(now, key, est.value, est.n, est.source)
                    self._snap_written[key] = now
            out[key] = [(s.ts, s.floor_ton) for s in await self.storage.snapshots(key, now - HOUR) if s.floor_ton]
        return out

    async def _crash_breaker(self, rules: list[BuyRule], snaps: dict[str, list[tuple[float, float]]],
                             now: float) -> set[int]:
        """Pause a rule whose floor fell more than CRASH_PCT within 1 h. Returns the paused rule ids."""
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import RuleCB

        paused: set[int] = set()
        for rule in rules:
            if rule.id is None:
                continue
            until = await self.storage.get_float(f"rule_paused_until:{rule.id}")
            if until is not None and until > now:
                paused.add(rule.id)
                continue
            drop = floor_crash(snaps.get(rule.filter_key_str(), []), now, self.s.CRASH_PCT)
            if drop is None:
                continue
            last_alert = await self.storage.get_float(f"crash_alert:{rule.id}")
            if last_alert is not None and now - last_alert < HOUR:
                continue
            minutes = self.s.CRASH_PAUSE_MIN
            await self.storage.set_setting(f"rule_paused_until:{rule.id}", now + minutes * 60)
            await self.storage.set_setting(f"crash_alert:{rule.id}", now)
            paused.add(rule.id)
            await self._expire_cards("Expired: rule paused by the floor-crash breaker", rule_id=rule.id)
            kb = InlineKeyboardBuilder()
            kb.button(text="Resume", callback_data=RuleCB(action="resume", rule_id=rule.id, t=int(now)))
            kb.button(text="Keep paused", callback_data=RuleCB(action="keep", rule_id=rule.id, t=int(now)))
            await self.notifier.send(f"Rule #{rule.id} paused for {minutes} min: floor −{drop:.0f}% in 1 h",
                                     keyboard=kb.as_markup())
            self._emit_state("pause", now, f"rule #{rule.id} floor crash -{drop:.0f}%")
        return paused

    def snapshot_data(self, floors: FloorTable | None, pools: dict[tuple, list[Listing]]) -> dict[str, dict]:
        """{display name: {floor, n, models}} for floors_tonnel.json: filterStats floors when Tonnel auth is
        available, else the cheapest TON listing seen per collection; model floors from both."""
        data: dict[str, dict[str, Any]] = {}
        if floors is not None:
            for key, floor in floors.collections.items():
                name = floors.display.get(key, key)
                models = {m: f for (c, m), f in floors.models.items() if c == key}
                data[name] = {"floor": floor, "n": floors.counts.get(key, 0), "models": models}
        by_key = {norm(name): name for name in data}
        for listings in pools.values():
            for x in listings:
                name = by_key.setdefault(norm(x.collection), x.collection)
                entry = data.setdefault(name, {"floor": x.price_ton, "n": 0, "models": {}})
                if floors is None or norm(x.collection) not in floors.collections:
                    entry["floor"] = min(entry["floor"], x.price_ton)
                    entry["n"] += 1
                if x.model:
                    entry["models"][x.model] = min(x.price_ton, entry["models"].get(x.model, float("inf")))
        return data

    async def _write_own_snapshot(self, floors: FloorTable | None, pools: dict[tuple, list[Listing]],
                                  now: float) -> None:
        """floors_tonnel.json for other bots you run (rarity suffixes are stripped by the snapshot keys)."""
        if self.snapshot_path is None or now - self._last_snapshot_file < self.s.floors_ttl:
            return
        try:
            write_snapshot(self.snapshot_path, build_snapshot(MARKET, self.fees, self.snapshot_data(floors, pools), now))
            self._last_snapshot_file = now
            # Telegram's own resale quotes (TG_MARKET) go to floors_telegram.json, so other bots
            # (getgems-checker included) get them through their peer file list.
            tg_snap = self.peers.tg_market.snapshot(now) if self.peers and self.peers.tg_market else None
            if tg_snap is not None:
                write_snapshot(self.snapshot_path.with_name("floors_telegram.json"), tg_snap)
        except OSError as exc:
            log.warning("cannot write %s: %s", self.snapshot_path, exc)

    def _estimate(self, listing: Listing, rule: BuyRule, pools: dict[tuple, list[Listing]],
                  floors: FloorTable | None, snaps: dict[str, list[tuple[float, float]]],
                  now: float) -> FloorEstimate:
        s = self.s
        return estimate_floor(
            listing, pools.get(rule.group_key(), []), api_floor_for(floors, rule),
            snaps.get(rule.filter_key_str(), []), now,
            k=s.CLUSTER_K, band=s.CLUSTER_BAND_PCT, jump_pct=s.FLOOR_JUMP_PCT,
            min_comparables=s.MIN_COMPARABLES_ATTR if rule.pins_attributes else s.MIN_COMPARABLES_COLLECTION,
        )

    def _deal(self, listing: Listing, est: FloorEstimate, now: float) -> DealMath:
        flags = self._classify(listing, now)
        tg = too_good_flag(listing.price_ton, est.value, self.s.TOO_GOOD_PCT)
        if tg:
            flags.append(tg)
        return assess(listing, est, self.fees_for(listing.collection), flags)

    async def _evaluate(self, listing: Listing, rules: list[BuyRule], pools: dict[tuple, list[Listing]],
                        floors: FloorTable | None, snaps: dict[str, list[tuple[float, float]]],
                        paused_rules: set[int], report: CycleReport, now: float, cycle_mode: str = "dry",
                        ) -> tuple[Listing, BuyRule, DealMath, MatchResult] | None:
        """Every rule of the listing's collection, each with its own comparable pool. The first match wins;
        otherwise the most telling rejection is logged (decisions, /why) and may trigger a near-miss card."""
        rejection: tuple[BuyRule, MatchResult] | None = None
        for rule in rules:
            if norm(rule.collection) != norm(listing.collection):
                continue
            deal = self._deal(listing, self._estimate(listing, rule, pools, floors, snaps, now), now)
            result = match(rule, listing, deal)
            if not result.ok:
                if rejection is None or (rejection[1].code == "MISMATCH" and result.code != "MISMATCH"):
                    rejection = (rule, result)
                continue
            if rule.id in paused_rules:
                rejection = (rule, MatchResult(False, "rule paused by the floor-crash breaker", deal.discount_pct,
                                               "RULE_PAUSED", deal))
                continue
            return listing, rule, deal, result
        if rejection is None:
            return None
        rule, result = rejection
        deal = result.deal
        report.rejected.append(Decision(listing, deal.floor if deal else None, rule, result))
        report.reasons[result.code] += 1
        report.rejections.append(
            f"skip #{listing.number if listing.number is not None else listing.id} {listing.price_ton:.2f} TON: "
            f"{result.reason} (rule #{rule.id})"
        )
        log.debug("reject %s by rule #%s: %s", listing.id, rule.id, result.reason)
        near = None
        if deal is not None and result.code in ("OVER_MAX", "LOW_DISCOUNT", "LOW_PROFIT"):
            near = near_miss(rule, listing, deal, pct=self.s.NEAR_MISS_PCT)
        ev_mode = "shadow" if cycle_mode == "live" and rule.state == "shadow" else cycle_mode
        await self._decide(now, listing, rule, "skip", result.code, deal, result.reason, mode=ev_mode,
                           extra={"near_miss": near.text() if near else None})
        if near is not None and deal is not None:
            await self._near_miss(rule, listing, deal, near, now)
        return None

    # ------------------------------------------------------------ decisions and events

    def _detail(self, listing: Listing, deal: DealMath | None, text: str, **extra: Any) -> dict[str, Any]:
        d: dict[str, Any] = {"text": text, "title": listing.short_title, "collection": listing.collection,
                             "model": listing.model, "number": listing.number}
        if deal is not None:
            d.update(floor=deal.floor, confidence=deal.confidence, n=deal.n, source=deal.floor_source,
                     cost=round(deal.cost, 6), discount=deal.discount_pct, note=deal.note,
                     profit_ton=None if deal.profit_ton is None else round(deal.profit_ton, 6))
        d.update({k: v for k, v in extra.items() if v is not None})
        return d

    async def _decide(self, now: float, listing: Listing, rule: BuyRule | None, outcome: str, code: str | None,
                      deal: DealMath | None, text: str, *, mode: str = "dry",
                      extra: dict[str, Any] | None = None) -> None:
        await self.storage.add_decision(now, listing.id, listing.price_ton, rule.id if rule else None, outcome, code,
                                        self._detail(listing, deal, text, **(extra or {})))
        kind = {"skip": "skip", "muted": "skip", "near": "skip", "alert": "alert", "dry": "buy", "bought": "buy",
                "failed": "error"}.get(outcome, "skip")
        verdict = {"skip": "SKIP", "muted": "SKIP", "alert": "WATCH", "dry": "BUY", "bought": "BUY"}.get(outcome)
        if code and code.startswith("TRAP"):
            verdict = "AVOID"
        codes = [code] if code else []
        if deal is not None and deal.discount_pct and deal.discount_pct > 0 and outcome in ("alert", "dry", "bought"):
            codes.insert(0, "BELOW_FLOOR")
        self._emit(kind, now, listing, deal, codes, rule, mode, verdict)

    def _emit(self, kind: str, now: float, listing: Listing, deal: DealMath | None, codes: list[str],
              rule: BuyRule | None, mode: str, verdict: str | None) -> None:
        if self.events is None:
            return
        reasons = []
        for c in codes:
            sc = suite_code(c)
            p: dict[str, Any] = {}
            if deal is not None:
                p = {k: round(v, 4) for k, v in (("cost", deal.cost), ("floor", deal.floor),
                                                 ("net", deal.profit_ton)) if v is not None}
            reasons.append({"code": sc, "sev": severity(sc), "p": p})
        money = None
        if deal is not None:
            fees = deal.cost - deal.price + ((deal.floor - deal.net_exit) if deal.floor and deal.net_exit else 0.0)
            rate = self._usd_rate()
            money = {"ccy": "TON", "price": deal.price, "ref_price": deal.floor, "fees": round(fees, 6),
                     "net_edge": deal.profit_ton, "usd": round(deal.cost * rate, 2) if rate else None}
        event = {
            "v": 1, "ts": now, "bot": BOT_NAME, "kind": kind, "mode": mode, "id": listing.key,
            "subject": {"type": "gift", "ref": listing.id, "title": listing.short_title, "url": listing.url},
            "money": money, "verdict": verdict, "score": None,
            "confidence": deal.confidence if deal is not None else None, "reasons": reasons,
            "rule_id": rule.id if rule else None, "tx": None,
        }
        try:
            self.events.append(Event.model_validate(event))
        except Exception as exc:  # noqa: BLE001 - a bad event must never break the loop
            log.debug("event dropped: %s", exc)

    def _emit_state(self, kind: str, now: float, text: str) -> None:
        if self.events is None:
            return
        try:
            self.events.append(Event.model_validate({
                "v": 1, "ts": now, "bot": BOT_NAME, "kind": kind, "mode": "dry", "id": f"{kind}:{now:.0f}",
                "subject": {"type": "market", "ref": MARKET, "title": text}, "reasons": [],
            }))
        except Exception as exc:  # noqa: BLE001
            log.debug("event dropped: %s", exc)

    # ------------------------------------------------------------ matches

    async def _peer_view(self, listing: Listing, rule: BuyRule, deal: DealMath) -> PeerView | None:
        if self.peers is None or not self.peers.enabled:
            return None
        timeout = None
        if self._peers_deadline is not None:  # inside run_cycle: all matches share one PEERS_TIMEOUT_SEC
            timeout = self._peers_deadline - time.monotonic()
        view = await self.peers.view(listing, rule, deal, timeout=timeout)
        return None if view.empty else view

    def _refresh_rate(self) -> None:
        """Refresh the TON->USD rate in the background (once per TTL); a buy never waits for it."""
        rate = self.peers.rate if self.peers is not None else None
        if rate is None or rate.fixed is not None or (self._rate_task is not None and not self._rate_task.done()):
            return
        self._rate_task = asyncio.create_task(rate.usd_per_ton(), name="ton-rate")

    def _usd_rate(self) -> float | None:
        if self.peers is None or self.peers.rate is None or self.s.DISPLAY_CURRENCY.lower() != "usd":
            return None
        return self.peers.rate.value  # cached; run_cycle refreshes it in the background

    async def _handle_match(self, rule: BuyRule, listing: Listing, deal: DealMath, report: CycleReport,
                            now: float) -> str | None:
        s = self.s
        downgrades: list[tuple[str, str]] = []
        too_good = deal.flag("TOO_GOOD")
        if too_good:
            downgrades.append(("TOO_GOOD", too_good.text))
        if deal.lock_days:
            downgrades.append(("LOCKED_ALLOWED", f"locked for {deal.lock_days:.0f} d"))
        peers = await self._peer_view(listing, rule, deal)
        downgrades += guards(peers, deal, cheaper_pct=s.CHEAPER_ELSEWHERE_PCT, tg_guard=s.TG_VALUE_GUARD)
        if deal.cost > s.confirm_above_ton:
            downgrades.append(("CONFIRM_ABOVE", f"buys above {s.confirm_above_ton:g} TON always ask first"))
        exit_best = None
        if rule.exit == "best" and peers is not None:
            exit_best = best_exit(MARKET, deal.net_exit, peers.quotes, deal.fees.withdraw_ton)
        for code, _ in downgrades:
            report.reasons[code] += 1
        mode = stricter_mode(rule.mode, "confirm" if downgrades else "auto")
        dry_global = await self.is_dry(now)
        shadow = rule.state == "shadow" and not dry_global
        practice = dry_global or shadow
        ev_mode = "dry" if dry_global else ("shadow" if shadow else "live")
        budget_left, buys_left = await self._limits_left(now, practice)
        view = DealView(
            listing=listing, deal=deal, rule=rule, mode=mode, status="", practice=practice,
            asking_first=[t for _, t in downgrades], peers=peers, best_exit=exit_best, usd_rate=self._usd_rate(),
            budget_left=budget_left, buys_left=buys_left,
            token=secrets.token_urlsafe(6), market_bot=s.TONNEL_BOT_USERNAME, market_title=MARKET_TITLE,
            market=MARKET, fees_verified=s.FEES_VERIFIED, llm=llm_available(s), now=now, shadow=shadow,
        )

        if mode != "auto":
            muted = await self.storage.muted_key(mute_keys(listing.collection, listing.model), now)
            if muted:
                report.reasons["MUTED"] += 1
                await self._decide(now, listing, rule, "muted", "MUTED", deal, f"muted ({muted})", mode=ev_mode)
                return f"MUTED ({muted}): {listing.short_title}"
            view.pending = mode == "confirm"
            view.status = (f"Waiting for your tap ({s.CONFIRM_TTL_SEC} s)" if mode == "confirm"
                           else "Alert only (watch mode): no Buy button")
            await self._send_card(view, status="pending" if mode == "confirm" else "watch", now=now,
                                  ttl=s.CONFIRM_TTL_SEC if mode == "confirm" else DAY)
            report.alerts += 1
            await self._decide(now, listing, rule, "alert", downgrades[0][0] if downgrades else "OK", deal,
                               "; ".join(t for _, t in downgrades) or f"{mode} mode: card sent", mode=ev_mode,
                               extra={"mode": mode})
            return f"CARD ({mode}): {listing.short_title}"

        # auto mode: one locked step re-reads the switches, checks the limits on all-in cost against fresh
        # totals and (practice) buys; then one card with the outcome
        spend = await self._spend(rule, listing, deal, now, practice=practice, detail="SHADOW_RULE" if shadow else "")
        if spend.practice and not practice:  # /dry on or /panic landed while we were looking: practice only
            practice, shadow, ev_mode = True, False, "dry"
            view.practice, view.shadow = True, False
        if spend.kind in ("practice", "bought", "failed", "unknown"):
            # The card reports the limits left after this buy, not the ones read before it.
            view.budget_left, view.buys_left = await self._limits_left(now, practice)
        if spend.kind == "refused":  # a switch, breaker or the balance said no; whoever refused has notified
            report.skipped += 1
            report.reasons[spend.code] += 1
            await self._decide(now, listing, rule, "skip", spend.code, deal, f"not bought: {spend.text}", mode=ev_mode)
            return f"NOT BOUGHT ({spend.text}): {listing.short_title}"
        if spend.kind == "blocked":
            report.skipped += 1
            report.reasons[spend.code] += 1
            view.status = f"Not bought: {spend.text}"
            await self._send_card(view, status="skipped", now=now, ttl=DAY)
            msg = format_skip(listing, spend.text)
            await self.notifier.send(msg, silent=True)
            await self._decide(now, listing, rule, "skip", spend.code, deal, spend.text, mode=ev_mode)
            return msg
        if spend.kind == "practice":
            report.dry_bought += 1
            view.status = "PRACTICE: would buy" + (" (shadow rule)" if shadow else "")
            await self._send_card(view, status="dry", now=now, ttl=DAY)
            await self._decide(now, listing, rule, "dry", "SHADOW_RULE" if shadow else "DRY_RUN", deal,
                               f"practice buy for {deal.cost:.2f} TON all-in", mode=ev_mode)
            return f"PRACTICE: would buy {listing.short_title} for {deal.cost:.2f} TON"
        if spend.kind == "bought":
            report.bought += 1
            view.status = f"BOUGHT in {spend.elapsed:.1f} s"
            await self._send_card(view, status="bought", now=now, ttl=DAY)
            msg = format_buy_ok(listing, deal.cost, deal.fees.buy_pct, spend.text)
            await self.notifier.send(msg)
            await self._decide(now, listing, rule, "bought", "OK", deal, f"bought for {deal.cost:.2f} TON", mode="live")
            return msg
        report.failed += 1
        uncertain = spend.kind == "unknown"
        view.status = ("Buy result unknown: counted toward your limits, check Tonnel" if uncertain
                       else f"Buy failed: {spend.text}")
        await self._send_card(view, status="skipped", now=now, ttl=DAY)
        msg = format_buy_failed(listing, spend.text, uncertain=uncertain)
        await self.notifier.send(msg)
        await self._decide(now, listing, rule, "failed", "BUY_FAILED", deal, spend.text, mode="live")
        return msg

    # ------------------------------------------------------------ the one spend path (auto-buys and taps)

    async def _spend(self, rule: BuyRule, listing: Listing, deal: DealMath, now: float, *, practice: bool,
                     detail: str, token: str | None = None) -> Spend:
        """Commit one buy, practice or live. Everything from the switch re-check to the purchase row runs
        under one lock, so concurrent taps and the loop see each other's spend and the daily budget and the
        hourly cap hold. A caller's `practice` can only be tightened here, never relaxed."""
        async with self._spend_lock:
            if self._closing:
                return Spend("refused", "PAUSED", "the bot is shutting down", practice=practice)
            practice = practice or rule.state == "shadow" or await self.is_dry()
            refused = await self._spend_gate(rule.id, live=not practice, token=token, listing=listing, deal=deal)
            if refused:
                return Spend("refused", *refused, practice=practice)
            if await self.storage.was_bought(listing.id, practice):
                return Spend("refused", "ALREADY_BOUGHT", "this listing was already bought", practice=practice)
            limits = await self.effective_limits()
            blocked = check_limits_detail(
                deal.cost, max_price=limits.max_price_ton, daily_budget=limits.daily_budget_ton,
                spent_today=await self.spent_today(now, practice), max_per_hour=limits.max_buys_per_hour,
                buys_last_hour=await self.storage.count_since(now - HOUR, practice))
            if blocked:
                return Spend("blocked", *blocked, practice=practice)
            if practice:
                pid = await self.storage.add_purchase(listing, cost_ton=deal.cost, floor_ton=deal.floor,
                                                      rule_id=rule.id, dry=True, status="dry", message=detail,
                                                      created_at=now)
                if rule.state == "shadow":  # the suite's hindsight record: never reaches the executor
                    await shadow.record(self.storage, rule, listing, deal, now, pid)
                return Spend("practice", practice=True)
            return await self._live_buy(rule, listing, deal, now, token)

    async def _rule_hold(self, rule_id: int | None, now: float | None = None, *,
                         live: bool = False) -> tuple[str, str] | None:
        """(code, text) when this rule may not buy now: removed, disabled, paused, crash-paused (or shadow, live)."""
        if rule_id is None:
            return None
        now = self.clock() if now is None else now
        rule = await self.storage.get_rule(rule_id)
        if rule is None or not rule.enabled:
            return "DISABLED", f"rule #{rule_id} was removed or disabled"
        if rule.state == "paused":
            return "RULE_PAUSED", f"rule #{rule_id} is paused"
        until = await self.storage.get_float(f"rule_paused_until:{rule_id}")
        if until is not None and until > now:
            return "RULE_PAUSED", f"rule #{rule_id} is paused by the floor-crash breaker"
        if live and rule.state == "shadow":
            return "SHADOW_RULE", f"rule #{rule_id} is in shadow (practice only)"
        return None

    async def _spend_gate(self, rule_id: int | None, *, live: bool, token: str | None = None,
                          listing: Listing | None = None, deal: DealMath | None = None) -> tuple[str, str] | None:
        """Every switch re-read from storage right before money moves: (code, text) when the buy must not happen."""
        if self._closing:
            return "PAUSED", "the bot is shutting down"
        if await self.is_panic():
            return "PAUSED", "stopped by /panic"
        if await self.is_paused():
            return "PAUSED", "the bot is paused"
        hold = await self._rule_hold(rule_id, live=live)
        if hold:
            return hold
        if rule_id is not None and listing is not None and deal is not None:
            changed = await self._rule_changed(rule_id, listing, deal, auto=token is None)
            if changed:
                return changed
        if token is not None:
            card = await self.storage.get_pending(token)
            if card is None or card.status != "executing":
                return "PAUSED", "the card was cancelled"
        if live and await self.is_dry():
            return "DRY_RUN", "practice mode is on"
        return None

    async def _rule_changed(self, rule_id: int, listing: Listing, deal: DealMath, *,
                            auto: bool) -> tuple[str, str] | None:
        """The rule as stored now (an edit may have landed since the cycle or the tap read it): its max, its
        filters and minimums (with a /preset minimum discount on top) and, for loop buys, its auto mode must
        still allow this buy."""
        fresh = await self.storage.get_rule(rule_id)
        if fresh is None:
            return "DISABLED", f"rule #{rule_id} was removed or disabled"
        if auto and fresh.mode != "auto":
            return "MODE_CHANGED", f"rule #{rule_id} was switched to {fresh.mode} mode"
        preset_min = await self.storage.get_float("limit:MIN_DISCOUNT_PCT")
        if preset_min:
            fresh = replace(fresh, min_discount_pct=max(fresh.min_discount_pct, preset_min))
        result = match(fresh, listing, deal)
        if not result.ok:
            return result.code, f"rule #{rule_id} changed: {result.reason}"
        return None

    async def _live_buy(self, rule: BuyRule, listing: Listing, deal: DealMath, now: float,
                        token: str | None) -> Spend:
        """Real money, called under the spend lock: balance (and the balance-mismatch breaker), every switch
        re-read once more (a /panic, /dry on, /pause or breaker may have landed during the balance call),
        then buy() and the purchase row. A buy whose result is unknown is recorded as 'unknown' and counts
        toward the limits."""
        try:
            balance = await self.client.balance()
        except Exception as exc:  # noqa: BLE001 - never buy without knowing the balance
            await self.notifier.send(f"SKIP {listing.short_title}: cannot read the balance ({exc})")
            return Spend("refused", "LIMIT_BALANCE", f"cannot read the balance ({exc})")
        if await self._balance_guard(balance, now):
            return Spend("refused", "PAUSED", "balance mismatch, paused")
        if balance < deal.cost:
            text = f"balance {balance:.2f} TON < cost {deal.cost:.2f} TON"
            await self.notifier.send(format_skip(listing, text))
            return Spend("refused", "LIMIT_BALANCE", text)
        refused = await self._spend_gate(rule.id, live=True, token=token, listing=listing, deal=deal)
        if refused:
            return Spend("refused", *refused)
        # Write-ahead: the purchase row exists as 'unknown' (it counts toward the limits) before the request
        # leaves, so a crash, a kill or a task cancel (shutdown) mid-request can never under-count a buy.
        purchase_id = await self.storage.add_purchase(
            listing, cost_ton=deal.cost, floor_ton=deal.floor, rule_id=rule.id, dry=False, status="unknown",
            message="buy request in flight (result unknown: counted toward your limits, check Tonnel)",
            created_at=now)
        started = time.perf_counter()
        try:
            res = await self.client.buy(listing)
        except Exception as exc:  # noqa: BLE001 - anything unexpected: the order may or may not have gone through
            res = BuyResult(False, f"{exc} (result unknown: counted toward your limits, check Tonnel)", uncertain=True)
        elapsed = time.perf_counter() - started
        status = "ok" if res.ok else ("unknown" if res.uncertain else "failed")
        await self.storage.set_purchase_status(purchase_id, status, res.message[:500])
        await self._after_buy(res, now)
        return Spend("bought" if res.ok else status, "OK" if res.ok else "BUY_FAILED", res.message, elapsed=elapsed)

    async def _after_buy(self, res: BuyResult, now: float) -> None:
        if res.ok:
            self.consecutive_buy_errors = 0
            return
        self.consecutive_buy_errors += 1
        if self.consecutive_buy_errors >= self.s.AUTO_PAUSE_ERRORS:
            await self.set_paused(True)
            await self._expire_cards("Expired: paused after failed buys")
            await self.notifier.send(
                f"Paused: {plural(self.consecutive_buy_errors, 'buy')} failed in a row (last: {res.message}). "
                "/resume to continue."
            )
            self._emit_state("pause", now, "executor errors")

    async def _expire_cards(self, line: str, *, rule_id: int | None = None) -> int:
        """A breaker fired: pending Buy buttons (all, or this rule's) stop working at once."""
        cards = await self.storage.expire_pending(None, rule_id=rule_id)
        for card in cards:
            await self._edit_card(card, line)
        return len(cards)

    async def _balance_guard(self, balance: float, now: float) -> bool:
        """True (and paused) when the balance fell by more than our own recorded live spend + tolerance."""
        if not self.s.BALANCE_GUARD:
            return False
        prev = await self.storage.get_float("balance_last")
        prev_ts = await self.storage.get_float("balance_ts")
        spent = await self.storage.spent_since(prev_ts, False) if prev_ts is not None else 0.0
        await self.storage.set_setting("balance_last", balance)
        await self.storage.set_setting("balance_ts", now)
        hit = balance_mismatch(prev, balance, spent, self.s.BALANCE_TOLERANCE_TON)
        if hit is None:
            return False
        fell, spent = hit
        await self.set_paused(True)
        await self._expire_cards("Expired: paused, the balance does not add up")
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import GateCB

        kb = InlineKeyboardBuilder()
        nonce = await self.issue_resume_nonce(now)  # after the pause above: a later stop voids it
        kb.button(text="Resume", callback_data=GateCB(action="resume", t=int(now), n=nonce))
        kb.button(text="Keep paused", callback_data=GateCB(action="keep", t=int(now)))
        await self.notifier.send(
            f"Balance fell {fell:.2f} TON but I spent {spent:.2f} TON: someone else may be using this account. "
            "Paused.",
            keyboard=kb.as_markup(),
        )
        self._emit_state("pause", now, "balance mismatch")
        return True

    # ------------------------------------------------------------ cards

    async def _send_card(self, view: DealView, *, status: str, now: float, ttl: float) -> str:
        """Render, send and store a deal card (pending row) so its buttons and edits work later."""
        assert view.token is not None
        card = render_deal_card(view)
        tpl = render_deal_card(view, with_slot=True)
        final_kb = render_deal_card(replace(view, pending=False)).keyboard
        listing = view.listing
        payload = {
            "listing": {"id": listing.id, "collection": listing.collection, "model": listing.model,
                        "backdrop": listing.backdrop, "symbol": listing.symbol, "number": listing.number,
                        "price_ton": listing.price_ton, "raw": listing.raw},
            "deal": view.deal.to_dict(),
            "title": listing.short_title,
            "mute_key": mute_keys(listing.collection, listing.model)[-1],
            "html_tpl": tpl.html,
            "kb_final": final_kb.model_dump_json(exclude_none=True) if final_kb else None,
            "mode": view.mode,
            "practice": view.practice,
        }
        await self.storage.create_pending(view.token, listing.id, view.rule.id, listing.price_ton, view.deal.cost,
                                          payload, now, ttl, status=status)
        msgs = await self.notifier.send_card(card)
        if msgs:
            await self.storage.set_pending_msgs(view.token, msgs)
        return view.token

    async def _edit_card(self, card: PendingCard, status_line: str) -> None:
        from aiogram.types import InlineKeyboardMarkup

        tpl = card.payload.get("html_tpl")
        if not tpl:
            return
        kb_json = card.payload.get("kb_final")
        kb = InlineKeyboardMarkup.model_validate_json(kb_json) if kb_json else None
        await self.notifier.edit(card.msgs, fill_status(tpl, status_line, is_html=True), html=True, keyboard=kb,
                                 note=f"{card.payload.get('title', card.listing_id)} → {status_line}")

    async def _near_miss(self, rule: BuyRule, listing: Listing, deal: DealMath, near: Any, now: float) -> None:
        s = self.s
        while self._near_times and now - self._near_times[0] >= HOUR:
            self._near_times.popleft()
        if len(self._near_times) >= s.NEAR_MISS_MAX_PER_HOUR:
            return
        ignored = await self.storage.get_float(f"near_ignore:{rule.id}")
        if ignored is not None and ignored > now:
            return
        if await self.storage.muted_key(mute_keys(listing.collection, listing.model), now):
            return
        self._near_times.append(now)
        token = secrets.token_urlsafe(6)
        limits = await self.effective_limits()
        card = render_near_miss(listing, deal, rule, near, token, max_cap=limits.max_price_ton,
                                practice=await self.is_dry(now), fees_verified=self.s.FEES_VERIFIED)
        await self.storage.create_pending(
            token, listing.id, rule.id, listing.price_ton, deal.cost,
            {"kind": "near", "title": listing.short_title, "check": near.check, "new_max": near.new_max,
             "mute_key": mute_keys(listing.collection, listing.model)[-1], "deal": deal.to_dict()}, now, DAY,
            status="near",
        )
        msgs = await self.notifier.send(card.html, html=True, keyboard=card.keyboard, silent=True, plain=card.plain)
        if msgs:
            await self.storage.set_pending_msgs(token, msgs)

    # ------------------------------------------------------------ taps (aiogram handlers and the mock FakeTapper)

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids

    async def tap(self, action: str, token: str, user_id: int | None, now: float | None = None) -> TapResult:
        now = self.clock() if now is None else now
        if not self.is_admin(user_id):
            return TapResult(False, "Not allowed")
        if action == "b":
            return await self._tap_buy(token, now)
        card = await self.storage.get_pending(token)
        if card is None:
            return TapResult(False, "Expired or already handled")
        title = card.payload.get("title", card.listing_id)
        if action == "s":
            if await self.storage.set_pending_status(token, "skipped", only_from=("pending",)):
                await self._edit_card(card, "Skipped")
                await self._tap_decision(card, now, "SKIPPED", "skipped by you")
                return TapResult(True, f"Skipped {title}", "skipped")
            return TapResult(False, "Expired or already handled")
        if action == "m":
            key = card.payload.get("mute_key") or norm(title)
            await self.storage.add_mute(key, now + MUTE_SEC)
            if await self.storage.set_pending_status(token, "skipped", only_from=("pending",)):
                await self._edit_card(card, "Skipped · muted for 24 h")
            return TapResult(True, f"Muted {key} for 24 h. Decisions are still logged (/why).", card.status)
        if action == "w":
            text = why_text(await self.storage.decisions_for(card.listing_id), title=title,
                            tz_offset_hours=self.s.TZ_OFFSET_HOURS)
            return TapResult(True, "Why?", card.status, reply=text)
        if action == "x":
            decisions = await self.storage.decisions_for(card.listing_id)
            event = {"title": title, "deal": card.payload.get("deal"),
                     "decisions": [{"outcome": d.outcome, "reason": d.reason_code, "detail": d.detail}
                                   for d in decisions]}
            text = await explain_more(event, self.s)
            return TapResult(text is not None, "Explain more", card.status, reply=text or "No explanation available.")
        return TapResult(False, "Unknown action")

    async def _tap_decision(self, card: PendingCard, now: float, code: str, text: str) -> None:
        await self.storage.add_decision(now, card.listing_id, card.price_ton, card.rule_id, "tap", code,
                                        {"text": text, "title": card.payload.get("title")})

    async def _finish(self, card: PendingCard, status: str, line: str, now: float, code: str,
                      text: str) -> TapResult:
        await self.storage.set_pending_status(card.token, status)
        await self._edit_card(card, line)
        await self._tap_decision(card, now, code, text)
        return TapResult(status in ("dry", "bought"), text, status)

    async def _tap_buy(self, token: str, now: float) -> TapResult:
        if await self.is_panic():
            return TapResult(False, "Stopped by /panic: /resume first, then /dry off")
        if await self.is_paused():
            return TapResult(False, "Paused: nothing is bought until /resume")
        pending = await self.storage.get_pending(token)
        if pending is not None and pending.status == "pending":
            hold = await self._rule_hold(pending.rule_id, now)
            if hold:
                return TapResult(False, f"Not bought: {hold[1]}")
        if not await self.storage.claim_pending(token, now):
            return TapResult(False, "Expired or already handled")
        card = await self.storage.get_pending(token)
        assert card is not None
        p = card.payload["listing"]
        hint = Listing(p["id"], p["collection"], p["model"], p["backdrop"], p["symbol"], p["number"], p["price_ton"],
                       None, raw=p.get("raw") or {})
        try:
            fresh = await self.client.get_listing(card.listing_id, hint)
        except Exception as exc:  # noqa: BLE001 - network error: never buy blind
            log.warning("re-check failed for %s: %s", card.listing_id, exc)
            if card.expires_at > now:
                await self.storage.set_pending_status(token, "pending", only_from=("executing",))
            await self._edit_card(card, "Couldn't re-check, not bought")
            return TapResult(False, "couldn't re-check, not bought", "pending")
        if fresh is None:
            return await self._finish(card, "gone", "Gone: sold to someone else", now, "GONE",
                                      "gone: sold to someone else")
        if abs(fresh.price_ton - card.price_ton) > 1e-9:
            return await self._finish(card, "changed", "Price changed, not bought", now, "PRICE_CHANGED",
                                      f"price changed {card.price_ton:.2f} → {fresh.price_ton:.2f}, not bought")
        rule = await self.storage.get_rule(card.rule_id) if card.rule_id is not None else None
        if rule is None or not rule.enabled:
            return await self._finish(card, "skipped", "Rule removed, not bought", now, "DISABLED",
                                      "rule removed, not bought")
        old = DealMath.from_dict(card.payload["deal"])
        est = FloorEstimate(old.floor, old.floor_source, old.n, old.confidence, old.note)
        deal = self._deal(fresh, est, now)
        result = match(rule, fresh, deal)
        if not result.ok:
            return await self._finish(card, "skipped", f"Re-check failed: {result.reason}", now, result.code,
                                      f"re-check failed: {result.reason}")
        # A card rendered as PRACTICE stays practice even if LIVE was armed since (fail closed: a payload
        # without the flag is practice). Live can only tighten to practice here, never the other way.
        card_practice = bool(card.payload.get("practice", True))
        spend = await self._spend(rule, fresh, deal, now, practice=card_practice or await self.is_dry(now),
                                  detail="tap", token=token)
        if spend.kind in ("refused", "blocked"):
            return await self._finish(card, "skipped", f"Not bought: {spend.text}", now, spend.code,
                                      f"not bought: {spend.text}")
        if spend.kind == "practice":
            shadow = rule.state == "shadow" and not await self.is_dry(now)
            text = f"re-check ok → PRACTICE: bought {fresh.short_title} for {deal.cost:.2f} TON"
            await self._decide(now, fresh, rule, "dry", "SHADOW_RULE" if shadow else "DRY_RUN", deal,
                               "practice buy after your tap", mode="shadow" if shadow else "dry")
            return await self._finish(card, "dry", f"PRACTICE: bought for {deal.cost:.2f} TON", now, "OK", text)
        if spend.kind == "bought":
            await self._decide(now, fresh, rule, "bought", "OK", deal, "bought after your tap", mode="live")
            return await self._finish(card, "bought", f"BOUGHT in {spend.elapsed:.1f} s", now, "OK",
                                      f"re-check ok → BOUGHT {fresh.short_title} for {deal.cost:.2f} TON")
        line = ("Buy result unknown: counted toward your limits, check Tonnel" if spend.kind == "unknown"
                else f"Buy failed: {spend.text}")
        return await self._finish(card, "skipped", line, now, "BUY_FAILED", f"buy failed: {spend.text}")

    async def drain(self, timeout: float) -> bool:
        """Shutdown: no new spend starts; wait (bounded) until the one in flight is recorded. True when idle."""
        self._closing = True
        try:
            await asyncio.wait_for(self._spend_lock.acquire(), timeout)
        except asyncio.TimeoutError:
            return False
        self._spend_lock.release()
        return True

    async def near_action(self, action: str, token: str, user_id: int | None, now: float | None = None) -> TapResult:
        """Near-miss buttons: r = raise the rule max (never above MAX_PRICE_TON), i = ignore this rule today."""
        now = self.clock() if now is None else now
        if not self.is_admin(user_id):
            return TapResult(False, "Not allowed")
        card = await self.storage.get_pending(token)
        if card is None or card.status != "near" or card.rule_id is None or card.expires_at <= now:
            return TapResult(False, "Expired or already handled")
        if action == "r":
            new_max = card.payload.get("new_max")
            cap = (await self.effective_limits()).max_price_ton
            if new_max is None or new_max > cap + 1e-9:
                return TapResult(False, f"Cannot raise above MAX_PRICE_TON {cap:g}")
            ok = await self.storage.update_rule(card.rule_id, max_price_ton=float(new_max))
            await self.storage.set_pending_status(token, "skipped")
            return TapResult(ok, f"Rule #{card.rule_id}: max raised to {new_max:.2f} TON" if ok else "Rule not found")
        if action == "i":
            end_of_day = now - (now + self.s.TZ_OFFSET_HOURS * HOUR) % DAY + DAY
            await self.storage.set_setting(f"near_ignore:{card.rule_id}", end_of_day)
            await self.storage.set_pending_status(token, "skipped")
            return TapResult(True, f"No more near-miss nudges for rule #{card.rule_id} today")
        return TapResult(False, "Unknown action")

    async def promote(self, rule_id: int, now: float | None = None) -> str:
        """The only path from shadow to live for a rule."""
        now = self.clock() if now is None else now
        rule = await self.storage.get_rule(rule_id)
        if rule is None:
            return f"Rule #{rule_id} not found."
        await self.storage.update_rule(rule_id, state="live", promoted_at=now)
        note = " Live buying also needs DRY_RUN=false in .env and /dry off." if self.live_locked else ""
        return f"Rule #{rule_id} is live (no longer shadow).{note}"

    # ------------------------------------------------------------ housekeeping

    async def tick(self, now: float | None = None, *, marks_search: bool = True) -> None:
        """Between cycles: expire cards, end an expired live gate, mark positions, run breakers, digest."""
        now = self.clock() if now is None else now
        self.last_tick_at = now
        for card in await self.storage.expire_pending(now):
            await self._edit_card(card, "Expired")
        await self.is_dry(now)
        if now - self.last_mark_at >= self.s.MARK_INTERVAL_SEC:
            await self.run_marks(now, search=marks_search)
        await self._losing_streak(now)
        await self._maybe_digest(now)
        await self._maybe_hindsight(now)

    async def _safe_tick(self, *, marks_search: bool = True) -> None:
        try:
            await self.tick(marks_search=marks_search)
        except ConfigError:
            raise
        except Exception:  # noqa: BLE001 - a failed tick must not stop the loop; the next one retries
            log.warning("tick failed", exc_info=True)

    async def run_marks(self, now: float | None = None, *, search: bool = True) -> int:
        from .ledger import mark_positions

        now = self.clock() if now is None else now
        self.last_mark_at = now

        async def do_search(collection: str, model: str | None) -> list[Listing]:
            return await self.client.search_listings(SearchFilters(
                collection=collection, model=model, sort="price_asc", limit=self.s.PAGE_LIMIT))

        written = await mark_positions(self.storage, self.s, now, self.fees_for, do_search if search else None)
        await shadow.mark(self.storage, now, sniped_window=self.s.SNIPED_WINDOW_SEC)  # fills reuse these marks
        return written

    async def _losing_streak(self, now: float) -> None:
        n = self.s.LOSS_STREAK_N
        recent = await self.storage.last_live_positions(n)
        if not losing_streak(recent, n, self.s.LOSS_STREAK_PCT):
            return
        newest = str(recent[0].id)
        if await self.storage.get_setting("streak_flagged") == newest:
            return
        await self.storage.set_setting("streak_flagged", newest)
        await self._void_live_window()  # an unused [Go live] button sent before the streak must not re-arm
        await self.notifier.send(
            f"{n} losing live buys in a row (each marked more than {self.s.LOSS_STREAK_PCT:g}% below cost): "
            "back to practice. /pnl for details, /dry off to go live again."
        )
        self._emit_state("pause", now, "losing streak")

    async def _maybe_digest(self, now: float) -> None:
        hour = self.s.DIGEST_HOUR
        if hour < 0:
            return
        local = now + self.s.TZ_OFFSET_HOURS * HOUR
        if int(local % DAY // HOUR) != hour:
            return
        day = int(local // DAY)
        if await self.storage.get_setting("digest_day") == str(day):
            return
        await self.storage.set_setting("digest_day", day)
        from .digest import digest_card, hint_keyboard

        start = (day - 1) * DAY - self.s.TZ_OFFSET_HOURS * HOUR
        text, hint = await digest_card(self, start, start + DAY)
        await self.notifier.send(text, silent=True, keyboard=hint_keyboard(hint, now))

    async def _maybe_hindsight(self, now: float) -> None:
        """Shadow rules: after SHADOW_MIN_HOURS with SHADOW_MIN_MATCHES (or SHADOW_MAX_HOURS), send the card."""
        for rule in await self.storage.list_rules(enabled_only=True):
            if rule.state != "shadow" or rule.id is None:
                continue
            card = await self.hindsight_card(rule, now)
            if card is None:
                continue
            text, kb = card
            await self.storage.set_setting(f"shadow_due:{rule.id}", now + DAY)
            await self.notifier.send(text, keyboard=kb)

    async def hindsight_card(self, rule: BuyRule, now: float, *, force: bool = False):
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        from .callbacks import ShadowCB
        from .ledger import hindsight_text, races_lost, summarize

        s = self.s
        started = (await self.storage.get_float(f"shadow_since:{rule.id}")
                   or await self.storage.rule_created(rule.id) or self.clock())
        hours = (now - started) / HOUR
        due = await self.storage.get_float(f"shadow_due:{rule.id}")
        positions = [p for p in await self.storage.positions(since=started, dry=True) if p.rule_id == rule.id]
        if not force:
            if due is not None and now < due:
                return None
            ready = hours >= s.SHADOW_MIN_HOURS and len(positions) >= s.SHADOW_MIN_MATCHES
            if not (ready or hours >= s.SHADOW_MAX_HOURS):
                return None
        stats = summarize(positions, practice=True, days=hours / 24 + 1, now=now, fees=self.fees).get("total")
        sniped, _ = await races_lost(self.storage, positions, s.SNIPED_WINDOW_SEC, now)
        text = hindsight_text(rule, hours, stats, sniped, self.live_locked)
        fills = await self.storage.shadow_fills(rule_id=rule.id, since=started)
        if fills:
            text += "\n" + shadow.fills_summary_line(fills)
        kb = InlineKeyboardBuilder()
        t = int(now)
        kb.button(text="Go live", callback_data=ShadowCB(action="live", rule_id=rule.id, t=t))
        kb.button(text="Keep testing 24h", callback_data=ShadowCB(action="more", rule_id=rule.id, t=t))
        kb.button(text="Delete", callback_data=ShadowCB(action="del", rule_id=rule.id, t=t))
        return text, kb.as_markup()

    # ------------------------------------------------------------ loop

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("engine started: poll=%.1fs dry=%s", self.s.POLL_INTERVAL, await self.is_dry())
        await self.storage.prune(self.clock(), obs_days=self.s.OBS_TTL_DAYS, snap_days=self.s.SNAPSHOT_TTL_DAYS,
                                 decision_days=self.s.DECISIONS_TTL_DAYS)
        while not stop.is_set():
            cycle_ok = False
            try:
                report = await self.run_cycle()
                cycle_ok = True
                level = logging.INFO if (report.new or report.actions) else logging.DEBUG
                log.log(level, "cycle %d: %s", self.cycles, report.summary())
                if self.consecutive_errors >= 5:
                    await self.notifier.send("API recovered, polling resumed.")
                self.consecutive_errors = 0
                self.last_error = None
                self._stale_warned = False
                delay = self.s.POLL_INTERVAL
            except ConfigError:
                raise
            except (RateLimited, AuthError) as exc:
                delay = await self._on_error(exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("cycle failed")
                delay = await self._on_error(exc)
            # Every iteration, even when the cycle failed: cards still expire, the live gate still ends on
            # time, breakers, the digest and hindsight cards still run. After a failed cycle the marks use
            # stored data only, so a rate-limited or down API gets no extra requests.
            await self._safe_tick(marks_search=cycle_ok)
            self.current_delay = delay
            if self.consecutive_errors == 5:
                await self.notifier.send(f"Engine: 5 consecutive errors, last: {self.last_error}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _on_error(self, exc: Exception) -> float:
        self.consecutive_errors += 1
        self.api_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        delay = backoff_delay(self.consecutive_errors, self.s.POLL_INTERVAL, self.s.MAX_BACKOFF)
        log.warning("%s; backing off %.1fs", self.last_error, delay)
        now = self.clock()
        day = int((now + self.s.TZ_OFFSET_HOURS * HOUR) // DAY)
        try:
            count = int(await self.storage.get_setting(f"errors:{day}", "0") or 0)
            await self.storage.set_setting(f"errors:{day}", count + 1)
        except Exception:  # noqa: BLE001 - storage trouble must not hide the original error
            pass
        last_ok = self.last_ok_at
        if last_ok is not None and now - last_ok > self.s.STALE_DATA_SEC and not self._stale_warned:
            # No fresh data means no new buys (every buy needs a fresh cycle or a re-check of the listing).
            self._stale_warned = True
            await self.notifier.send(f"Market data is stale ({now - last_ok:.0f}s without a good cycle): "
                                     "no buys until it recovers. /doctor may help.")
            self._emit_state("pause", now, "stale data")
        return delay
