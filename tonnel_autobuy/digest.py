"""Daily digest: one silent message with yesterday's numbers, spend against the budget, top skip reasons
and a tuning hint. When the hint is a rule's own max (OVER_MAX), the digest carries [Raise to X] [Keep];
[Raise to X] only asks for a confirmation, and the confirmed raise never goes above MAX_PRICE_TON.

Optional 'Your other bots' section: when PEER_EVENT_FILES lists the events.jsonl files of other bots you
run, their counts are added (missing, unreadable or schema-mismatched files are skipped with a single warning).
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from .events import read_events, validate_event
from .explain import reason_text
from .ledger import summarize
from .models import plural

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
_warned: set[str] = set()

ENV_LIMITS = ("LIMIT_MAX_PRICE", "LIMIT_DAILY_BUDGET", "LIMIT_HOURLY")
TUNABLE = {"OVER_MAX": "max price", "LOW_DISCOUNT": "min discount", "LOW_PROFIT": "min profit",
           "LIMIT_MAX_PRICE": "MAX_PRICE_TON", "LIMIT_DAILY_BUDGET": "DAILY_BUDGET_TON", "LIMIT_HOURLY": "MAX_BUYS_PER_HOUR"}


def aggregate_events(events: Iterable[dict[str, Any]], since: float, until: float) -> dict[str, Any]:
    """Counts per kind, buys by mode, money in USD where present, and top skip reasons."""
    kinds: Counter = Counter()
    reasons: Counter = Counter()
    modes: Counter = Counter()
    usd = 0.0
    for e in events:
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or not since <= ts < until:
            continue
        kind = e.get("kind")
        kinds[kind] += 1
        if kind == "buy":
            modes[e.get("mode")] += 1
            money = e.get("money") or {}
            if isinstance(money.get("usd"), (int, float)):
                usd += money["usd"]
        if kind == "skip":
            for r in e.get("reasons") or []:
                if isinstance(r, dict) and r.get("code"):
                    reasons[r["code"]] += 1
    return {"alerts": kinds["alert"], "buys": kinds["buy"], "skips": kinds["skip"], "errors": kinds["error"],
            "buys_by_mode": dict(modes), "usd": round(usd, 2), "top_reasons": reasons.most_common(3)}


def load_peer_events(paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    """events.jsonl files of other bots you run. Bad files are skipped with one warning each, never a crash."""
    out: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        try:
            events = read_events(path)
            if not events:
                raise FileNotFoundError(path)
            if validate_event(events[-1]):
                raise ValueError("schema mismatch")
            out[path] = events
        except Exception as exc:
            if path not in _warned:
                _warned.add(path)
                log.warning("peer events %s skipped: %s", path, type(exc).__name__)
    return out


@dataclass(slots=True)
class RaiseHint:
    """A digest tuning hint that a button can apply (after a confirmation): raise one rule's max."""

    rule_id: int
    old_max: float
    new_max: float
    skipped: int
    winners: int
    gain: float


def _round_up(value: float, step: float = 0.05) -> float:
    return round(math.ceil(round(value / step, 6)) * step, 2)


async def _net_now(engine: "Engine", d: Any) -> float | None:
    """Missed net profit of a skipped deal at today's floor (the newest floor snapshot for its collection and
    model, net of the sell and withdraw fees), else its net at the floor when it was skipped."""
    from .ledger import position_key

    cost, coll = d.detail.get("cost"), d.detail.get("collection")
    if cost is not None and coll:
        for key in (position_key(coll, d.detail.get("model")), position_key(coll, None)):
            snap = await engine.storage.latest_snapshot(key)
            if snap and snap.floor_ton:
                return engine.fees_for(coll).net_exit(snap.floor_ton) - float(cost)
    profit = d.detail.get("profit_ton")
    return float(profit) if profit is not None else None


async def tuning(engine: "Engine", decisions: list[Any], top: list[tuple[str, int]]) -> tuple[str | None, RaiseHint | None]:
    """One tuning line for the first tunable top skip reason that hid +net deals, plus a RaiseHint when the
    fix is a rule's own max (OVER_MAX) and stays within MAX_PRICE_TON. .env limits are never raised by the bot:
    for those the line says which .env key to edit."""
    for code, n in top:
        if code not in TUNABLE:
            continue
        winners = []
        for d in decisions:
            if d.reason_code != code:
                continue
            net = await _net_now(engine, d)
            if net is not None and net > 0:
                winners.append((d, net))
        if not winners:
            continue
        gain = sum(net for _, net in winners)
        line = (f"Tuning hint: {plural(n, 'deal')} {'was' if n == 1 else 'were'} skipped by {code} "
                f"({TUNABLE[code]}), and {len(winners)} of {'it' if n == 1 else 'them'} would "
                f"be +net today ({gain:+.2f} TON).")
        if code in ENV_LIMITS:
            return line + f" Only you can raise {TUNABLE[code]} in .env; the bot never raises it.", None
        if code != "OVER_MAX":
            return line + " Adjust the rule with /rules.", None
        by_rule: dict[int, list[tuple[Any, float]]] = {}
        for d, net in winners:
            if d.rule_id is not None and d.detail.get("cost") is not None:
                by_rule.setdefault(d.rule_id, []).append((d, net))
        if not by_rule:
            return line, None
        rule_id, items = max(by_rule.items(), key=lambda kv: (len(kv[1]), sum(x for _, x in kv[1])))
        rule = await engine.storage.get_rule(rule_id)
        if rule is None:
            return line, None
        new_max = _round_up(max(float(d.detail["cost"]) for d, _ in items))
        cap = (await engine.effective_limits()).max_price_ton
        if new_max > cap + 1e-9:
            return line + f" Raising rule #{rule_id} to {new_max:.2f} would pass MAX_PRICE_TON {cap:g}.", None
        skipped = sum(1 for d in decisions if d.reason_code == code and d.rule_id == rule_id)
        hint = RaiseHint(rule_id, rule.max_price_ton, new_max, skipped, len(items), sum(x for _, x in items))
        return line + f" Rule #{rule_id}: raise max {rule.max_price_ton:g} → {new_max:.2f} TON? (asks to confirm)", hint
    return None, None


