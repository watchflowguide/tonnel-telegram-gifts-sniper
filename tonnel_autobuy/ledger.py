"""Practice-first ledger: marks open positions at the current floor and reports P&L net of fees.

PRACTICE and LIVE results are never mixed. Practice assumes you won every race for the
listing, so real results are worse; the scoreboard says so.
"""

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable

from .models import Listing, norm, plural
from .pricing import FeeModel, estimate_floor
from .storage import Position

if TYPE_CHECKING:
    from .config import Settings
    from .rules import BuyRule
    from .storage import Storage

log = logging.getLogger(__name__)

DAY = 86400.0
FOOTNOTE = "Practice assumes you won every race for the listing; real results are worse."


@dataclass(slots=True)
class Stats:
    buys: int = 0
    spent: float = 0.0
    unrealized: float = 0.0
    realized: float = 0.0
    wins: int = 0
    valued: int = 0
    best: float | None = None
    worst: float | None = None
    tied_up: float = 0.0

    @property
    def net(self) -> float:
        return self.unrealized + self.realized

    def add(self, cost: float, net: float | None, *, closed: bool) -> None:
        self.buys += 1
        self.spent += cost
        if not closed:
            self.tied_up += cost
        if net is None:
            return
        self.valued += 1
        if closed:
            self.realized += net
        else:
            self.unrealized += net
        if net > 0:
            self.wins += 1
        self.best = net if self.best is None else max(self.best, net)
        self.worst = net if self.worst is None else min(self.worst, net)


def position_net(p: Position, fees: FeeModel) -> float | None:
    """Realized net for closed positions, else latest mark minus cost; None when never marked."""
    if p.closed_at is not None and p.exit_price_ton is not None:
        return fees.net_exit(p.exit_price_ton) - p.cost_ton
    if p.mark_net is not None:
        return p.mark_net - p.cost_ton
    return None


def summarize(positions: Iterable[Position], *, practice: bool, days: float, now: float,
              fees: FeeModel) -> dict[int | str | None, Stats]:
    """Per rule id plus 'total'. Only practice (dry) or only live positions, never both."""
    out: dict[int | str | None, Stats] = defaultdict(Stats)
    for p in positions:
        if p.dry != practice or p.created_at < now - days * DAY:
            continue
        net = position_net(p, fees)
        closed = p.closed_at is not None
        out[p.rule_id].add(p.cost_ton, net, closed=closed)
        out["total"].add(p.cost_ton, net, closed=closed)
    out.setdefault("total", Stats())
    return dict(out)


def verdict(stats: Stats | None, settings: "Settings") -> str:
    if stats is None or stats.buys == 0:
        return "not enough data"
    if stats.valued and stats.net < 0:
        return "losing"
    if stats.buys >= settings.GATE_MIN_TRADES and stats.valued and stats.net >= 0:
        return "ready"
    return "not enough data"


def _ton(x: float | None) -> str:
    if x is None:
        return "n/a"
    return ("−" if x < 0 else "+") + f"{abs(x):.1f} TON"


def headline(label: str, days: float, s: Stats) -> str:
    if s.buys == 0:
        return f"{label} {days:g}d: no buys"
    parts = [f"{label} {days:g}d: {plural(s.buys, 'buy')}", f"spent {s.spent:.1f} TON",
             f"unrealized {_ton(s.unrealized)} after fees"]
    if s.realized:
        parts.append(f"realized {_ton(s.realized)}")
    parts.append(f"win {s.wins}/{s.buys}")
    if s.worst is not None:
        parts.append(f"worst {_ton(s.worst)}")
    return " · ".join(parts)


def _table(summary: dict[int | str | None, Stats]) -> list[str]:
    rows = [f"{'Rule':<6}{'Buys':>5}{'Spent':>8}{'Unreal':>8}{'Real':>7}{'Win':>6}"]
    for key in sorted((k for k in summary if k != "total"), key=lambda k: (k is None, k if isinstance(k, int) else 0)):
        s = summary[key]
        name = f"#{key}" if isinstance(key, int) else "-"
        rows.append(f"{name:<6}{s.buys:>5}{s.spent:>8.2f}{s.unrealized:>+8.2f}{s.realized:>+7.2f}{s.wins:>4}/{s.buys}")
    return rows


