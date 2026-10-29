"""Tap-to-hunt wizard (/hunt) with a 'would have caught' preview.

aiogram 3 Router with FSM on MemoryStorage (a half-done wizard is lost on restart, which is fine).
Option lists live in FSM data and buttons carry only a short index, so callback_data stays tiny.
Nothing is saved before [Save]; [Cancel] saves nothing.
"""

from __future__ import annotations

import difflib
import logging
import math
import re
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .ledger import position_key
from .models import Listing, alnum_key, norm
from .pricing import FeeModel, Flag, FloorEstimate, assess
from .rules import BuyRule, match

if TYPE_CHECKING:
    from .bot import AppContext

log = logging.getLogger(__name__)

PAGE_SIZE = 8
MODE_BUTTONS = (("Ask me first (recommended)", "confirm"), ("Buy automatically", "auto"), ("Just alert me", "watch"))
MODE_WORDS = {"confirm": "Ask before buying.", "auto": "Buy automatically.", "watch": "Just alert me."}


class HuntCB(CallbackData, prefix="h"):
    action: str  # p page | c collection | m model | g goal | x max | o mode | s save | cg change goal | q cancel
    idx: int = 0


class Hunt(StatesGroup):
    collection = State()
    model = State()
    goal = State()
    maxprice = State()
    typing_max = State()
    mode = State()
    summary = State()


# --- pure helpers (unit-tested) -------------------------------------------------------------

_NFT_LINK = re.compile(r"(?:https?://)?t\.me/nft/([A-Za-z0-9]+)-(\d+)", re.IGNORECASE)


def parse_nft_link(text: str) -> tuple[str, int] | None:
    """'https://t.me/nft/PlushPepe-123' -> ('PlushPepe', 123); anything else -> None."""
    m = _NFT_LINK.search(text or "")
    return (m.group(1), int(m.group(2))) if m else None


def collection_options(floors: dict[str, float]) -> list[tuple[str, float]]:
    """Unique collections (name aliases collapse by normalized key), sorted by name."""
    seen: dict[str, tuple[str, float]] = {}
    for name, floor in floors.items():
        key = norm(name)
        if key and (key not in seen or (" " in name and " " not in seen[key][0])):
            seen[key] = (name, floor)
    return sorted(seen.values(), key=lambda x: x[0].lower())


def fuzzy_matches(text: str, names: list[str], n: int = 3) -> list[str]:
    lowered = {name.lower(): name for name in names}
    hits = difflib.get_close_matches(text.lower().strip(), list(lowered), n=n, cutoff=0.6)
    return [lowered[h] for h in hits]


def slug_to_index(slug_name: str, options: list[tuple[str, float]]) -> int | None:
    key = alnum_key(slug_name)
    return next((i for i, (name, _) in enumerate(options) if alnum_key(name) == key), None)


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:g}" if x >= 10 else f"{x:.2f}"


def goal_options(floor: float | None) -> list[tuple[str, str, float]]:
    """(label, kind, value): kind 'profit' -> min_profit_ton, 'discount' -> min_discount_pct."""
    if floor and floor > 50:
        profits = [(f"Earn ≥ {_fmt(floor * p / 100)} TON ({p}%)", "profit", round(floor * p / 100, 2)) for p in (2, 5, 10)]
    else:
        profits = [("Earn ≥ 0.2 TON", "profit", 0.2), ("≥ 0.5", "profit", 0.5), ("≥ 1", "profit", 1.0)]
    discounts = [("Under floor by 10%", "discount", 10.0), ("20%", "discount", 20.0), ("30%", "discount", 30.0)]
    return profits + discounts


def max_options(floor: float | None, cap: float) -> list[tuple[str, float, bool]]:
    """(label, value, capped) for floor x0.8 / x0.9 / x1.0, always capped at MAX_PRICE_TON."""
    if not floor:
        return [(f"{cap:g} TON (MAX_PRICE_TON)", cap, True)]
    out = []
    for mult in (0.8, 0.9, 1.0):
        value = round(floor * mult, 2)
        capped = value > cap
        value = min(value, cap)
        out.append((f"floor×{mult:g} = {_fmt(value)}" + (" (cap)" if capped else ""), value, capped))
    return out


