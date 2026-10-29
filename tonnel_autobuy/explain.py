"""Reason codes -> plain English, the /why trail, and the optional [Explain more] LLM call.

Reason codes are machine readable; their text lives only in REASON_TEXT. Internal codes
(match, limits, guards) map onto the suite-wide codes shared by all six bots via SUITE_CODE.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .models import plural

if TYPE_CHECKING:
    from .config import Settings
    from .storage import Decision

log = logging.getLogger(__name__)

REASON_TEXT: dict[str, str] = {
    # match() codes
    "OK": "matches the rule",
    "DISABLED": "the rule is disabled",
    "MISMATCH": "a different collection or attribute",
    "BAD_PRICE": "no usable TON price",
    "TRAP_ASSET": "priced in another asset, not TON",
    "TRAP_BUNDLE": "a bundle, not a single gift",
    "TRAP_AUCTION": "an auction, not a fixed-price listing",
    "TRAP_LUCKY": "a lucky-buy ticket, not a plain listing",
    "LOCKED": "the gift is locked and cannot be resold yet",
    "OVER_MAX": "the all-in cost is above your max",
    "LOW_CONFIDENCE": "the floor is not reliable enough",
    "LOW_DISCOUNT": "the discount is too small",
    "LOSES_AFTER_FEES": "it loses money after fees",
    "LOW_PROFIT": "the net profit is below your minimum",
    # engine limits and states
    "LIMIT_MAX_PRICE": "the cost is above MAX_PRICE_TON",
    "LIMIT_DAILY_BUDGET": "today's budget (UTC day) is used up",
    "LIMIT_HOURLY": "the buys-per-hour cap is reached",
    "LIMIT_BALANCE": "the balance is too low",
    "LIMIT_EXPOSURE": "too much capital is tied up",
    "MUTED": "you muted this collection",
    "PAUSED": "buying is paused",
    "RULE_PAUSED": "this rule is paused by a breaker",
    "PANIC": "/panic stopped all buying",
    "DRY_RUN": "practice mode: nothing is really bought",
    "SHADOW_RULE": "the rule is still in shadow (practice) mode",
    # downgrades: auto -> ask first
    "TOO_GOOD": "the price is suspiciously far below the floor",
    "LOCKED_ALLOWED": "the gift is locked for a while",
    "CHEAPER_ELSEWHERE": "the same gift is cheaper on another market",
    "ABOVE_TG_VALUE": "the cost is above Telegram's own valuation",
    "CONFIRM_ABOVE": "big buys always ask first",
    # tap results
    "GONE": "the listing was sold to someone else",
    "PRICE_CHANGED": "the price changed before the buy",
    "RECHECK_FAILED": "the listing could not be re-checked",
    "EXPIRED": "the card expired",
    "SKIPPED": "you skipped it",
    "BUY_FAILED": "the market rejected the buy",
    "ALREADY_BOUGHT": "this listing was already bought",
    "MODE_CHANGED": "the rule left auto mode while the buy was being prepared",
    # suite-wide codes (shared JSON contract)
    "BELOW_FLOOR": "listed below the floor",
    "BELOW_FAIR": "listed below fair value",
    "FEW_COMPS": "few comparable listings",
    "STALE_DATA": "the market data is stale",
    "FEES_EAT_PROFIT": "fees eat the profit",
    "PRICE_ABOVE_MAX": "the price is above your max",
    "DISCOUNT_TOO_SMALL": "the discount is too small",
    "WASHY_COMPS": "comparable sales look like wash trades",
    "RARE_TRAITS": "rare traits",
}

# Internal code -> suite-wide code used in events.jsonl (codes not listed map to themselves).
SUITE_CODE: dict[str, str] = {
    "LOSES_AFTER_FEES": "FEES_EAT_PROFIT",
    "LOW_PROFIT": "FEES_EAT_PROFIT",
    "OVER_MAX": "PRICE_ABOVE_MAX",
    "LIMIT_MAX_PRICE": "PRICE_ABOVE_MAX",
    "LOW_DISCOUNT": "DISCOUNT_TOO_SMALL",
    "LOW_CONFIDENCE": "FEW_COMPS",
}

GOOD_CODES = {"OK", "BELOW_FLOOR", "BELOW_FAIR", "RARE_TRAITS"}
WARN_CODES = {"TOO_GOOD", "LOCKED_ALLOWED", "CHEAPER_ELSEWHERE", "ABOVE_TG_VALUE", "CONFIRM_ABOVE", "FEW_COMPS",
              "DRY_RUN", "SHADOW_RULE", "MUTED", "STALE_DATA"}


def reason_text(code: str | None) -> str:
    if not code:
        return "no reason recorded"
    return REASON_TEXT.get(code, code.replace("_", " ").lower())


def suite_code(code: str) -> str:
    return SUITE_CODE.get(code, code)


def severity(code: str) -> str:
    if code in GOOD_CODES:
        return "good"
    return "warn" if code in WARN_CODES else "bad"


OUTCOME_WORD = {
    "skip": "Skipped",
    "alert": "Alerted",
    "dry": "Practice buy",
    "bought": "Bought",
    "failed": "Buy failed",
    "muted": "Muted",
    "near": "Near miss",
    "tap": "Tapped",
}


def _clock(ts: float, tz_offset_hours: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=tz_offset_hours))).strftime("%H:%M")


def why_line(d: "Decision", tz_offset_hours: int = 0) -> str:
    """'Skipped 14:02: price 5.2 TON > your max 5.0 · floor 5.4 (low, 2 listings)'."""
    det = d.detail or {}
    text = det.get("text") or reason_text(d.reason_code)
    line = f"{OUTCOME_WORD.get(d.outcome, d.outcome.title())} {_clock(d.ts, tz_offset_hours)}: {text}"
    if det.get("floor") is not None:
        n = plural(int(det.get("n") or 0), "listing")
        line += f" · floor {det['floor']:.2f} ({det.get('confidence', '?')}, {n})"
    if det.get("profit_ton") is not None:
        line += f" · net {det['profit_ton']:+.2f} TON"
    if d.rule_id is not None:
        line += f" · rule #{d.rule_id}"
    return line


def why_text(decisions: list["Decision"], *, title: str | None = None, tz_offset_hours: int = 0) -> str:
    if not decisions:
        return "No decision recorded for this listing (yet). /why needs a listing id, a t.me/nft link or a reply to a card."
    head = title or (decisions[0].detail or {}).get("title") or decisions[0].listing_id
    lines = [f"Why: {head}"]
    lines += [f"• {why_line(d, tz_offset_hours)}" for d in decisions]
    return "\n".join(lines)


# --- optional LLM explanation -------------------------------------------------------

LLM_SYSTEM = (
    "You explain decisions of a Telegram gift sniping bot to its owner. Use plain English, at most 4 short "
    "sentences, no markdown. Mention the net numbers (after fees) and the main risk."
)
# Models that accept the server-side `fallbacks: "default"` mode.
FALLBACK_DEFAULT_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}

Caller = Callable[[str, str, str], Awaitable[str]]


def llm_available(settings: "Settings") -> bool:
    """[Explain more] is shown only with ANTHROPIC_API_KEY set and the anthropic package installed."""
    if not settings.ANTHROPIC_API_KEY:
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


async def _anthropic_call(api_key: str, model: str, prompt: str) -> str:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    kwargs: dict[str, Any] = {}
    if model in FALLBACK_DEFAULT_MODELS:
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = await client.beta.messages.create(
        model=model,
        max_tokens=1024,
        system=LLM_SYSTEM,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    if resp.stop_reason == "refusal":
        return "The model declined to explain this one."
    return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()


async def explain_more(event: dict[str, Any], settings: "Settings", call: Caller | None = None) -> str | None:
    """Plain-language explanation of one event. Without ANTHROPIC_API_KEY: None and no network call.
    The event JSON is the only payload; the key never goes into it."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    payload = json.dumps(event, ensure_ascii=False, default=str)
    prompt = f"Explain this decision in plain words:\n{payload}"
    try:
        return await (call or _anthropic_call)(settings.ANTHROPIC_API_KEY, settings.ANTHROPIC_MODEL, prompt)
    except Exception as exc:  # optional feature: never break the bot
        log.warning("explain_more failed: %s", type(exc).__name__)
        return None