def format_pnl(practice: dict, live: dict, days: float, *, html: bool = True) -> str:
    blocks = []
    for label, summary in (("PRACTICE", practice), ("LIVE", live)):
        total = summary.get("total", Stats())
        lines = [headline(label, days, total)]
        if total.buys:
            lines += _table(summary)
            lines.append(f"Capital tied up: {total.tied_up:.2f} TON · best {_ton(total.best)}")
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    if html:
        from html import escape

        return f"<pre>{escape(body)}</pre>\n{FOOTNOTE}"
    return f"{body}\n{FOOTNOTE}"


def format_score(rules: list["BuyRule"], verdicts: dict[int, str]) -> str:
    parts = [f"Rule #{r.id}: {verdicts.get(r.id or 0, 'not enough data')}" for r in rules]
    text = " · ".join(parts) if parts else "No rules yet."
    if any(v != "ready" for v in verdicts.values()) or not verdicts:
        text += " → go-live needs /dry off confirm"
    else:
        text += " → ready for /dry off"
    return text


async def rule_verdicts(storage: "Storage", rules: list["BuyRule"], settings: "Settings", now: float,
                        fees: FeeModel) -> dict[int, str]:
    positions = await storage.positions(since=now - settings.GATE_DAYS * DAY, dry=True)
    summary = summarize(positions, practice=True, days=settings.GATE_DAYS, now=now, fees=fees)
    return {r.id: verdict(summary.get(r.id), settings) for r in rules if r.id is not None}


CSV_HEADER = [
    "purchase_id", "created_at", "mode", "rule_id", "collection", "model", "title", "price_ton", "cost_ton",
    "status", "closed_at", "exit_price_ton", "mark_ts", "mark_floor_ton", "mark_net_exit_ton", "net_ton",
]


def _iso(ts: float | None) -> str:
    return "" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def export_csv(positions: Iterable[Position], fees: FeeModel) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for p in positions:
        net = position_net(p, fees)
        w.writerow([
            p.id, _iso(p.created_at), "practice" if p.dry else "live", p.rule_id or "", p.collection, p.model or "",
            p.title, f"{p.price_ton:.4f}", f"{p.cost_ton:.4f}", p.status, _iso(p.closed_at),
            "" if p.exit_price_ton is None else f"{p.exit_price_ton:.4f}", _iso(p.mark_ts),
            "" if p.mark_floor is None else f"{p.mark_floor:.4f}", "" if p.mark_net is None else f"{p.mark_net:.4f}",
            "" if net is None else f"{net:.4f}",
        ])
    return buf.getvalue()


def position_key(collection: str, model: str | None) -> str:
    """Snapshot key of a (collection, model) position: the filter key of a rule pinning only that model."""
    return "|".join((norm(collection), norm(model), "", ""))


Search = Callable[[str, str | None], Awaitable[list[Listing]]]


async def mark_positions(storage: "Storage", settings: "Settings", now: float,
                         fees_for: Callable[[str], FeeModel], search: Search | None = None) -> int:
    """Mark every open position at the current floor (net of fees). Uses floor_snapshots first; falls
    back to at most MARK_MAX_QUERIES searches per pass. Returns the number of marks written."""
    positions = await storage.open_positions(now, settings.PAPER_HORIZON_DAYS)
    groups: dict[tuple[str, str], list[Position]] = defaultdict(list)
    for p in positions:
        groups[(p.collection, p.model or "")].append(p)
    queries = written = 0
    for (collection, model), items in groups.items():
        floor = conf = None
        for key in (position_key(collection, model), position_key(collection, None)):
            snap = await storage.latest_snapshot(key, now - settings.MARK_MAX_AGE_SEC, before=now)
            if snap and snap.floor_ton:
                floor = snap.floor_ton
                conf = "high" if snap.source == "cluster" and snap.n >= settings.MIN_COMPARABLES_ATTR else (
                    "med" if snap.n >= 2 else "low")
                break
        if floor is None and search is not None and queries < settings.MARK_MAX_QUERIES:
            queries += 1
            try:
                pool = await search(collection, model or None)
            except Exception as exc:
                log.warning("mark search failed for %s/%s: %s", collection, model, exc)
                pool = []
            est = estimate_floor(None, pool, None, [], now, k=settings.CLUSTER_K, band=settings.CLUSTER_BAND_PCT,
                                 min_comparables=settings.MIN_COMPARABLES_ATTR if model else settings.MIN_COMPARABLES_COLLECTION)
            floor, conf = est.value, est.confidence
        if floor is None:
            continue
        fees = fees_for(collection)
        for p in items:
            await storage.add_mark(p.id, now, floor, fees.net_exit(floor), conf or "low")
            written += 1
    return written


