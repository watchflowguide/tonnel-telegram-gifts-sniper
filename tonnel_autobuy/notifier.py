"""Notifications to admins via the aiogram bot, or stdout when BOT_TOKEN is empty.

Deal cards (HTML parse mode) follow the suite-wide layout: PRACTICE/LIVE tag + item, a compact
block of net numbers, a one-line Risk note, a <blockquote expandable> 'Why' section and inline
buttons (main action, Why?, Skip/Mute). The plain form of every card has no tags.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .callbacks import DealCB, NearCB
from .fx import usd
from .models import Listing, plural
from .pricing import DealMath, deal_summary

if TYPE_CHECKING:
    from aiogram import Bot

    from .peers import PeerView
    from .rules import BuyRule, NearMiss

log = logging.getLogger(__name__)


def format_skip(listing: Listing, reason: str) -> str:
    return f"SKIP ({reason}): {listing.title()}"


def format_buy_ok(listing: Listing, cost: float, fee_pct: float, detail: str = "") -> str:
    tail = f" ({detail})" if detail else ""
    return (f"BOUGHT: {listing.title()} - {listing.price_ton:g} TON (cost {cost:.2f} incl. {fee_pct:g}% fee)"
            f"{tail}")


def format_buy_failed(listing: Listing, detail: str, *, uncertain: bool = False) -> str:
    if uncertain:
        return f"BUY UNCERTAIN (check manually): {listing.title()} - {detail}"
    return f"BUY FAILED: {listing.title()} - {detail}"


# --- deal cards -----------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def keyboard_labels(keyboard: InlineKeyboardMarkup | None) -> list[str]:
    if keyboard is None:
        return []
    return [b.text for row in keyboard.inline_keyboard for b in row]


def keyboard_callbacks(keyboard: InlineKeyboardMarkup | None) -> list[str]:
    if keyboard is None:
        return []
    return [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]


@dataclass(slots=True)
class RenderedCard:
    html: str
    plain: str
    keyboard: InlineKeyboardMarkup | None
    summary: str = ""


@dataclass(slots=True)
class DealView:
    listing: Listing
    deal: DealMath
    rule: "BuyRule"
    mode: str  # effective mode: auto | confirm | watch
    status: str
    practice: bool
    asking_first: list[str] = field(default_factory=list)
    peers: "PeerView | None" = None
    best_exit: tuple[str, float] | None = None
    usd_rate: float | None = None
    budget_left: float | None = None
    buys_left: int | None = None
    token: str | None = None
    market_bot: str = "Tonnel_Network_bot"
    market_title: str = "Tonnel"
    market: str = "tonnel"
    fees_verified: bool = False
    llm: bool = False
    now: float = 0.0
    shadow: bool = False
    pending: bool = False  # Buy/Skip buttons still active


def rarity_line(listing: Listing) -> str | None:
    """Attributes with Tonnel's rarity suffix as sent in the raw strings, e.g. 'Wizard (1.5%) · Black (1%)'."""
    raw = listing.raw or {}
    parts = [str(raw.get(attr) or getattr(listing, attr)) for attr in ("model", "backdrop", "symbol")
             if raw.get(attr) or getattr(listing, attr)]
    return " · ".join(parts) or None


def _disc(d: float | None) -> str:
    if d is None:
        return "no floor"
    return f"−{d:.0f}%" if d >= 0 else f"+{-d:.0f}%"


def risk_line(view: DealView) -> str:
    return deal_risk(view.deal, view.fees_verified)


def deal_risk(deal: DealMath, fees_verified: bool) -> str:
    """The one-line Risk note shared by deal cards and near-miss cards."""
    too_good = deal.flag("TOO_GOOD")
    if too_good:
        return f"Risk: {too_good.text}: could be a fake discount or a trap."
    if deal.lock_days:
        return f"Risk: locked for {deal.lock_days:.0f} d, you cannot resell until then."
    if deal.confidence == "low":
        return f"Risk: thin floor, only {plural(deal.n, 'comparable listing')}."
    if deal.note:
        return f"Risk: the floor moved fast ({deal.note})."
    if not fees_verified:
        return "Risk: fee rates are assumptions until FEES_VERIFIED=true; the floor can move before you resell."
    return "Risk: the floor can move before you resell."


