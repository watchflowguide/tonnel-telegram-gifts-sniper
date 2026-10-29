"""Inline-button callback data. Every packed value stays well under Telegram's 64-byte limit.

Buttons that change state carry `t`, the unix second they were sent, and work only within a TTL (see
button_fresh). [Confirm resume] and a breaker's [Resume] also carry a one-time nonce that any later stop
(/pause, /panic, a breaker) voids, and a rule's crash [Resume] only works for the pause it was sent with,
so an old button can never undo a newer /panic or breaker. Deal and near-miss buttons expire through their
stored card instead; [Go live] through its one-time nonce.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

BUTTON_TTL_SEC = 600  # a confirm step ([Confirm resume], breaker [Resume], [Apply] preset, digest [Confirm])


def button_fresh(t: int, now: float, ttl: float) -> bool:
    """True when a button sent at unix second `t` may still act at `now`. Fails closed: t=0 (no time) is stale."""
    return t > 0 and -60 <= now - t <= ttl


class DealCB(CallbackData, prefix="d"):
    """Deal card: b=buy, s=skip, m=mute 24h, w=why, x=explain more (LLM). token = 8 chars."""

    action: str
    token: str


class NearCB(CallbackData, prefix="n"):
    """Near-miss card: r=raise max, i=ignore today."""

    action: str
    token: str


class RuleCB(CallbackData, prefix="r"):
    """Breaker on one rule: resume | keep. [Resume] works only while the crash pause it was sent with lasts,
    and never for a newer pause of the same rule."""

    action: str
    rule_id: int
    t: int = 0


class GateCB(CallbackData, prefix="g"):
    """Global switches: stay | resume (confirm /resume) | keep (stay paused). 'live' is legacy: see LiveCB.
    resume needs its one-time nonce `n` (Engine.issue_resume_nonce) and works for BUTTON_TTL_SEC at most."""

    action: str
    t: int = 0
    n: str = ""


class LiveCB(CallbackData, prefix="gl"):
    """[Go live for 24 h] from a /dry off report: a one-time nonce that /panic and /dry on void."""

    nonce: str


class ShadowCB(CallbackData, prefix="sh"):
    """Hindsight card of a shadow rule: live | more (keep testing 24 h) | del. Works for 24 h."""

    action: str
    rule_id: int
    t: int = 0


class PresetCB(CallbackData, prefix="ps"):
    """/preset confirmation: ok | no. ok works for BUTTON_TTL_SEC."""

    action: str
    name: str
    t: int = 0


class DigestCB(CallbackData, prefix="dg"):
    """Digest tuning hint: ask (show the confirm step) | ok (apply, for BUTTON_TTL_SEC) | keep.
    cents = the new max x 100."""

    action: str
    rule_id: int
    cents: int
    t: int = 0
