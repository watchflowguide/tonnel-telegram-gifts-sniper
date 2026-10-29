"""aiogram 3 admin bot. All commands and buttons are restricted to ADMIN_IDS."""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .callbacks import (BUTTON_TTL_SEC, DealCB, DigestCB, GateCB, LiveCB, NearCB, PresetCB, RuleCB, ShadowCB,
                        button_fresh)
from .client import MarketClient, SearchFilters
from .config import ConfigError, Settings
from .doctor import DoctorDeps, format_doctor, run_doctor
from .engine import DAY, HOUR, Engine
from .explain import reason_text, why_text
from .ledger import export_csv, format_pnl, format_score, rule_verdicts, scoreboard, summarize
from .models import norm, plural
from .peers import MARKET_TITLES, Peers, best_quote_for
from .pricing import estimate_floor
from .rules import ADDRULE_FORMAT, MODES, RuleParseError, parse_addrule_args
from .safety import PRESETS, preset_diff, preset_values, safety_text
from .storage import Storage
from .wizard import begin as wizard_begin
from .wizard import build_wizard_router, parse_nft_link

log = logging.getLogger(__name__)

BUY_STATUS_WORD = {"dry": "bought", "ok": "bought", "unknown": "result unknown", "failed": "failed",
                   "skipped": "not bought"}


def purchase_tag(dry: bool, status: str) -> str:
    """'PRACTICE bought', 'LIVE bought', 'LIVE result unknown', 'LIVE failed': the mode is always named."""
    return f"{'PRACTICE' if dry else 'LIVE'} {BUY_STATUS_WORD.get(status, status)}"

HELP = """Tonnel autobuy admin

Safety
/safety - mode, every limit, spend today/hour, live gate
/panic - stop all buying at once; /resume (with confirm) to undo
/dry on|off - practice/live. off shows the practice report first; LIVE then needs its
   one-time button (every rule ready) or /dry off confirm, and ends after LIVE_ARM_HOURS
/doctor - self-check with a fix per failing item
/status - engine state, top skip reasons, fee assumptions
/pause, /resume - stop/start sniping

Rules
/hunt - set up a hunt with buttons (no syntax), with a 24 h preview
/rules - list buy rules
/addrule <collection> max=<ton> [model=..] [backdrop=..] [symbol=..] [discount=<pct>]
   [profit=<ton>] [profit%=<pct>] [conf=low|med|high] [locked=<days>] [mode=auto|confirm|watch] [exit=here|best]
   (max= is the cost incl. the Tonnel fee; discount= is vs. the robust floor)
/delrule <id> - delete a rule
/mode <rule_id> auto|confirm|watch
/promote <rule_id> - move a shadow rule to live
/preset careful|normal|bold - tighten limits (asks first)
/mute <collection[:model]> [hours], /unmute <collection[:model]>

Results
/why <listing id | t.me/nft link> - or reply /why to a card
/scoreboard - practice results net of fees: the honest proof before going live
/pnl [days] - PRACTICE and LIVE P&L, net of fees
/score - verdict per rule
/sold <purchase_id> <price_ton> - close a position by hand
/export - CSV of purchases with their latest mark
/digest - preview of the daily digest
/buys - last purchases (dry and live)

Market
/balance - Tonnel TON balance
/floor <collection> - floor, cheapest listings, other markets, Telegram's value
/compare <collection> [model=<model>] - the same gift on every market, all-in and net
/peers - other-market sources and Telegram valuation status

Example: /addrule Lunar Snake max=2.5 discount=20 profit=0.2 mode=confirm"""

ADDRULE_USAGE = f"Usage: /addrule {ADDRULE_FORMAT}"

BOT_COMMANDS = [
    BotCommand(command="safety", description="Mode, limits and live gate"),
    BotCommand(command="panic", description="Stop all buying now"),
    BotCommand(command="hunt", description="Set up a hunt with buttons"),
    BotCommand(command="status", description="Engine state and limits"),
    BotCommand(command="scoreboard", description="Practice results net of fees"),
    BotCommand(command="why", description="Why did / didn't the bot act"),
    BotCommand(command="doctor", description="Self-check with fixes"),
    BotCommand(command="pnl", description="P&L, practice and live"),
    BotCommand(command="score", description="Verdict per rule"),
    BotCommand(command="dry", description="Practice/live switch"),
    BotCommand(command="rules", description="List buy rules"),
    BotCommand(command="addrule", description="Add a buy rule"),
    BotCommand(command="delrule", description="Delete a buy rule"),
    BotCommand(command="mode", description="Rule mode auto/confirm/watch"),
    BotCommand(command="pause", description="Pause sniping"),
    BotCommand(command="resume", description="Resume sniping"),
    BotCommand(command="floor", description="Collection floor"),
    BotCommand(command="compare", description="Same gift on other markets"),
    BotCommand(command="peers", description="Second-opinion sources"),
    BotCommand(command="digest", description="Daily digest preview"),
    BotCommand(command="mute", description="Mute a collection"),
    BotCommand(command="export", description="CSV of purchases"),
    BotCommand(command="balance", description="Tonnel balance"),
    BotCommand(command="buys", description="Last purchases"),
]


