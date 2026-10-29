"""The admin bot wiring: every shared command exists and answers offline (fake Bot API session)."""

import asyncio

from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from helpers import ADMIN, demo_engine, settings
from test_wizard import Driver, FakeSession

from tonnel_autobuy.bot import BOT_COMMANDS, BotContext, build_dispatcher

SHARED = ("why", "panic", "safety", "doctor", "scoreboard")
ALL = SHARED + ("start", "status", "pause", "resume", "rules", "addrule", "delrule", "dry", "balance", "buys",
                "floor", "mode", "promote", "pnl", "score", "sold", "export", "digest", "mute", "unmute", "preset",
                "peers", "compare")


async def _driver(**over):
    engine, storage, client, notifier, clock = await demo_engine(settings(**over))
    session = FakeSession()
    d = Driver(build_dispatcher(BotContext(engine.settings, storage, engine, client)), Bot(token="42:TEST",
               session=session), session)
    return d, engine, storage


def test_every_command_has_a_handler():
    async def go():
        d, engine, storage = await _driver()
        try:
            names = {h.callback.__name__ for r in d.dp.sub_routers for h in r.message.handlers}
            assert {f"cmd_{c}" for c in ALL} | {"cmd_hunt"} <= names
            assert {c.command for c in BOT_COMMANDS} >= set(SHARED) | {"hunt"}
        finally:
            await storage.close()

    asyncio.run(go())


def test_commands_answer_offline():
    async def go():
        d, engine, storage = await _driver()
        try:
            await engine.run_cycle()
            replies = {}
            for cmd in ("/safety", "/status", "/scoreboard", "/score", "/pnl 7", "/why 814412", "/peers",
                        "/compare Spy Agaric", "/digest", "/mode 3 watch", "/addrule Toy Bear max=12 profit=0.5",
                        "/rules", "/mute Plush Pepe 2", "/buys", "/floor Lunar Snake", "/sold 1 1.5", "/doctor"):
                await d.text(cmd)
                replies[cmd] = d.last_text()
            assert replies["/safety"].startswith("Mode: PRACTICE, locked by DRY_RUN=true in .env.")
            assert "Fees: buy 10% · sell 0% · withdraw 0 TON (unverified)" in replies["/status"]
            assert replies["/scoreboard"].startswith("Practice scoreboard, last 7 days (net of fees):")
            assert "<pre>" in replies["/pnl 7"] and "PRACTICE 7d" in replies["/pnl 7"]
            assert "loses 0.07 TON after fees" in replies["/why 814412"]
            assert "Tonnel" in replies["/compare Spy Agaric"]
            assert replies["/mode 3 watch"] == "Rule #3 mode: watch." and (await storage.get_rule(3)).mode == "watch"
            assert "[shadow]" in replies["/addrule Toy Bear max=12 profit=0.5"]  # new rules start in shadow
            assert "Muted plush pepe for 2 h." in replies["/mute Plush Pepe 2"]
            assert "PRACTICE bought Lunar Snake #4410" in replies["/buys"]
            assert "API floor 0.97 TON" in replies["/floor Lunar Snake"]
            assert replies["/sold 1 1.5"] == "Closed #1 at 1.5 TON: +0.40 TON net after fees."
            assert replies["/doctor"].startswith("Doctor: ")
            await d.text("/start")
            assert d.last_text().startswith("I watch Tonnel for gifts listed under a well-backed floor, count the 10%")
            assert "Try first: /hunt" in d.last_text() and "Mode: PRACTICE" in d.last_text()
            await d.text("/export")
            assert isinstance(d.session.calls[-1], SendDocument)
        finally:
            await storage.close()

    asyncio.run(go())


def test_panic_then_resume_needs_the_confirm_button():
    async def go():
        d, engine, storage = await _driver()
        try:
            await d.text("/panic")
            assert d.last_text().startswith("Stopped.") and await engine.is_panic()
            await d.text("/resume")
            assert d.last_text().startswith("Resume sniping?") and await engine.is_paused()  # nothing yet
            await d.tap("Confirm resume")
            assert not await engine.is_panic() and not await engine.is_paused() and await engine.is_dry()
            await d.text("/dry off")
            assert d.last_text().startswith("Live mode is locked: DRY_RUN=true")  # the .env lock still holds
        finally:
            await storage.close()

    asyncio.run(go())


def test_buttons_refuse_non_admins():
    async def go():
        d, engine, storage = await _driver()
        try:
            await engine.run_cycle()
            card = (await storage.list_pending(("pending",)))[0]
            await d.text(f"/why {card.listing_id}")  # any message with a markup will do for the tap helper
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            from tonnel_autobuy.callbacks import DealCB

            d.session.calls.append(SendMessage(chat_id=ADMIN, text="card", reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="Buy", callback_data=DealCB(action="b",
                                                                                        token=card.token).pack())]])))
            await d.tap("Buy", uid=999)
            answers = [c for c in d.session.calls if isinstance(c, AnswerCallbackQuery)]
            assert answers[-1].text == "Not allowed"
            assert (await storage.get_pending(card.token)).status == "pending"
        finally:
            await storage.close()

    asyncio.run(go())
