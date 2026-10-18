"""Shadow-first rules: record what a shadow rule would have bought, and revalue it honestly.

A rule in state 'shadow' never reaches the executor (Engine._spend forces practice for it). Every buy
it would have made is written twice: as a practice purchase (so the limits, /pnl and /scoreboard see it)
and as a row in `shadow_fills` (the suite-wide hindsight record). Each tick, mark() revalues the open
fills net of fees and records whether the original listing vanished within SNIPED_WINDOW_SEC, which is
evidence the deal was real and someone else took it.

Mark price: the newest ledger mark of the matching practice purchase (the current comparable floor,
minus the sell fee and the withdraw fee, see ledger.mark_positions). Without a mark yet, the entry
deal's own net exit is used and the source says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Listing
    from .pricing import DealMath
    from .rules import BuyRule
    from .storage import Storage

FLAT_EPS = 0.005  # TON: a mark within this of the all-in cost is 'flat'


def outcome_for(executable_price: float, mark_price: float | None, *, eps: float = FLAT_EPS) -> str:
    """gain | loss | flat | open, comparing the net mark with the all-in cost."""
    if mark_price is None:
        return "open"
    diff = mark_price - executable_price
    if diff > eps:
        return "gain"
    if diff < -eps:
        return "loss"
    return "flat"


async def record(storage: "Storage", rule: "BuyRule", listing: "Listing", deal: "DealMath", now: float,
                 purchase_id: int | None) -> int:
    """One shadow fill. entry_price is the listed price, entry_costs the buy fee, executable_price the
    all-in cost a real buy would have paid."""
    meta: dict[str, Any] = {
        "title": listing.short_title, "collection": listing.collection, "model": listing.model,
        "purchase_id": purchase_id, "floor": deal.floor, "net_exit": deal.net_exit,
    }
    return await storage.add_shadow_fill(rule.id, listing.id, now, deal.price, round(deal.cost - deal.price, 9),
                                         deal.cost, meta)


async def mark(storage: "Storage", now: float, *, sniped_window: float) -> int:
    """Revalue every fill that is not settled as sniped. Returns the number of fills updated."""
    updated = 0
    for fill in await storage.shadow_fills():
        if fill["outcome"] == "sniped":
            continue
        meta = fill["meta"]
        price, source = None, "entry"
        pid = meta.get("purchase_id")
        latest = await storage.latest_mark(int(pid)) if pid is not None else None
        if latest is not None and latest[1] is not None:
            price, source = latest[1], f"floor ({latest[2] or 'low'})"
        elif meta.get("net_exit") is not None:
            price = float(meta["net_exit"])
        outcome = outcome_for(fill["executable_price"], price)
        last_seen = await storage.last_seen(fill["subject_ref"])
        if (last_seen is not None and now - fill["ts"] >= sniped_window and last_seen - fill["ts"] < sniped_window
                and now - last_seen > sniped_window
                and await storage.polled_after(meta.get("collection"), last_seen)):  # still polling: really gone
            outcome = "sniped"
        await storage.mark_shadow_fill(fill["id"], price, now, source, outcome)
        updated += 1
    return updated


@dataclass(slots=True)
class FillStats:
    n: int = 0
    cost: float = 0.0
    worth: float = 0.0
    sniped: int = 0
    best: float | None = None
    worst: float | None = None

    @property
    def net(self) -> float:
        return self.worth - self.cost

    @property
    def pct(self) -> float:
        return self.net / self.cost * 100 if self.cost else 0.0


def summarize_fills(fills: list[dict[str, Any]]) -> FillStats:
    """Totals net of fees. A fill without a mark is valued at its cost (no gain claimed)."""
    st = FillStats()
    for f in fills:
        st.n += 1
        cost = float(f["executable_price"])
        worth = float(f["mark_price"]) if f["mark_price"] is not None else cost
        st.cost += cost
        st.worth += worth
        st.sniped += f["outcome"] == "sniped"
        pnl = worth - cost
        st.best = pnl if st.best is None else max(st.best, pnl)
        st.worst = pnl if st.worst is None else min(st.worst, pnl)
    return st


def fill_line(f: dict[str, Any]) -> str:
    mark = f"{f['mark_price']:.2f}" if f["mark_price"] is not None else "n/a"
    pnl = f" ({f['mark_price'] - f['executable_price']:+.2f})" if f["mark_price"] is not None else ""
    return (f"shadow fill #{f['id']} rule #{f['rule_id']} {f['meta'].get('title', f['subject_ref'])}: "
            f"entry {f['entry_price']:.2f} + {f['entry_costs']:.2f} fee = {f['executable_price']:.2f} TON all-in · "
            f"mark {mark} net{pnl} [{f['mark_source'] or 'not marked'}] · {f['outcome']}")


def fills_summary_line(fills: list[dict[str, Any]]) -> str:
    st = summarize_fills(fills)
    if not st.n:
        return "Shadow fills: none yet"
    return (f"Shadow fills: {st.n}, {st.cost:.2f} TON all-in, worth {st.worth:.2f} net now ({st.pct:+.1f}%), "
            f"{st.sniped} sniped")