@dataclass(slots=True)
class BotContext:
    settings: Settings
    storage: Storage
    engine: Engine
    client: MarketClient
    peers: Peers | None = None
    tg_session: Any = None
    gift_lookup: Callable[[str], Awaitable[dict | None]] | None = None
    tg_probe: Callable[[], Awaitable[tuple[bool, bool | None]]] | None = None


AppContext = BotContext  # the name the wizard uses


class AdminFilter(Filter):
    def __init__(self, admin_ids: list[int]) -> None:
        self.admin_ids = set(admin_ids)

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        allowed = bool(user and user.id in self.admin_ids)
        if not allowed:
            log.info("ignored update from non-admin %s", user.id if user else None)
        return allowed


def parse_on_off(arg: str | None) -> bool | None:
    value = (arg or "").strip().lower().split(" ")[0] if arg else ""
    if value in ("on", "1", "true", "yes"):
        return True
    if value in ("off", "0", "false", "no"):
        return False
    return None


def mute_key(target: str) -> str:
    """'Toy Bear', 'toy bear (1%)' -> 'toy bear'; 'Toy Bear:Wizard' -> 'toy bear:wizard'. /mute and /unmute
    both use it, so any spelling that mutes also unmutes."""
    coll, _, model = target.strip().partition(":")
    return norm(coll) + (f":{norm(model)}" if model.strip() else "")


def _ago(ts: float | None, now: float | None = None) -> str:
    return "never" if ts is None else f"{(now or time.time()) - ts:.0f}s ago"


# --- shared texts (also printed by mock mode) -----------------------------------------------------


async def safety_for(ctx: AppContext) -> str:
    return safety_text(await ctx.engine.safety_state(), ctx.settings)


async def pnl_text(ctx: AppContext, days: float, *, html_mode: bool = True) -> str:
    e, now = ctx.engine, ctx.engine.clock()
    if now - e.last_mark_at > 600:
        await e.run_marks(now)
    positions = await ctx.storage.positions(since=now - days * DAY)
    practice = summarize(positions, practice=True, days=days, now=now, fees=e.fees)
    live = summarize(positions, practice=False, days=days, now=now, fees=e.fees)
    return format_pnl(practice, live, days, html=html_mode)


async def score_text(ctx: AppContext) -> str:
    rules = await ctx.storage.list_rules(enabled_only=True)
    verdicts = await rule_verdicts(ctx.storage, rules, ctx.settings, ctx.engine.clock(), ctx.engine.fees)
    return format_score(rules, verdicts)


def start_intro(ctx: AppContext) -> str:
    """/start: what the bot does in one line, and three commands to try first."""
    fee = ctx.engine.fees.buy_pct
    return (f"I watch Tonnel for gifts listed under a well-backed floor, count the {fee:g}% buy fee, and buy "
            "only when the net numbers hold up. Practice mode buys nothing real until you go live.\n"
            "Try first: /hunt (a rule in a few taps) · /scoreboard (what practice would have made) · "
            "/safety (mode and limits)")


async def dry_off_gate(ctx: AppContext, confirm: bool, *, preview: bool = False) -> tuple[str, InlineKeyboardMarkup | None]:
    """/dry off: the practice report first; Go-live only when every enabled rule is 'ready'.
    preview=True (mock) shows the answer as it would be with DRY_RUN=false and never arms anything."""
    e, now = ctx.engine, ctx.engine.clock()
    rules = await ctx.storage.list_rules(enabled_only=True)
    verdicts = await rule_verdicts(ctx.storage, rules, ctx.settings, now, e.fees)
    score = format_score(rules, verdicts)
    if preview:
        confirm = False
    elif e.live_locked:
        return (f"Live mode is locked: DRY_RUN=true in .env. Set DRY_RUN=false and restart to allow /dry off.\n"
                f"Gate preview: {score}"), None
    if await e.is_panic() and not preview:
        return "Everything is stopped by /panic. Use /resume first, then /dry off.", None
    hours = ctx.settings.LIVE_ARM_HOURS
    if confirm:
        error = await e.arm_live(now)
        return (error or f"LIVE for {hours:g} h. Then I switch back to practice by myself. /panic stops everything."), None
    report = await pnl_text(ctx, ctx.settings.GATE_DAYS, html_mode=False)
    ready = bool(rules) and all(verdicts.get(r.id or 0) == "ready" for r in rules)
    kb = InlineKeyboardBuilder()
    if ready:
        nonce = "preview" if preview else await e.issue_gate_nonce(now)
        kb.button(text=f"Go live for {hours:g} h", callback_data=LiveCB(nonce=nonce))
        tail = score
    else:
        tail = f"{score}\nNot every rule is proven yet: type /dry off confirm to go live anyway."
    kb.button(text="Stay in practice", callback_data=GateCB(action="stay"))
    return f"{report}\n\n{tail}", kb.as_markup()