def why_words(view: DealView) -> str:
    deal, rule = view.deal, view.rule
    parts = floor_words(deal)
    limits = [f"max {rule.max_price_ton:g} TON"]
    if rule.min_discount_pct:
        limits.append(f"discount ≥ {rule.min_discount_pct:g}%")
    if rule.min_profit_ton:
        limits.append(f"profit ≥ {rule.min_profit_ton:g} TON")
    parts.append(f"It passes rule #{rule.id} ({', '.join(limits)}, mode {rule.mode}).")
    return " ".join(parts)


def floor_words(deal: DealMath) -> list[str]:
    """Plain-language floor and net-exit sentences shared by deal cards and near-miss cards."""
    strength = {"high": "solid", "med": "fair", "low": "thin"}.get(deal.confidence, deal.confidence)
    parts = []
    if deal.floor is not None and deal.discount_pct is not None:
        parts.append(
            f"Listed {abs(deal.discount_pct):.0f}% {'under' if deal.discount_pct >= 0 else 'over'} a {strength} floor of "
            f"{deal.floor:.2f} TON ({plural(deal.n, 'comparable listing')}, {deal.floor_source})."
        )
    if deal.net_exit is not None and deal.profit_ton is not None:
        wd = f" and {deal.fees.withdraw_ton:g} TON withdraw" if deal.fees.withdraw_ton else ""
        parts.append(
            f"After the {deal.fees.sell_pct:g}% sell fee{wd}, reselling at the floor gives {deal.net_exit:.2f} TON: "
            f"{deal.profit_ton:+.2f} TON net."
        )
    return parts


def deal_keyboard(view: DealView, *, final: bool = False) -> InlineKeyboardMarkup | None:
    kb = InlineKeyboardBuilder()
    deal = view.deal
    if view.token and view.mode == "confirm" and view.pending and not final:
        kb.row(InlineKeyboardButton(text=f"Buy {deal.cost:.2f} TON", style="success",
                                    callback_data=DealCB(action="b", token=view.token).pack()))
    if view.token:
        row = []
        if view.pending and not final:
            row.append(InlineKeyboardButton(text="Skip", callback_data=DealCB(action="s", token=view.token).pack()))
        row.append(InlineKeyboardButton(text="Mute 24h", callback_data=DealCB(action="m", token=view.token).pack()))
        row.append(InlineKeyboardButton(text="Why?", callback_data=DealCB(action="w", token=view.token).pack()))
        if view.llm:
            row.append(InlineKeyboardButton(text="Explain more", callback_data=DealCB(action="x", token=view.token).pack()))
        kb.row(*row)
    links = []
    if view.listing.url:
        links.append(InlineKeyboardButton(text="Open gift", url=view.listing.url))
    links.append(InlineKeyboardButton(text=f"Open {view.market_title}", url=f"https://t.me/{view.market_bot}"))
    kb.row(*links)
    return kb.as_markup()


STATUS_SLOT = "⁣STATUS⁣"  # replaced on edits so a stored card can be re-rendered


