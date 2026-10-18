"""Safety envelope: /safety text, breakers (floor crash, losing streak, balance mismatch) and presets.

All functions here are pure; the engine feeds them storage data and acts on the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from .models import plural

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Position


@dataclass(slots=True, frozen=True)
class SafetyState:
    live: bool
    locked: bool  # DRY_RUN=true in .env
    live_until: float | None
    panic: bool
    paused: bool
    spent_today: float  # since 00:00 UTC (DAILY_BUDGET_TON is per UTC day)
    buys_last_hour: int
    max_price: float
    daily_budget: float
    max_per_hour: int


def _clock(ts: float, tz_offset_hours: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=tz_offset_hours))).strftime("%H:%M")


def safety_text(st: SafetyState, settings: "Settings") -> str:
    """Plain sentences built from Settings and storage. Shown by /safety and atop /start and /status."""
    if st.panic:
        mode = "Mode: STOPPED by /panic (practice). /resume, then /dry off, to go live again."
    elif st.live and st.live_until:
        mode = f"Mode: LIVE until {_clock(st.live_until, settings.TZ_OFFSET_HOURS)}, practice after that."
    elif st.live:
        mode = "Mode: LIVE."
    elif st.locked:
        mode = "Mode: PRACTICE, locked by DRY_RUN=true in .env."
    else:
        mode = ("Mode: PRACTICE. To go live for %g h: /dry off (practice report first), then its "
                "one-time button or /dry off confirm." % settings.LIVE_ARM_HOURS)
    left = max(0.0, st.daily_budget - st.spent_today)
    lines = [
        mode + (" Buying is paused." if st.paused and not st.panic else ""),
        f"Today (UTC day) I can spend up to {st.daily_budget:g} TON ({st.spent_today:.1f} used, {left:.1f} left).",
        f"One gift: max {st.max_price:g} TON incl. fees. At most {plural(st.max_per_hour, 'buy')} per hour "
        f"({st.buys_last_hour} in the last hour).",
        f"Buys above {settings.confirm_above_ton:g} TON always ask first.",
        "I never withdraw, transfer, list or sell.",
        f"Breakers: floor crash {settings.CRASH_PCT:g}%/1h, {settings.LOSS_STREAK_N} losing buys in a row"
        + (f", balance mismatch over {settings.BALANCE_TOLERANCE_TON:g} TON" if settings.BALANCE_GUARD else "")
        + f", {settings.AUTO_PAUSE_ERRORS} failed buys in a row.",
    ]
    return "\n".join(lines)


def floor_crash(snapshots: Iterable[tuple[float, float]], now: float, pct: float, window: float = 3600.0) -> float | None:
    """Drop (percent, positive) of the newest floor versus the highest floor in the window, if > pct."""
    recent = sorted((ts, v) for ts, v in snapshots if now - window <= ts <= now + 1 and v and v > 0)
    if len(recent) < 2:
        return None
    newest = recent[-1][1]
    top = max(v for _, v in recent[:-1])
    drop = (top - newest) / top * 100
    return drop if drop > pct else None


def losing_streak(positions: list["Position"], n: int, pct: float) -> bool:
    """The last n LIVE buys are all marked below cost by more than pct."""
    if n <= 0 or len(positions) < n:
        return False
    recent = positions[:n]
    return all(p.mark_net is not None and p.mark_net < p.cost_ton * (1 - pct / 100) for p in recent)


def balance_mismatch(previous: float | None, current: float, spent_since: float, tolerance: float) -> tuple[float, float] | None:
    """Balance fell by more than our own recorded live spend plus tolerance -> (fell, spent)."""
    if previous is None:
        return None
    fell = previous - current
    if fell > spent_since + tolerance:
        return fell, spent_since
    return None


# --- presets ---------------------------------------------------------------------------

PRESETS: dict[str, dict[str, float]] = {
    "careful": {"MAX_PRICE_TON": 5, "DAILY_BUDGET_TON": 15, "MAX_BUYS_PER_HOUR": 1, "MIN_DISCOUNT_PCT": 20},
    "normal": {"MAX_PRICE_TON": 10, "DAILY_BUDGET_TON": 50, "MAX_BUYS_PER_HOUR": 3, "MIN_DISCOUNT_PCT": 10},
    "bold": {"MAX_PRICE_TON": 25, "DAILY_BUDGET_TON": 150, "MAX_BUYS_PER_HOUR": 6, "MIN_DISCOUNT_PCT": 5},
}
PRESET_KEYS = ("MAX_PRICE_TON", "DAILY_BUDGET_TON", "MAX_BUYS_PER_HOUR", "MIN_DISCOUNT_PCT")


def preset_values(name: str, settings: "Settings") -> dict[str, float]:
    """Preset values capped by .env: a preset can tighten limits but never raise them above .env."""
    preset = PRESETS[name]
    caps = {"MAX_PRICE_TON": settings.MAX_PRICE_TON, "DAILY_BUDGET_TON": settings.DAILY_BUDGET_TON,
            "MAX_BUYS_PER_HOUR": settings.MAX_BUYS_PER_HOUR}
    return {k: (min(v, caps[k]) if k in caps else v) for k, v in preset.items()}


def preset_diff(current: dict[str, float], name: str, settings: "Settings") -> list[str]:
    """Before/after lines for /preset; notes where .env caps the preset."""
    new = preset_values(name, settings)
    lines = []
    for key in PRESET_KEYS:
        old, val = current.get(key, 0), new[key]
        mark = "" if old == val else ("  ↑" if val > old else "  ↓")
        capped = " (capped by .env)" if val < PRESETS[name][key] else ""
        lines.append(f"{key}: {old:g} → {val:g}{mark}{capped}")
    return lines