async def go_live_button(ctx: AppContext, nonce: str) -> str | None:
    """[Go live for 24 h]: works once, within GATE_TTL_SEC of its /dry off report, never after /panic or
    /dry on, and only while every enabled rule is still 'ready'. Returns an error text, or None when LIVE."""
    e, now = ctx.engine, ctx.engine.clock()
    if not await e.consume_gate_nonce(nonce, now):
        return "This button is out of date. Send /dry off again for a fresh report."
    rules = await ctx.storage.list_rules(enabled_only=True)
    verdicts = await rule_verdicts(ctx.storage, rules, ctx.settings, now, e.fees)
    if not (rules and all(verdicts.get(r.id or 0) == "ready" for r in rules)):
        return "Not every rule is proven any more. Send /dry off again (or /dry off confirm)."
    return await e.arm_live(now)


async def why_for(ctx: AppContext, arg: str, reply_to: Message | None = None) -> str:
    storage, tz = ctx.storage, ctx.settings.TZ_OFFSET_HOURS
    if reply_to is not None and not arg:
        card = await storage.pending_by_message(reply_to.chat.id, reply_to.message_id)
        if card is None:
            return "That message is not a deal card I remember."
        return why_text(await storage.decisions_for(card.listing_id), title=card.payload.get("title"), tz_offset_hours=tz)
    if not arg:
        return "Usage: /why <gift number | listing id | t.me/nft link>, or reply /why to a card."
    link = parse_nft_link(arg)
    if link:
        ids = await storage.listing_ids_for(link[0], link[1])
        decisions = []
        for lid in ids:
            decisions += await storage.decisions_for(lid)
        decisions.sort(key=lambda d: d.ts, reverse=True)
        return why_text(decisions[:10], title=f"{link[0]} #{link[1]}", tz_offset_hours=tz)
    card = await storage.get_pending(arg)
    listing_id = card.listing_id if card else arg
    decisions = await storage.decisions_for(listing_id)
    number = arg.lstrip("#")
    if not decisions and number.isdigit():
        # Cards and skip lines show the gift number (#4412), not Tonnel's internal gift_id: resolve it.
        seen = await storage.listings_with_number(int(number))
        for lid, _ in seen:
            decisions += await storage.decisions_for(lid)
        decisions.sort(key=lambda d: d.ts, reverse=True)
        names = sorted({c for _, c in seen if c})
        title = f"{' / '.join(names)} #{number}" if names else None
        return why_text(decisions[:10], title=title, tz_offset_hours=tz)
    return why_text(decisions, tz_offset_hours=tz)


OUT_OF_DATE = "This button is out of date."


async def digest_action(ctx: AppContext, action: str, rule_id: int, cents: int, t: int = 0):
    """Digest [Raise to X] [Keep]: 'ask' only shows a confirm step, 'ok' applies it (capped at MAX_PRICE_TON)
    when its button was sent less than BUTTON_TTL_SEC ago."""
    from .digest import apply_raise, confirm_keyboard

    new_max = cents / 100
    now = ctx.engine.clock()
    if action == "ask":
        rule = await ctx.storage.get_rule(rule_id)
        if rule is None:
            return f"Rule #{rule_id} not found", None
        return (f"Raise rule #{rule_id} max {rule.max_price_ton:g} → {new_max:.2f} TON? "
                f"MAX_PRICE_TON {(await ctx.engine.effective_limits()).max_price_ton:g} still caps every buy."), \
            confirm_keyboard(rule_id, cents, now)
    if action == "ok":
        if not button_fresh(t, now, BUTTON_TTL_SEC):
            return f"{OUT_OF_DATE} Nothing changed; use /mode or /addrule to edit the rule.", None
        return (await apply_raise(ctx.engine, rule_id, new_max))[1], None
    return f"Rule #{rule_id} kept as it is", None


async def resume_prompt(ctx: AppContext) -> tuple[str, InlineKeyboardMarkup]:
    """/resume: a confirm step. Its [Confirm resume] works once, for BUTTON_TTL_SEC, and only until the next
    stop: a later /pause, /panic or breaker voids it."""
    e = ctx.engine
    now = e.clock()
    t, nonce = int(now), await e.issue_resume_nonce(now)
    kb = InlineKeyboardBuilder()
    kb.button(text="Confirm resume", callback_data=GateCB(action="resume", t=t, n=nonce))
    kb.button(text="Keep paused", callback_data=GateCB(action="keep", t=t))
    extra = " /panic is on: resuming keeps practice mode until a new /dry off." if await e.is_panic() else ""
    return f"Resume sniping?{extra}", kb.as_markup()