def render_deal_card(view: DealView, *, with_slot: bool = False) -> RenderedCard:
    listing, deal = view.listing, view.deal
    lines: list[tuple[str, str]] = []  # (html, plain)

    def add(h: str, p: str | None = None) -> None:
        lines.append((h, strip_html(h) if p is None else p))

    tag = "LIVE" if not view.practice else ("PRACTICE · shadow rule" if view.shadow else "PRACTICE")
    add(f"<b>{tag}</b> · <b>{esc(listing.short_title)}</b> — {deal.price:.2f} TON ({_disc(deal.discount_pct)})")
    rare = rarity_line(listing)
    if rare:
        add(esc(rare))
    money = f"You pay {deal.cost:.2f} TON (incl. {deal.fees.buy_pct:g}% fee)"
    cash = usd(deal.cost, view.usd_rate)
    if cash:
        money += f" {cash}"
    if deal.floor is not None:
        money += f" · Floor {deal.floor:.2f} ({deal.confidence}, {plural(deal.n, 'comparable')})"
    else:
        money += " · no floor yet"
    add(esc(money))
    if deal.profit_ton is not None and deal.profit_pct is not None and deal.break_even_price is not None:
        add(esc(f"If resold at floor: {deal.profit_ton:+.2f} TON net ({deal.profit_pct:+.0f}%) · "
                f"break-even {deal.break_even_price:.2f}"))
    if view.best_exit and view.best_exit[0] != view.market:
        from .peers import MARKET_TITLES

        mkt, net = view.best_exit
        add(esc(f"Best exit: {MARKET_TITLES.get(mkt, mkt)} ~{net:.2f} → {net - deal.cost:+.2f} TON net"))
    if view.peers is not None:
        line = view.peers.elsewhere_line()
        if line:
            add(esc(line))
        if view.peers.tg is not None:
            add(esc(view.peers.tg.line(view.now)))
    badges = []
    if deal.lock_days:
        badges.append("🔒 unlocks in ? d" if deal.lock_days == float("inf") else f"🔒 unlocks in {deal.lock_days:.0f} d")
    if deal.has("TOO_GOOD"):
        badges.append("⚠ too good to be true")
    if badges:
        add(" · ".join(badges))
    if view.asking_first:
        add(esc("Asking first: " + "; ".join(view.asking_first)))
    add(esc(risk_line(view)))
    status_html = f"<b>{STATUS_SLOT if with_slot else esc(view.status)}</b>"
    lines.append((status_html, STATUS_SLOT if with_slot else view.status))
    budget = f"{view.budget_left:.1f} TON left today" if view.budget_left is not None else ""
    buys = f"{plural(view.buys_left, 'buy')} left this hour" if view.buys_left is not None else ""
    meta = " · ".join(x for x in (f"Rule #{view.rule.id}", budget, buys, f"floor source: {deal.floor_source}"
                                  + (f" ({deal.note})" if deal.note else "")) if x)
    why = esc(why_words(view))
    body_html = "\n".join(h for h, _ in lines) + f"\n<blockquote expandable>Why: {why}\n{esc(meta)}</blockquote>"
    summary = "DEAL " + deal_summary(listing.short_title, deal)
    plain = "\n".join([summary] + [p for _, p in lines] + [f"Why: {why_words(view)}", meta])
    return RenderedCard(body_html, plain, deal_keyboard(view), summary)


def fill_status(text: str, status: str, *, is_html: bool) -> str:
    return text.replace(STATUS_SLOT, esc(status) if is_html else status)


def render_near_miss(listing: Listing, deal: DealMath, rule: "BuyRule", miss: "NearMiss", token: str,
                     *, max_cap: float, practice: bool, fees_verified: bool = False) -> RenderedCard:
    """Same layout as a deal card: tag + item, net numbers, Risk line, <blockquote expandable> Why, and the
    buttons [Raise max] (main action, only within MAX_PRICE_TON) · [Ignore today] · [Why?] · [Mute 24h]."""
    tag = "PRACTICE" if practice else "LIVE"
    head = f"<b>{tag}</b> · <b>{esc(listing.short_title)}</b> — {deal.price:.2f} TON ({_disc(deal.discount_pct)})"
    body = [head, esc(miss.text())]
    if deal.floor is not None and deal.profit_ton is not None:
        body.append(esc(f"You pay {deal.cost:.2f} TON (incl. {deal.fees.buy_pct:g}% fee) · Floor {deal.floor:.2f} "
                        f"({deal.confidence}, {plural(deal.n, 'comparable')}) · resold at floor: "
                        f"{deal.profit_ton:+.2f} TON net"))
    else:
        body.append(esc(f"You pay {deal.cost:.2f} TON (incl. {deal.fees.buy_pct:g}% fee) · no floor yet"))
    body.append(esc(deal_risk(deal, fees_verified)))
    why = " ".join(floor_words(deal) + [f"It misses rule #{rule.id} by {miss.miss_pct:.1f}%, so nothing was bought."])
    meta = f"Rule #{rule.id} · near-miss nudges are silent and capped per hour · floor source: {deal.floor_source}"
    kb = InlineKeyboardBuilder()
    buttons = []
    if miss.check == "max" and miss.new_max is not None and miss.new_max <= max_cap + 1e-9:
        buttons.append(InlineKeyboardButton(text=f"Raise max to {miss.new_max:.2f}",
                                            callback_data=NearCB(action="r", token=token).pack()))
    buttons.append(InlineKeyboardButton(text="Ignore today", callback_data=NearCB(action="i", token=token).pack()))
    kb.row(*buttons)
    kb.row(InlineKeyboardButton(text="Why?", callback_data=DealCB(action="w", token=token).pack()),
           InlineKeyboardButton(text="Mute 24h", callback_data=DealCB(action="m", token=token).pack()))
    html_text = "\n".join(body) + f"\n<blockquote expandable>Why: {esc(why)}\n{esc(meta)}</blockquote>"
    plain = "NEAR MISS " + "\n".join([strip_html(x) for x in body] + [f"Why: {why}", meta])
    return RenderedCard(html_text, plain, kb.as_markup(), "NEAR MISS")