async def spend_line(engine: "Engine", positions: list[Any]) -> str:
    limits = await engine.effective_limits()
    budget = limits.daily_budget_ton
    practice = sum(p.cost_ton for p in positions if p.dry)
    live = sum(p.cost_ton for p in positions if not p.dry)

    def pct(x: float) -> str:
        return f" ({x / budget * 100:.0f}%)" if budget else ""

    return (f"Spend vs budget: practice {practice:.2f} / {budget:g} TON{pct(practice)} · "
            f"live {live:.2f} / {budget:g} TON{pct(live)} (DAILY_BUDGET_TON per UTC day)")


async def build_digest(engine: "Engine", since: float, until: float, *, title: str = "Daily digest") -> str:
    return (await digest_card(engine, since, until, title=title))[0]


async def digest_card(engine: "Engine", since: float, until: float, *,
                      title: str = "Daily digest") -> tuple[str, RaiseHint | None]:
    storage, s = engine.storage, engine.settings
    observed = [o for o in await storage.observations_since(since) if o["first_seen"] < until]
    decisions = await storage.decisions_since(since, until)
    alerts = sum(1 for d in decisions if d.outcome == "alert")
    buys = [d for d in decisions if d.outcome in ("dry", "bought")]
    positions = await storage.positions(since=since)
    positions = [p for p in positions if p.created_at < until]
    days = (until - since) / 86400 + 1
    practice = summarize(positions, practice=True, days=days, now=until, fees=engine.fees)["total"]
    live = summarize(positions, practice=False, days=days, now=until, fees=engine.fees)["total"]
    reasons = Counter({k: v for k, v in (await storage.reason_counts(since, until)).items()})
    lines = [
        f"{title}:",
        f"Listings scanned: {len({o['listing_id'] for o in observed})} · alerts: {alerts} · buys: {len(buys)}",
        await spend_line(engine, positions),
        f"Practice net: {practice.net:+.2f} TON ({plural(practice.buys, 'buy')}) · "
        f"Live net: {live.net:+.2f} TON ({plural(live.buys, 'buy')})",
    ]
    top = reasons.most_common(3)
    hint = None
    if top:
        lines.append("Top skip reasons: " + " · ".join(f"{code} {n} ({reason_text(code)})" for code, n in top))
        line, hint = await tuning(engine, decisions, top)
        if line:
            lines.append(line)
    else:
        lines.append("Top skip reasons: none")
    day = int((until - 1 + s.TZ_OFFSET_HOURS * 3600) // 86400)
    errors = await storage.get_setting(f"errors:{day}", "0")
    lines.append(f"API errors: {errors}")
    peers = load_peer_events(s.peer_event_files)
    if peers:
        lines.append("Your other bots:")
        for path, events in peers.items():
            agg = aggregate_events(events, since, until)
            bot = next((e.get("bot") for e in events if e.get("bot")), path)
            lines.append(f"  {bot}: {plural(agg['alerts'], 'alert')} · {plural(agg['buys'], 'buy')} · "
                         f"{plural(agg['skips'], 'skip')}"
                         + (f" · ${agg['usd']:.2f} bought" if agg["usd"] else ""))
    return "\n".join(lines), hint


def hint_keyboard(hint: RaiseHint | None, now: float = 0.0):
    """[Raise to X] [Keep] under the digest. [Raise to X] only asks for confirmation; it changes nothing."""
    if hint is None:
        return None
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from .callbacks import DigestCB

    kb = InlineKeyboardBuilder()
    cents = int(round(hint.new_max * 100))
    t = int(now)
    kb.button(text=f"Raise to {hint.new_max:.2f}",
              callback_data=DigestCB(action="ask", rule_id=hint.rule_id, cents=cents, t=t))
    kb.button(text="Keep", callback_data=DigestCB(action="keep", rule_id=hint.rule_id, cents=cents, t=t))
    return kb.as_markup()


def confirm_keyboard(rule_id: int, cents: int, now: float):
    """The confirm step: [Confirm] works for BUTTON_TTL_SEC after it was sent."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from .callbacks import DigestCB

    kb = InlineKeyboardBuilder()
    t = int(now)
    kb.button(text=f"Confirm: max {cents / 100:.2f} TON",
              callback_data=DigestCB(action="ok", rule_id=rule_id, cents=cents, t=t))
    kb.button(text="Cancel", callback_data=DigestCB(action="keep", rule_id=rule_id, cents=cents, t=t))
    return kb.as_markup()


async def apply_raise(engine: "Engine", rule_id: int, new_max: float) -> tuple[bool, str]:
    """The confirmed raise: one rule's max, never above the effective MAX_PRICE_TON."""
    cap = (await engine.effective_limits()).max_price_ton
    if new_max > cap + 1e-9:
        return False, f"Cannot raise above MAX_PRICE_TON {cap:g}"
    rule = await engine.storage.get_rule(rule_id)
    if rule is None:
        return False, f"Rule #{rule_id} not found"
    if new_max <= rule.max_price_ton:
        return False, f"Rule #{rule_id} max is already {rule.max_price_ton:g} TON"
    await engine.storage.update_rule(rule_id, max_price_ton=float(new_max))
    return True, f"Rule #{rule_id}: max raised {rule.max_price_ton:g} → {new_max:.2f} TON"