async def gate_action(ctx: AppContext, action: str, t: int = 0, nonce: str = "") -> str:
    """[Confirm resume] (after /resume) and the breaker's [Resume]: one-time, for BUTTON_TTL_SEC at most, and
    voided by any later stop, so an old button can never undo a newer /panic or breaker pause."""
    e = ctx.engine
    if action == "live":  # a button from before one-time go-live tokens: never arms LIVE
        return "This button is out of date. Send /dry off again for a fresh report."
    if action == "resume":
        now = e.clock()
        if not button_fresh(t, now, BUTTON_TTL_SEC) or not await e.resume_with(nonce, now):
            if await e.is_paused():
                return f"{OUT_OF_DATE} Still paused: send /resume again."
            return f"{OUT_OF_DATE} Nothing changed."
        if await e.is_dry():
            return "Resumed. Mode stays practice until a new /dry off."
        return "Resumed. LIVE mode continues until its gate ends (/safety)."
    return "OK, nothing changed"


async def rule_action(ctx: AppContext, action: str, rule_id: int, t: int = 0) -> str:
    """Floor-crash breaker buttons. [Resume] works only while the pause it was sent with still runs."""
    if action != "resume":
        return f"Rule #{rule_id} stays paused"
    if not button_fresh(t, ctx.engine.clock(), ctx.settings.CRASH_PAUSE_MIN * 60):
        return f"{OUT_OF_DATE} Rule #{rule_id} is not resumed by it."
    paused_at = await ctx.storage.get_float(f"crash_alert:{rule_id}")
    if paused_at is not None and paused_at >= t + 1:  # a newer crash pause than the one this button belongs to
        return f"{OUT_OF_DATE} Rule #{rule_id} is not resumed by it."
    await ctx.storage.delete_setting(f"rule_paused_until:{rule_id}")
    return f"Rule #{rule_id} resumed"


async def shadow_action(ctx: AppContext, action: str, rule_id: int, t: int = 0) -> str:
    """Hindsight card of a shadow rule: [Go live] [Keep testing 24h] [Delete], valid for 24 h."""
    now = ctx.engine.clock()
    if not button_fresh(t, now, DAY):
        return f"{OUT_OF_DATE} Nothing changed: /promote {rule_id} goes live, /delrule {rule_id} deletes."
    if action == "live":
        return await ctx.engine.promote(rule_id)
    if action == "more":
        await ctx.storage.set_setting(f"shadow_due:{rule_id}", now + DAY)
        return f"Rule #{rule_id} keeps practising for 24 h more."
    return f"Rule #{rule_id} deleted." if await ctx.storage.delete_rule(rule_id) else f"Rule #{rule_id} not found."


async def preset_action(ctx: AppContext, action: str, name: str, t: int = 0) -> str:
    """/preset [Apply]: works for BUTTON_TTL_SEC after the preview was sent."""
    if action != "ok" or name not in PRESETS:
        return "Preset not applied"
    if not button_fresh(t, ctx.engine.clock(), BUTTON_TTL_SEC):
        return f"{OUT_OF_DATE} Preset not applied: send /preset {name} again."
    for key, value in preset_values(name, ctx.settings).items():
        await ctx.storage.set_setting(f"limit:{key}", value)
    return f"Preset {name} applied"


async def peers_text(ctx: AppContext) -> str:
    if ctx.peers is None:
        return "Second opinion is off: set PEER_FLOOR_FILES and/or a Telegram session (TG_VALUE_ENABLED)."
    return "\n".join(await ctx.peers.status_lines())


async def compare_text(ctx: AppContext, collection: str, model: str | None) -> str:
    from .engine import is_floor_pool_member

    e, now = ctx.engine, ctx.engine.clock()
    try:
        pool = await ctx.client.search_listings(SearchFilters(collection=collection, model=model, sort="price_asc",
                                                              limit=ctx.settings.PAGE_LIMIT))
    except Exception as exc:  # noqa: BLE001
        return f"Cannot load {collection}: {exc}"
    pool = [x for x in pool if is_floor_pool_member(x, now, ctx.settings.TONNEL_ASSET, ctx.settings.EXPORT_AT_LOCKS)]
    fees = e.fees_for(collection)
    own = estimate_floor(None, pool, None, [], now)
    rows = [f"{'Market':<9}{'Floor':>8}{'All-in':>8}{'Net exit':>9}"]
    if own.value is not None:
        rows.append(f"{'Tonnel':<9}{own.value:>8.2f}{fees.cost(own.value):>8.2f}{fees.net_exit(own.value):>9.2f}")
    if ctx.peers is not None:
        level = "model" if model else "collection"
        for src in await ctx.peers.sources(now):
            q = best_quote_for(src, collection, model, level) if src.status == "ok" else None
            if q:
                rows.append(f"{q.title:<9}{q.floor:>8.2f}{q.all_in_cost:>8.2f}{q.net_if_sold:>9.2f}")
            elif src.status != "own":
                rows.append(f"{MARKET_TITLES.get(src.market, src.market):<9}{'n/a':>8}  ({src.status})")
    if len(rows) == 1:
        return f"No data for {collection}."
    title = f"{collection}" + (f" / {model}" if model else "")
    return f"{title} (all-in = what you pay, net exit = what a resale at that floor leaves you)\n" + "\n".join(rows)