async def races_lost(storage: "Storage", positions: Iterable[Position], window: float, now: float) -> tuple[int, int]:
    """(lost, checked): practice buys whose listing vanished within `window` seconds, so someone else was
    probably faster. A listing that disappeared only counts when the bot kept polling its collection after
    it was last seen; while paused, after /panic or with the bot stopped there is no evidence either way,
    and the buy is not checked."""
    lost = checked = 0
    for p in positions:
        if not p.dry or now - p.created_at < window:
            continue
        last = await storage.last_seen(p.listing_id)
        if last is None:
            continue
        gone = now - last > window
        if gone and not await storage.polled_after(p.collection, last):
            continue
        checked += 1
        if gone and last - p.created_at < window:
            lost += 1
    return lost, checked


def races_line(lost: int, checked: int, window: float) -> str:
    minutes = int(window // 60) or 1
    if not checked:
        return "Races probably lost: none to check yet"
    return (f"Races probably lost: {lost} of {plural(checked, 'practice buy')} "
            f"(listing gone within {minutes} min)")


async def scoreboard(storage: "Storage", settings: "Settings", rules: list["BuyRule"], now: float,
                     fees: FeeModel, days: float = 7) -> str:
    """/scoreboard: the honest proof before going live (practice, net of fees)."""
    positions = await storage.positions(since=now - days * DAY, dry=True)
    summary = summarize(positions, practice=True, days=days, now=now, fees=fees)
    total = summary.get("total", Stats())
    lost, checked = await races_lost(storage, positions, settings.SNIPED_WINDOW_SEC, now)
    worth = sum((p.mark_net if p.mark_net is not None else p.cost_ton) for p in positions if p.closed_at is None)
    verdicts = await rule_verdicts(storage, rules, settings, now, fees)
    lines = [
        f"Practice scoreboard, last {days:g} days (net of fees):",
        f"Would have bought: {plural(total.buys, 'gift')} for {total.spent:.2f} TON all-in",
        f"Worth now if resold at floor: {worth:.2f} TON net",
        f"Would-be P&L: {_ton(total.net)} (unrealized {_ton(total.unrealized)}, realized {_ton(total.realized)})",
        races_line(lost, checked, settings.SNIPED_WINDOW_SEC),
        format_score(rules, verdicts),
        FOOTNOTE,
    ]
    return "\n".join(lines)


def hindsight_text(rule: "BuyRule", hours: float, s: Stats, sniped: int, dry_run_locked: bool) -> str:
    worth = s.spent + s.net
    pct = (s.net / s.spent * 100) if s.spent else 0.0
    text = (
        f'Rule "{rule.describe()}", {hours:.0f}h trial: would have bought {s.buys} for {s.spent:.1f} TON; '
        f"worth {worth:.1f} net now ({pct:+.1f}%); {sniped} of {s.buys} sold to someone else within 2 min; "
        f"best {s.best if s.best is not None else 0:+.1f}, worst {s.worst if s.worst is not None else 0:+.1f}"
    )
    if dry_run_locked:
        text += "\nGo live only promotes the rule: real buying also needs DRY_RUN=false in .env and /dry off."
    return text