def summary_text(rule: BuyRule, *, practice: bool, shadow: bool, cap_note: bool, cap: float) -> str:
    model = f"model {rule.model}" if rule.model else "any model"
    if rule.min_profit_ton:
        goal = f"I'd make at least {_fmt(rule.min_profit_ton)} TON after fees"
    else:
        goal = f"it is at least {rule.min_discount_pct:g}% under the floor"
    text = (f"Hunt {rule.collection} ({model}). Tell me when {goal}, price up to {_fmt(rule.max_price_ton)} TON. "
            f"{MODE_WORDS[rule.mode]} {'Practice mode.' if practice else 'LIVE mode.'}")
    if cap_note:
        text += f" (Max price capped by MAX_PRICE_TON={cap:g}.)"
    if shadow:
        text += " New rules start in shadow: they practise until you promote them."
    return text


@dataclass(slots=True)
class Preview:
    text: str
    alerts: int
    locked: int
    approximate: bool


def preview_rule(
    rule: BuyRule,
    observations: Iterable[dict[str, Any]],
    snapshots: dict[str, list[tuple[float, float, int, str]]],
    fees: FeeModel,
    now: float,
    hours: float = 24,
    *,
    current_floor: float | None = None,
    locked_ids: Iterable[str] = (),
    tape_start: float | None = None,
    min_comparables: int = 5,
) -> Preview:
    """Replay the observation tape through rule.match with the floor of the time (pure)."""
    obs_all = [o for o in observations if norm(o["collection"]) == norm(rule.collection)]
    if not obs_all:
        return Preview("No history yet: I'll start watching after you save.", 0, 0, False)
    start = now - hours * 3600
    window = [o for o in obs_all if o["first_seen"] >= start]
    locked_set = set(locked_ids)
    keys = [rule.filter_key_str()]
    alerts, locked, approximate = [], 0, False
    for o in window:
        listing = Listing(o["listing_id"], o["collection"], o.get("model"), o.get("backdrop"), o.get("symbol"),
                          o.get("number"), o["price_ton"], None)
        floor = None
        for key in keys + [position_key(o["collection"], o.get("model")), position_key(o["collection"], None)]:
            older = [s for s in snapshots.get(key, []) if s[0] <= o["first_seen"] and s[1]]
            if older:
                floor = max(older, key=lambda s: s[0])
                break
        if floor is not None:
            ts, value, n, source = floor
            conf = "high" if source == "cluster" and n >= min_comparables else "med" if n >= 2 else "low"
            est = FloorEstimate(value, source, n, conf)
        elif current_floor:
            approximate = True
            est = FloorEstimate(current_floor, "api", 0, "med")
        else:
            continue
        flags = [Flag("LOCKED", "locked", False, math.inf)] if o["listing_id"] in locked_set else []
        deal = assess(listing, est, fees, flags)
        res = match(rule, listing, deal)
        if res.ok:
            alerts.append(deal)
        elif res.code == "LOCKED":
            locked += 1
    if alerts:
        avg = statistics.mean(d.discount_pct or 0 for d in alerts)
        best = max(d.profit_ton or 0 for d in alerts)
        text = (f"In the last {hours:g} h this would have alerted {len(alerts)}× "
                f"(avg −{avg:.0f}%, best {best:+.1f} TON net)")
    else:
        text = f"In the last {hours:g} h this would not have alerted"
    if locked:
        text += f"; {locked} was locked" if locked == 1 else f"; {locked} were locked"
    text += "."
    if approximate:
        text += " (Approximate: no floor history for part of the tape.)"
    first = tape_start if tape_start is not None else min(o["first_seen"] for o in obs_all)
    watched = (now - first) / 3600
    if watched < hours:
        text = f"I've only been watching for {max(watched, 0):.0f} h. " + text
    return Preview(text, len(alerts), locked, approximate)


async def preview_for(ctx: "AppContext", rule: BuyRule, now: float) -> Preview:
    s = ctx.settings
    hours = s.PREVIEW_HOURS
    storage = ctx.storage
    obs = await storage.observations_since(now - 14 * 86400)
    snaps: dict[str, list[tuple[float, float, int, str]]] = {}
    for snap in await storage.all_snapshots(now - hours * 3600 - 86400):
        snaps.setdefault(snap.filter_key, []).append((snap.ts, snap.floor_ton or 0, snap.n, snap.source))
    locked = {d.listing_id for d in await storage.decisions_since(now - hours * 3600) if d.reason_code == "LOCKED"}
    from .engine import api_floor_for

    tape_start, _ = await storage.observation_bounds()
    return preview_rule(rule, obs, snaps, ctx.engine.fees_for(rule.collection), now, hours,
                        current_floor=api_floor_for(ctx.engine._floors, rule), locked_ids=locked, tape_start=tape_start,
                        min_comparables=s.MIN_COMPARABLES_ATTR if rule.pins_attributes else s.MIN_COMPARABLES_COLLECTION)


# --- keyboards ---------------------------------------------------------------------------------