async def status_text(ctx: AppContext) -> str:
    e, s, now = ctx.engine, ctx.settings, ctx.engine.clock()
    rules = await ctx.storage.list_rules(enabled_only=True)
    reasons = await ctx.storage.reason_counts(now - DAY)
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:3]
    lines = [
        await safety_for(ctx),
        "",
        f"State: {'PAUSED' if await e.is_paused() else 'running'} · Client: {getattr(ctx.client, 'name', 'tonnel')}"
        f" · Active rules: {len(rules)}",
        f"Engine: {plural(e.cycles, 'cycle')}, last {_ago(e.last_cycle_ts, now)}, poll {s.POLL_INTERVAL:g}s, "
        f"delay {e.current_delay:.1f}s",
        f"Errors in a row: {e.consecutive_errors}" + (f" ({e.last_error})" if e.last_error else ""),
        "Top skip reasons (24 h): " + (" · ".join(f"{k} {v} ({reason_text(k)})" for k, v in top) or "none"),
        e.fees.describe(s.FEES_VERIFIED),
    ]
    return "\n".join(lines)


async def doctor_text(ctx: AppContext, *, cli: bool = False, service_running: bool = False) -> str:
    """/doctor in the bot, `doctor` on the CLI (cli=True: the engine does not run in this process)."""
    peers_status = ctx.peers.status_lines if ctx.peers is not None else None
    deps = DoctorDeps(ctx.settings, ctx.engine, ctx.tg_probe, ctx.settings.session_file,
                      peers_status, now=ctx.engine.clock(), cli=cli, service_running=service_running)
    return format_doctor(await run_doctor(deps))


# --- routers -----------------------------------------------------------------------------------------