# --- notifier --------------------------------------------------------------------------------

class Notifier:
    def __init__(self, bot: "Bot | None" = None, admin_ids: list[int] | None = None) -> None:
        self.bot = bot
        self.admin_ids = list(admin_ids or [])

    @property
    def chat_ids(self) -> list[int]:
        return self.admin_ids

    @property
    def to_stdout(self) -> bool:
        return self.bot is None or not self.admin_ids

    @staticmethod
    def _print(prefix: str, body: str, keyboard: InlineKeyboardMarkup | None = None) -> None:
        first, *rest = body.split("\n")
        print(f"{prefix} {first}", flush=True)
        for line in rest:
            print(f"  {line}", flush=True)
        labels = keyboard_labels(keyboard)
        if labels:
            print(f"  [buttons: {' | '.join(labels)}]", flush=True)

    async def send(
        self,
        text: str,
        *,
        html: bool = False,
        keyboard: InlineKeyboardMarkup | None = None,
        silent: bool = False,
        plain: str | None = None,
    ) -> list[tuple[int, int]]:
        """Send to every admin; returns (chat_id, message_id) pairs for later edits."""
        if self.to_stdout:
            body = plain if plain is not None else (strip_html(text) if html else text)
            self._print("[notify]", body, keyboard)
            return []
        from aiogram.types import LinkPreviewOptions

        log.info("notify: %s", (plain or text).split("\n", 1)[0])
        assert self.bot is not None
        sent: list[tuple[int, int]] = []
        for chat_id in self.admin_ids:
            try:
                msg = await self.bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML" if html else None,
                    reply_markup=keyboard,
                    disable_notification=silent,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                sent.append((chat_id, msg.message_id))
            except Exception as exc:  # never let a notification break the sniping loop
                log.warning("failed to notify %s: %s", chat_id, exc)
        return sent

    async def send_card(self, card: RenderedCard, *, silent: bool = False) -> list[tuple[int, int]]:
        return await self.send(card.html, html=True, keyboard=card.keyboard, silent=silent, plain=card.plain)

    async def edit(
        self,
        msgs: list[tuple[int, int]],
        text: str,
        *,
        html: bool = False,
        keyboard: InlineKeyboardMarkup | None = None,
        note: str | None = None,
    ) -> None:
        """Edit earlier messages (deal cards change their status line)."""
        if self.to_stdout:
            print(f"[edit card] {note or strip_html(text).splitlines()[0]}", flush=True)
            return
        assert self.bot is not None
        for chat_id, message_id in msgs:
            try:
                await self.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, parse_mode="HTML" if html else None,
                    reply_markup=keyboard,
                )
            except Exception as exc:
                log.warning("failed to edit %s/%s: %s", chat_id, message_id, exc)


class MemoryNotifier(Notifier):
    """Collects messages (used by tests). `messages` holds the plain text of every send."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []

    async def send(self, text: str, *, html: bool = False, keyboard: InlineKeyboardMarkup | None = None,
                   silent: bool = False, plain: str | None = None) -> list[tuple[int, int]]:
        body = plain if plain is not None else (strip_html(text) if html else text)
        self.messages.append(body)
        self.sent.append({"text": text, "html": html, "keyboard": keyboard, "silent": silent, "plain": body})
        return [(1, len(self.sent))]

    async def edit(self, msgs: list[tuple[int, int]], text: str, *, html: bool = False,
                   keyboard: InlineKeyboardMarkup | None = None, note: str | None = None) -> None:
        self.edits.append({"msgs": msgs, "text": text, "html": html, "keyboard": keyboard, "note": note})