def collections_keyboard(options: list[tuple[str, float]], page: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for i, (name, floor) in enumerate(options[start : start + PAGE_SIZE], start):
        kb.row(InlineKeyboardButton(text=f"{name} · {_fmt(floor)}", callback_data=HuntCB(action="c", idx=i).pack()))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="‹", callback_data=HuntCB(action="p", idx=page - 1).pack()))
    if start + PAGE_SIZE < len(options):
        nav.append(InlineKeyboardButton(text="›", callback_data=HuntCB(action="p", idx=page + 1).pack()))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="Cancel", callback_data=HuntCB(action="q").pack()))
    return kb.as_markup()


def _rows(buttons: list[tuple[str, CallbackData]], per_row: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for label, cb in buttons:
        kb.button(text=label, callback_data=cb)
    kb.adjust(per_row)
    kb.row(InlineKeyboardButton(text="Cancel", callback_data=HuntCB(action="q").pack()))
    return kb.as_markup()


def all_wizard_keyboards(floor: float = 1500.0, cap: float = 10.0) -> list[InlineKeyboardMarkup]:
    """Every keyboard shape the wizard can show (for the 64-byte callback_data test)."""
    options = [(f"Collection {i}", float(i)) for i in range(20)]
    kbs = [collections_keyboard(options, 0), collections_keyboard(options, 1), collections_keyboard(options, 2)]
    kbs.append(_rows([("Any model", HuntCB(action="m", idx=-1))] +
                     [(f"Model {i}", HuntCB(action="m", idx=i)) for i in range(8)], 2))
    kbs.append(_rows([(g[0], HuntCB(action="g", idx=i)) for i, g in enumerate(goal_options(floor))], 3))
    kbs.append(_rows([(m[0], HuntCB(action="x", idx=i)) for i, m in enumerate(max_options(floor, cap))]
                     + [("type it", HuntCB(action="x", idx=99))], 2))
    kbs.append(_rows([(label, HuntCB(action="o", idx=i)) for i, (label, _) in enumerate(MODE_BUTTONS)], 1))
    kbs.append(_rows([("Save", HuntCB(action="s")), ("Change goal", HuntCB(action="cg"))], 2))
    return kbs


# --- router ------------------------------------------------------------------------------------


async def _show(target: Message | CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    if isinstance(target, CallbackQuery):
        msg = target.message
        if isinstance(msg, Message):
            try:
                await msg.edit_text(text, reply_markup=kb)
                return
            except Exception as exc:  # message too old / unchanged: send a new one
                log.debug("edit failed: %s", exc)
            await msg.answer(text, reply_markup=kb)
        return
    await target.answer(text, reply_markup=kb)


async def begin(message: Message, state: FSMContext, ctx: "AppContext") -> None:
    try:
        table = await ctx.engine.api_floors(force=True)  # Tonnel filterStats (needs initData)
    except Exception as exc:
        log.warning("wizard: collection floors failed: %s", exc)
        table = None
    options = collection_options(table.named() if table else {})
    if not options:
        await message.answer("I can't load Tonnel collections right now (filterStats needs a Tonnel login). "
                             "Run /doctor; you may need: python -m tonnel_autobuy login. "
                             "/addrule still works without it.")
        return
    await state.set_state(Hunt.collection)
    await state.set_data({"options": options, "page": 0})
    await message.answer("Step 1/5 · Which collection? Tap one, type a name, or paste a t.me/nft link.",
                         reply_markup=collections_keyboard(options, 0))


async def _models(ctx: "AppContext", collection: str, limit: int = 8) -> list[tuple[str, float, int]]:
    """Tonnel: per-model floors from filterStats (FloorTable.models); else models seen in observations."""
    table = ctx.engine._floors
    models = table.models_of(collection) if table is not None else []
    return models[:limit] if models else await ctx.storage.observed_models(collection, limit=limit)


async def _floor_for(ctx: "AppContext", data: dict[str, Any]) -> float | None:
    if data.get("model"):
        for model, lo, _ in await _models(ctx, data["collection"], limit=50):
            if norm(model) == norm(data["model"]):
                return lo
    return data.get("floor")


async def _ask_model(target: Message | CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
    data = await state.get_data()
    models = await _models(ctx, data["collection"])
    await state.update_data(models=[(m, lo) for m, lo, _ in models])
    await state.set_state(Hunt.model)
    buttons = [("Any model", HuntCB(action="m", idx=-1))] + [
        (f"{m} · {_fmt(lo)}", HuntCB(action="m", idx=i)) for i, (m, lo, _) in enumerate(models)]
    extra = "" if models else " (no models seen yet: type one, or take any)"
    await _show(target, f"Step 2/5 · {data['collection']}: which model?{extra}", _rows(buttons, 2))


async def _ask_goal(target: Message | CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
    data = await state.get_data()
    floor = await _floor_for(ctx, data)
    goals = goal_options(floor)
    await state.update_data(goals=goals, step_floor=floor)
    await state.set_state(Hunt.goal)
    fl = f" (floor {_fmt(floor)} TON)" if floor else ""
    await _show(target, f"Step 3/5 · What is a good deal for you?{fl}",
                _rows([(g[0], HuntCB(action="g", idx=i)) for i, g in enumerate(goals)], 3))


async def _ask_max(target: Message | CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
    data = await state.get_data()
    cap = (await ctx.engine.effective_limits()).max_price_ton
    maxes = max_options(data.get("step_floor"), cap)
    await state.update_data(maxes=maxes)
    await state.set_state(Hunt.maxprice)
    buttons = [(m[0], HuntCB(action="x", idx=i)) for i, m in enumerate(maxes)] + [("type it", HuntCB(action="x", idx=99))]
    note = f" Every buy is also capped at MAX_PRICE_TON={cap:g}." if any(m[2] for m in maxes) else ""
    await _show(target, f"Step 4/5 · Highest price you would pay?{note}", _rows(buttons, 2))


async def _ask_mode(target: Message | CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Hunt.mode)
    await _show(target, "Step 5/5 · What should I do when I find one?",
                _rows([(label, HuntCB(action="o", idx=i)) for i, (label, _) in enumerate(MODE_BUTTONS)], 1))


def _rule_from(data: dict[str, Any], ctx: "AppContext") -> BuyRule:
    kind, value = data["goal"]
    return BuyRule(
        collection=data["collection"],
        max_price_ton=float(data["max"]),
        model=data.get("model"),
        backdrop=data.get("backdrop"),
        symbol=data.get("symbol"),
        min_discount_pct=value if kind == "discount" else 0.0,
        min_profit_ton=value if kind == "profit" else 0.0,
        mode=data["mode"],
        source="bot",
        state="shadow" if ctx.settings.SHADOW_NEW_RULES else "live",
    )


async def _summary(target: Message | CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
    data = await state.get_data()
    rule = _rule_from(data, ctx)
    now = ctx.engine.clock()
    cap = (await ctx.engine.effective_limits()).max_price_ton
    text = summary_text(rule, practice=await ctx.engine.is_dry(now), shadow=rule.state == "shadow",
                        cap_note=bool(data.get("capped")), cap=cap)
    preview = await preview_for(ctx, rule, now)
    await state.set_state(Hunt.summary)
    kb = InlineKeyboardBuilder()
    kb.button(text="Save", callback_data=HuntCB(action="s"))
    kb.button(text="Change goal", callback_data=HuntCB(action="cg"))
    kb.button(text="Cancel", callback_data=HuntCB(action="q"))
    await _show(target, f"{text}\n\n{preview.text}", kb.as_markup())


def build_wizard_router(message_filter: Any, callback_filter: Any) -> Router:
    router = Router(name="wizard")
    router.message.filter(message_filter)
    router.callback_query.filter(callback_filter)

    @router.message(Command("hunt"))
    async def cmd_hunt(message: Message, state: FSMContext, ctx: "AppContext") -> None:
        await begin(message, state, ctx)

    @router.callback_query(HuntCB.filter(F.action == "q"))
    async def on_cancel(cb: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await cb.answer()
        await _show(cb, "Cancelled. Nothing was saved.")

    @router.callback_query(Hunt.collection, HuntCB.filter(F.action == "p"))
    async def on_page(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext) -> None:
        data = await state.get_data()
        await state.update_data(page=callback_data.idx)
        await cb.answer()
        await _show(cb, "Step 1/5 · Which collection?", collections_keyboard(data["options"], callback_data.idx))

    @router.callback_query(Hunt.collection, HuntCB.filter(F.action == "c"))
    async def on_collection(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext, ctx: "AppContext") -> None:
        data = await state.get_data()
        name, floor = data["options"][callback_data.idx]
        await state.update_data(collection=name, floor=floor)
        await cb.answer()
        await _ask_model(cb, state, ctx)

    @router.message(Hunt.collection, F.text)
    async def on_collection_text(message: Message, state: FSMContext, ctx: "AppContext") -> None:
        data = await state.get_data()
        options = data["options"]
        link = parse_nft_link(message.text or "")
        if link:
            idx = slug_to_index(link[0], options)
            if idx is None:
                await message.answer("I don't know that collection on Tonnel. Pick one from the list.")
                return
            name, floor = options[idx]
            await state.update_data(collection=name, floor=floor)
            attrs = None
            if ctx.gift_lookup is not None:
                try:
                    attrs = await ctx.gift_lookup(f"{link[0]}-{link[1]}")
                except Exception as exc:
                    log.info("gift lookup failed: %s", exc)
            if attrs and attrs.get("model"):
                await state.update_data(model=attrs["model"], backdrop=None, symbol=None)
                await message.answer(f"Hunt this one cheaper: {name}, model {attrs['model']}.")
                await _ask_goal(message, state, ctx)
                return
            await message.answer(f"Collection: {name}.")
            await _ask_model(message, state, ctx)
            return
        hits = fuzzy_matches(message.text or "", [n for n, _ in options])
        if not hits:
            await message.answer("No close match. Tap a collection from the list or paste a t.me/nft link.")
            return
        index = {n: i for i, (n, _) in enumerate(options)}
        await message.answer("Did you mean:", reply_markup=_rows([(h, HuntCB(action="c", idx=index[h])) for h in hits], 1))

    @router.callback_query(Hunt.model, HuntCB.filter(F.action == "m"))
    async def on_model(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext, ctx: "AppContext") -> None:
        data = await state.get_data()
        model = None if callback_data.idx < 0 else data["models"][callback_data.idx][0]
        await state.update_data(model=model)
        await cb.answer()
        await _ask_goal(cb, state, ctx)

    @router.message(Hunt.model, F.text)
    async def on_model_text(message: Message, state: FSMContext, ctx: "AppContext") -> None:
        await state.update_data(model=(message.text or "").strip() or None)
        await _ask_goal(message, state, ctx)

    @router.callback_query(Hunt.goal, HuntCB.filter(F.action == "g"))
    async def on_goal(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext, ctx: "AppContext") -> None:
        data = await state.get_data()
        _, kind, value = data["goals"][callback_data.idx]
        await state.update_data(goal=(kind, value))
        await cb.answer()
        if data.get("max") is not None and data.get("mode"):
            await _summary(cb, state, ctx)  # 'Change goal' returns straight to the summary
            return
        await _ask_max(cb, state, ctx)

    @router.callback_query(Hunt.maxprice, HuntCB.filter(F.action == "x"))
    async def on_max(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext) -> None:
        await cb.answer()
        if callback_data.idx == 99:
            await state.set_state(Hunt.typing_max)
            await _show(cb, "Type the max price in TON (fees included), e.g. 2.5")
            return
        data = await state.get_data()
        _, value, capped = data["maxes"][callback_data.idx]
        await state.update_data(max=value, capped=capped)
        await _ask_mode(cb, state)

    @router.message(Hunt.typing_max, F.text)
    async def on_max_text(message: Message, state: FSMContext, ctx: "AppContext") -> None:
        try:
            value = float((message.text or "").replace(",", ".").strip())
        except ValueError:
            await message.answer("Send a number, e.g. 2.5")
            return
        if not math.isfinite(value) or value <= 0:  # float() also takes "nan" and "inf"
            await message.answer("The max must be a number above 0, e.g. 2.5")
            return
        cap = (await ctx.engine.effective_limits()).max_price_ton
        await state.update_data(max=min(value, cap), capped=value > cap)
        await _ask_mode(message, state)

    @router.callback_query(Hunt.mode, HuntCB.filter(F.action == "o"))
    async def on_mode(cb: CallbackQuery, callback_data: HuntCB, state: FSMContext, ctx: "AppContext") -> None:
        await state.update_data(mode=MODE_BUTTONS[callback_data.idx][1])
        await cb.answer()
        await _summary(cb, state, ctx)

    @router.callback_query(Hunt.summary, HuntCB.filter(F.action == "cg"))
    async def on_change_goal(cb: CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
        await cb.answer()
        await _ask_goal(cb, state, ctx)

    @router.callback_query(Hunt.summary, HuntCB.filter(F.action == "s"))
    async def on_save(cb: CallbackQuery, state: FSMContext, ctx: "AppContext") -> None:
        data = await state.get_data()
        rule = _rule_from(data, ctx)
        await ctx.storage.add_rule(rule)
        if rule.state == "shadow":
            await ctx.storage.set_setting(f"shadow_since:{rule.id}", ctx.engine.clock())
        await state.clear()
        await cb.answer("Saved")
        await _show(cb, f"Saved rule {rule.describe()}. /rules to list, /mode {rule.id} auto|confirm|watch to change.")

    return router