def build_router(admin_ids: list[int]) -> Router:
    router = Router(name="admin")
    admin = AdminFilter(admin_ids)
    router.message.filter(admin)
    router.callback_query.filter(admin)

    @router.message(Command("start", "help"))
    async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
        await message.answer(start_intro(ctx) + "\n\n" + await safety_for(ctx) + "\n\n" + HELP)
        if not await ctx.storage.list_rules():
            await wizard_begin(message, state, ctx)

    @router.message(Command("status"))
    async def cmd_status(message: Message, ctx: AppContext) -> None:
        await message.answer(await status_text(ctx))

    @router.message(Command("safety"))
    async def cmd_safety(message: Message, ctx: AppContext) -> None:
        await message.answer(await safety_for(ctx))

    @router.message(Command("panic"))
    async def cmd_panic(message: Message, ctx: AppContext) -> None:
        n = await ctx.engine.panic()
        await message.answer(f"Stopped. {plural(n, 'pending card')} expired. Practice is forced until /resume "
                             "and /dry off.")

    @router.message(Command("pause"))
    async def cmd_pause(message: Message, ctx: AppContext) -> None:
        await ctx.engine.set_paused(True)
        await message.answer("Paused.")

    @router.message(Command("resume"))
    async def cmd_resume(message: Message, ctx: AppContext) -> None:
        text, kb = await resume_prompt(ctx)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("rules"))
    async def cmd_rules(message: Message, ctx: AppContext) -> None:
        rules = await ctx.storage.list_rules()
        if not rules:
            await message.answer("No rules yet. /hunt sets one up with buttons.\n" + ADDRULE_USAGE)
            return
        await message.answer("\n".join(f"{r.describe()} [{r.source}]" for r in rules))

    @router.message(Command("addrule"))
    async def cmd_addrule(message: Message, command: CommandObject, ctx: AppContext) -> None:
        try:
            rule = parse_addrule_args(command.args or "")
        except RuleParseError as exc:
            await message.answer(f"Cannot add rule: {exc}\n{ADDRULE_USAGE}")
            return
        if ctx.settings.SHADOW_NEW_RULES:
            rule.state = "shadow"
        await ctx.storage.add_rule(rule)
        if rule.state == "shadow":
            await ctx.storage.set_setting(f"shadow_since:{rule.id}", ctx.engine.clock())
        note = ""
        if rule.max_price_ton > ctx.settings.MAX_PRICE_TON:
            note = f"\nNote: MAX_PRICE_TON={ctx.settings.MAX_PRICE_TON:g} still caps every buy."
        if rule.state == "shadow":
            note += "\nIt starts in shadow: it practises until you promote it (/promote or the hindsight card)."
        await message.answer(f"Added rule {rule.describe()}{note}")

    @router.message(Command("delrule"))
    async def cmd_delrule(message: Message, command: CommandObject, ctx: AppContext) -> None:
        arg = (command.args or "").strip().lstrip("#")
        if not arg.isdigit():
            await message.answer("Usage: /delrule <id>")
            return
        ok = await ctx.storage.delete_rule(int(arg))
        await message.answer(f"Rule #{arg} deleted." if ok else f"Rule #{arg} not found.")

    @router.message(Command("mode"))
    async def cmd_mode(message: Message, command: CommandObject, ctx: AppContext) -> None:
        parts = (command.args or "").split()
        if len(parts) != 2 or not parts[0].lstrip("#").isdigit() or parts[1].lower() not in MODES:
            await message.answer("Usage: /mode <rule_id> auto|confirm|watch")
            return
        rid = int(parts[0].lstrip("#"))
        ok = await ctx.storage.update_rule(rid, mode=parts[1].lower())
        await message.answer(f"Rule #{rid} mode: {parts[1].lower()}." if ok else f"Rule #{rid} not found.")

    @router.message(Command("promote"))
    async def cmd_promote(message: Message, command: CommandObject, ctx: AppContext) -> None:
        arg = (command.args or "").strip().lstrip("#")
        if not arg.isdigit():
            await message.answer("Usage: /promote <rule_id>")
            return
        await message.answer(await ctx.engine.promote(int(arg)))

    @router.message(Command("dry"))
    async def cmd_dry(message: Message, command: CommandObject, ctx: AppContext) -> None:
        value = parse_on_off(command.args)
        if value is None:
            mode = "ON (practice)" if await ctx.engine.is_dry() else "OFF (LIVE)"
            await message.answer(f"Dry-run is {mode}. Usage: /dry on|off")
            return
        if value:
            await ctx.engine.set_dry(True)
            await message.answer("Dry-run ON: practice only, purchases are simulated.")
            return
        confirm = "confirm" in (command.args or "").lower().split()
        text, kb = await dry_off_gate(ctx, confirm)
        await message.answer(text, reply_markup=kb)

    @router.message(Command("balance"))
    async def cmd_balance(message: Message, ctx: AppContext) -> None:
        if not ctx.client.has_auth:
            await message.answer("No Tonnel auth configured (TG_API_ID/TG_API_HASH or TONNEL_INIT_DATA).")
            return
        try:
            balance = await ctx.client.balance()
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Balance error: {exc}")
            return
        await message.answer(f"Balance: {balance:.4f} TON")

    @router.message(Command("buys"))
    async def cmd_buys(message: Message, ctx: AppContext) -> None:
        rows = await ctx.storage.recent_purchases(10)
        if not rows:
            await message.answer("No purchases yet.")
            return
        lines = []
        for r in rows:
            when = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
            tag = purchase_tag(bool(r["dry"]), r["status"])
            num = f" #{r['number']}" if r["number"] is not None else ""
            lines.append(f"#{r['id']} {when} {tag} {r['collection']}{num} {r['price_ton']:g} TON "
                         f"(cost {r['cost_ton']:.2f} all-in)" + (f" ({r['message']})" if r["message"] else ""))
        await message.answer("\n".join(lines))

    @router.message(Command("floor"))
    async def cmd_floor(message: Message, command: CommandObject, ctx: AppContext) -> None:
        name = (command.args or "").strip()
        if not name:
            await message.answer("Usage: /floor <collection>")
            return
        lines = []
        floors = await ctx.engine.api_floors()
        if floors is not None:
            api = floors.get(name)
            lines.append(f"{name}: API floor {api:g} TON (raw, + {ctx.settings.BUY_FEE_PCT:g}% fee)"
                         if api is not None else f"{name}: API floor n/a")
        try:
            cheapest = await ctx.client.search_listings(SearchFilters(collection=name, sort="price_asc", limit=3))
        except Exception as exc:  # noqa: BLE001
            cheapest = []
            lines.append(f"Listings error: {exc}")
        for i, item in enumerate(cheapest, 1):
            lines.append(f"{i}. {item.title()} {item.price_ton:g} TON (cost {ctx.engine.fees.cost(item.price_ton):.2f})")
        if not cheapest and not any("error" in x for x in lines):
            lines.append("No listings found (check the collection name).")
        if ctx.peers is not None:
            now = ctx.engine.clock()
            for src in await ctx.peers.sources(now):
                q = best_quote_for(src, name, None, "collection") if src.status == "ok" else None
                if q:
                    lines.append(f"{q.title}: floor {q.floor:.2f} · {q.all_in_cost:.2f} all-in · {q.net_if_sold:.2f} net if sold")
                elif src.status != "own":
                    lines.append(f"{src.title}: n/a ({src.status})")
            if cheapest and ctx.peers.tg is not None:
                from .peers import parse_tg_value

                info = await ctx.peers.tg.get(cheapest[0].slug)
                if info:
                    rate = await ctx.peers.rate.usd_per_ton() if ctx.peers.rate else None
                    tg = parse_tg_value(info, rate)
                    lines.append(tg.line(now) + (f" · floor {tg.floor:g} {tg.currency}" if tg.floor else ""))
        await message.answer("\n".join(lines))

    @router.message(Command("compare"))
    async def cmd_compare(message: Message, command: CommandObject, ctx: AppContext) -> None:
        args = (command.args or "").strip()
        if not args:
            await message.answer("Usage: /compare <collection> [model=<model>]")
            return
        collection, _, model = args.partition("model=")
        text = await compare_text(ctx, collection.strip(), model.strip() or None)
        await message.answer(f"<pre>{html.escape(text)}</pre>", parse_mode="HTML")

    @router.message(Command("peers"))
    async def cmd_peers(message: Message, ctx: AppContext) -> None:
        await message.answer(await peers_text(ctx))

    @router.message(Command("why"))
    async def cmd_why(message: Message, command: CommandObject, ctx: AppContext) -> None:
        await message.answer(await why_for(ctx, (command.args or "").strip(), message.reply_to_message))

    @router.message(Command("doctor"))
    async def cmd_doctor(message: Message, ctx: AppContext) -> None:
        await message.answer(await doctor_text(ctx))

    @router.message(Command("scoreboard"))
    async def cmd_scoreboard(message: Message, ctx: AppContext) -> None:
        e = ctx.engine
        if e.clock() - e.last_mark_at > 600:
            await e.run_marks()
        rules = await ctx.storage.list_rules(enabled_only=True)
        await message.answer(await scoreboard(ctx.storage, ctx.settings, rules, e.clock(), e.fees))

    @router.message(Command("pnl"))
    async def cmd_pnl(message: Message, command: CommandObject, ctx: AppContext) -> None:
        arg = (command.args or "7").strip()
        days = float(arg) if arg.replace(".", "", 1).isdigit() else 7.0
        await message.answer(await pnl_text(ctx, days), parse_mode="HTML")

    @router.message(Command("score"))
    async def cmd_score(message: Message, ctx: AppContext) -> None:
        await message.answer(await score_text(ctx))

    @router.message(Command("sold"))
    async def cmd_sold(message: Message, command: CommandObject, ctx: AppContext) -> None:
        parts = (command.args or "").replace(",", ".").split()
        try:
            pid, price = int(parts[0].lstrip("#")), float(parts[1])
        except (IndexError, ValueError):
            await message.answer("Usage: /sold <purchase_id> <price_ton>")
            return
        ok = await ctx.storage.close_position(pid, price, "manual /sold", ctx.engine.clock())
        if not ok:
            await message.answer(f"Purchase #{pid} not found.")
            return
        pos = await ctx.storage.get_position(pid)
        net = ctx.engine.fees_for(pos.collection).net_exit(price) - pos.cost_ton if pos else 0.0
        await message.answer(f"Closed #{pid} at {price:g} TON: {net:+.2f} TON net after fees.")

    @router.message(Command("export"))
    async def cmd_export(message: Message, ctx: AppContext) -> None:
        data = export_csv(await ctx.storage.positions(), ctx.engine.fees).encode()
        await message.answer_document(BufferedInputFile(data, filename="tonnel_purchases.csv"),
                                      caption="Purchases with their latest mark (net of fees).")

    @router.message(Command("digest"))
    async def cmd_digest(message: Message, ctx: AppContext) -> None:
        from .digest import digest_card, hint_keyboard

        now = ctx.engine.clock()
        text, hint = await digest_card(ctx.engine, now - DAY, now, title="Digest, last 24 h")
        await message.answer(text, reply_markup=hint_keyboard(hint, now))

    @router.message(Command("mute"))
    async def cmd_mute(message: Message, command: CommandObject, ctx: AppContext) -> None:
        args = (command.args or "").strip()
        if not args:
            mutes = await ctx.storage.list_mutes(ctx.engine.clock())
            text = "\n".join(f"{k} until {datetime.fromtimestamp(u, tz=timezone.utc):%m-%d %H:%M} UTC" for k, u in mutes)
            await message.answer(text or "Nothing muted. Usage: /mute <collection[:model]> [hours]")
            return
        words = args.split()
        hours = 24.0
        if len(words) > 1 and words[-1].replace(".", "", 1).isdigit():
            hours = float(words.pop())
        key = mute_key(" ".join(words))
        await ctx.storage.add_mute(key, ctx.engine.clock() + hours * HOUR)
        await message.answer(f"Muted {key} for {hours:g} h. Auto buys still notify; decisions are still logged.")

    @router.message(Command("unmute"))
    async def cmd_unmute(message: Message, command: CommandObject, ctx: AppContext) -> None:
        target = (command.args or "").strip()
        if not target:
            await message.answer("Usage: /unmute <collection[:model]> (/mute lists the mutes)")
            return
        key = mute_key(target)
        ok = await ctx.storage.remove_mute(key)
        await message.answer(f"Unmuted {key}." if ok else f"{key} was not muted. /mute lists the mutes.")

    @router.message(Command("preset"))
    async def cmd_preset(message: Message, command: CommandObject, ctx: AppContext) -> None:
        name = (command.args or "").strip().lower()
        if name not in PRESETS:
            await message.answer("Usage: /preset careful|normal|bold")
            return
        limits = await ctx.engine.effective_limits()
        overrides = await ctx.storage.settings_with_prefix("limit:")
        current = {"MAX_PRICE_TON": limits.max_price_ton, "DAILY_BUDGET_TON": limits.daily_budget_ton,
                   "MAX_BUYS_PER_HOUR": limits.max_buys_per_hour,
                   "MIN_DISCOUNT_PCT": float(overrides.get("limit:MIN_DISCOUNT_PCT", 0))}
        kb = InlineKeyboardBuilder()
        t = int(ctx.engine.clock())
        kb.button(text="Apply", callback_data=PresetCB(action="ok", name=name, t=t))
        kb.button(text="Cancel", callback_data=PresetCB(action="no", name=name, t=t))
        await message.answer(f"Preset {name}:\n" + "\n".join(preset_diff(current, name, ctx.settings)),
                             reply_markup=kb.as_markup())

    # --- buttons ---

    @router.callback_query(DealCB.filter())
    async def on_deal(cb: CallbackQuery, callback_data: DealCB, ctx: AppContext) -> None:
        if callback_data.action == "b":
            await cb.answer("Checking…")
        result = await ctx.engine.tap(callback_data.action, callback_data.token, cb.from_user.id)
        if callback_data.action != "b":
            await cb.answer(result.text[:190])
        if isinstance(cb.message, Message):
            if result.reply:
                await cb.message.answer(result.reply)
            elif callback_data.action == "b":
                await cb.message.answer(result.text)

    @router.callback_query(NearCB.filter())
    async def on_near(cb: CallbackQuery, callback_data: NearCB, ctx: AppContext) -> None:
        result = await ctx.engine.near_action(callback_data.action, callback_data.token, cb.from_user.id)
        await cb.answer(result.text[:190], show_alert=not result.ok)

    @router.callback_query(RuleCB.filter())
    async def on_rule(cb: CallbackQuery, callback_data: RuleCB, ctx: AppContext) -> None:
        text = await rule_action(ctx, callback_data.action, callback_data.rule_id, callback_data.t)
        await cb.answer(text, show_alert=text.startswith(OUT_OF_DATE))

    @router.callback_query(LiveCB.filter())
    async def on_live(cb: CallbackQuery, callback_data: LiveCB, ctx: AppContext) -> None:
        error = await go_live_button(ctx, callback_data.nonce)
        hours = ctx.settings.LIVE_ARM_HOURS
        await cb.answer(error or f"LIVE for {hours:g} h", show_alert=bool(error))

    @router.callback_query(GateCB.filter())
    async def on_gate(cb: CallbackQuery, callback_data: GateCB, ctx: AppContext) -> None:
        text = await gate_action(ctx, callback_data.action, callback_data.t, callback_data.n)
        stale = text.startswith(OUT_OF_DATE) or callback_data.action == "live"
        await cb.answer(text[:190], show_alert=stale)
        if callback_data.action == "resume" and not stale and isinstance(cb.message, Message):
            await cb.message.answer(text)

    @router.callback_query(ShadowCB.filter())
    async def on_shadow(cb: CallbackQuery, callback_data: ShadowCB, ctx: AppContext) -> None:
        text = await shadow_action(ctx, callback_data.action, callback_data.rule_id, callback_data.t)
        await cb.answer(text[:190], show_alert=text.startswith(OUT_OF_DATE))

    @router.callback_query(DigestCB.filter())
    async def on_digest(cb: CallbackQuery, callback_data: DigestCB, ctx: AppContext) -> None:
        text, kb = await digest_action(ctx, callback_data.action, callback_data.rule_id, callback_data.cents,
                                       callback_data.t)
        await cb.answer(text[:190])
        if kb is not None and isinstance(cb.message, Message):
            await cb.message.answer(text, reply_markup=kb)

    @router.callback_query(PresetCB.filter())
    async def on_preset(cb: CallbackQuery, callback_data: PresetCB, ctx: AppContext) -> None:
        text = await preset_action(ctx, callback_data.action, callback_data.name, callback_data.t)
        await cb.answer(text[:190], show_alert=text.startswith(OUT_OF_DATE))

    return router


def build_deny_router() -> Router:
    """Last router: anyone who is not an admin gets 'Not allowed' on buttons."""
    router = Router(name="deny")

    @router.callback_query()
    async def deny(cb: CallbackQuery) -> None:
        await cb.answer("Not allowed", show_alert=True)

    return router


def create_bot(settings: Settings) -> Bot:
    from aiogram.utils.token import TokenValidationError

    try:
        return Bot(settings.BOT_TOKEN)
    except TokenValidationError as exc:
        raise ConfigError("BOT_TOKEN in .env is not a valid bot token") from exc


def build_dispatcher(ctx: AppContext) -> Dispatcher:
    if not ctx.settings.admin_ids:
        raise ConfigError("ADMIN_IDS is empty in .env: set your Telegram user id(s)")
    dp = Dispatcher()
    dp["ctx"] = ctx
    admin = AdminFilter(ctx.settings.admin_ids)
    dp.include_router(build_router(ctx.settings.admin_ids))
    dp.include_router(build_wizard_router(admin, admin))
    dp.include_router(build_deny_router())
    return dp


async def set_bot_commands(bot: Bot) -> None:
    try:
        await bot.set_my_commands(BOT_COMMANDS)
    except Exception as exc:
        log.warning("set_my_commands failed: %s", exc)
