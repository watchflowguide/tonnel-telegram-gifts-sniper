import asyncio
import os
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from helpers import ADMIN, NOW, Clock, demo_engine, settings

from tonnel_autobuy.bot import BotContext, build_dispatcher
from tonnel_autobuy.client import MockClient
from tonnel_autobuy.doctor import (
    Check,
    DoctorDeps,
    check_2fa,
    check_market_auth,
    check_session,
    check_session_mode,
    format_doctor,
    run_doctor,
)
from tonnel_autobuy.models import FloorTable
from tonnel_autobuy.notifier import keyboard_callbacks
from tonnel_autobuy.pricing import FeeModel
from tonnel_autobuy.rules import BuyRule
from tonnel_autobuy.wizard import (
    all_wizard_keyboards,
    collection_options,
    fuzzy_matches,
    max_options,
    parse_nft_link,
    preview_rule,
    slug_to_index,
)

NAMES = ["Astral Shard", "B-Day Candle", "Berry Box", "Bunny Muffin", "Candy Cane", "Desk Calendar", "Durov's Cap",
         "Eternal Rose", "Genie Lamp", "Hex Pot", "Jelly Bunny", "Lunar Snake", "Plush Pepe"]


class WizardClient(MockClient):
    """Demo book plus 13 collections in filterStats (with model floors), so the wizard needs a second page."""

    async def collection_floors(self):
        self.requests += 1
        table = FloorTable()
        for i, name in enumerate(NAMES):
            table.add(name, None, 1.0 + i, 3)
        table.add("Lunar Snake", "Albino (1.2%)", 13.0, 2)
        table.add("Lunar Snake", "Cobra (2%)", 12.5, 1)
        return table


class FakeSession(BaseSession):
    """Answers Bot API calls locally and records them (no network)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = []
        self._id = 100

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._id += 1
            return Message(message_id=self._id, date=datetime.now(timezone.utc),
                           chat=Chat(id=getattr(method, "chat_id", None) or ADMIN, type="private"),
                           text=method.text, reply_markup=method.reply_markup)
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):
        yield b""


def _user(uid=ADMIN):
    return User(id=uid, is_bot=False, first_name="Admin")


class Driver:
    def __init__(self, dp, bot, session):
        self.dp, self.bot, self.session = dp, bot, session
        self.n = 0

    def last_markup(self):
        for call in reversed(self.session.calls):
            if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup is not None:
                return call.reply_markup
        return None

    def last_text(self):
        for call in reversed(self.session.calls):
            if isinstance(call, (SendMessage, EditMessageText)):
                return call.text
        return ""

    def labels(self):
        return [b.text for row in self.last_markup().inline_keyboard for b in row]

    async def text(self, text, uid=ADMIN):
        self.n += 1
        msg = Message(message_id=self.n, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                      from_user=_user(uid), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg))

    async def tap(self, label_start, uid=ADMIN):
        markup = self.last_markup()
        button = next(b for row in markup.inline_keyboard for b in row if b.text.startswith(label_start))
        self.n += 1
        msg = Message(message_id=500, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                      from_user=_user(), text="card")
        cb = CallbackQuery(id=str(self.n), from_user=_user(uid), chat_instance="x", message=msg, data=button.callback_data)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cb))


async def _wizard(s=None):
    clock = Clock()
    engine, storage, client, notifier, _ = await demo_engine(s or settings(), rules=[],
                                                             client=WizardClient("demo", now=NOW), clock=clock)
    ctx = BotContext(engine.settings, storage, engine, client)
    session = FakeSession()
    bot = Bot(token="42:TEST", session=session)
    return Driver(build_dispatcher(ctx), bot, session), storage


def test_scripted_wizard_saves_a_confirm_rule_in_six_taps():
    async def go():
        d, storage = await _wizard(settings(MAX_PRICE_TON=100, DAILY_BUDGET_TON=200))
        try:
            await d.text("/hunt")
            assert "Step 1/5" in d.last_text()
            assert d.labels()[0] == "Astral Shard · 1.00"
            await d.tap("›")  # page 2
            await d.tap("Lunar Snake")  # 1
            assert "Step 2/5" in d.last_text()
            assert d.labels()[:3] == ["Any model", "Cobra · 12.5", "Albino · 13"]  # filterStats model floors
            await d.tap("Any model")  # 2
            await d.tap("≥ 0.5")  # 3
            await d.tap("floor×0.9")  # 4
            await d.tap("Ask me first")  # 5
            summary = d.last_text()
            assert summary.startswith("Hunt Lunar Snake (any model). Tell me when I'd make at least 0.50 TON after fees")
            assert "Ask before buying. Practice mode." in summary
            assert "No history yet" in summary or "In the last 24 h" in summary
            assert await storage.list_rules() == []  # nothing saved before Save
            await d.tap("Save")  # 6
            [rule] = await storage.list_rules()
            assert (rule.collection, rule.model, rule.mode, rule.source) == ("Lunar Snake", None, "confirm", "bot")
            assert rule.min_profit_ton == 0.5 and rule.max_price_ton == pytest.approx(12 * 0.9)
            assert rule.state == "shadow"
        finally:
            await storage.close()

    asyncio.run(go())


def test_model_step_uses_the_model_floor():
    async def go():
        d, storage = await _wizard(settings(MAX_PRICE_TON=100, DAILY_BUDGET_TON=200))
        try:
            await d.text("/hunt")
            await d.tap("›")
            await d.tap("Lunar Snake")
            await d.tap("Albino")
            assert "(floor 13 TON)" in d.last_text()
            await d.tap("20%")
            await d.tap("floor×0.8")
            await d.tap("Buy automatically")
            await d.tap("Save")
            [rule] = await storage.list_rules()
            assert (rule.model, rule.mode, rule.min_discount_pct, rule.max_price_ton) == ("Albino", "auto", 20, 10.4)
        finally:
            await storage.close()

    asyncio.run(go())


def test_cancel_saves_nothing_and_non_admin_is_refused():
    async def go():
        d, storage = await _wizard()
        try:
            await d.text("/hunt")
            await d.tap("Desk Calendar")
            await d.tap("Cancel")
            assert "Nothing was saved" in d.last_text()
            assert await storage.list_rules() == []
            await d.text("/hunt")
            await d.tap("Desk Calendar", uid=999)  # someone else presses the button
            answers = [c for c in d.session.calls if isinstance(c, AnswerCallbackQuery)]
            assert answers[-1].text == "Not allowed"
        finally:
            await storage.close()

    asyncio.run(go())


def test_typed_name_and_pasted_link():
    async def go():
        d, storage = await _wizard()
        try:
            await d.text("/hunt")
            await d.text("plush pep")
            assert "Did you mean" in d.last_text()
            assert d.labels()[0] == "Plush Pepe"
            await d.text("https://t.me/nft/LunarSnake-4410")
            assert "Step 2/5 · Lunar Snake" in d.last_text()
        finally:
            await storage.close()

    asyncio.run(go())


def test_pasted_link_prefills_attributes_with_telethon():
    async def go():
        d, storage = await _wizard()
        ctx = d.dp["ctx"]

        async def lookup(slug):
            assert slug == "DurovsCap-5"
            return {"model": "Gold Star", "backdrop": "Black", "symbol": "Star"}

        ctx.gift_lookup = lookup
        try:
            await d.text("/hunt")
            await d.text("t.me/nft/DurovsCap-5")
            texts = [c.text for c in d.session.calls if isinstance(c, SendMessage)]
            assert any(t.startswith("Hunt this one cheaper: Durov's Cap, model Gold Star") for t in texts)
            assert "Step 3/5" in d.last_text()
        finally:
            await storage.close()

    asyncio.run(go())


def test_max_price_capped_by_max_price_ton():
    async def go():
        d, storage = await _wizard(settings(MAX_PRICE_TON=5))
        try:
            await d.text("/hunt")
            await d.tap("›")
            await d.tap("Plush Pepe")  # floor 13
            await d.tap("Any model")
            await d.tap("Under floor by 10%")
            assert "MAX_PRICE_TON=5" in d.last_text()
            await d.tap("floor×1")
            await d.tap("Just alert me")
            assert "(Max price capped by MAX_PRICE_TON=5.)" in d.last_text()
            await d.tap("Save")
            [rule] = await storage.list_rules()
            assert rule.max_price_ton == 5 and rule.mode == "watch" and rule.min_discount_pct == 10
        finally:
            await storage.close()

    asyncio.run(go())
    opts = max_options(1500.0, 1350.0)
    assert [o[1] for o in opts] == [1200.0, 1350.0, 1350.0] and opts[2][2]


def test_wizard_without_tonnel_auth_points_to_doctor():
    class NoAuth(MockClient):
        has_auth = False

    async def go():
        engine, storage, client, notifier, _ = await demo_engine(rules=[], client=NoAuth("demo", now=NOW))
        d = Driver(build_dispatcher(BotContext(engine.settings, storage, engine, client)), Bot(token="42:TEST",
                   session=FakeSession()), None)
        d.session = d.bot.session
        try:
            await d.text("/hunt")
            assert "/doctor" in d.last_text() and "python -m tonnel_autobuy login" in d.last_text()
        finally:
            await storage.close()

    asyncio.run(go())


def test_every_wizard_callback_fits_64_bytes():
    datas = [d for kb in all_wizard_keyboards() for d in keyboard_callbacks(kb)]
    assert datas and all(len(d.encode()) <= 64 for d in datas)


def test_fuzzy_match():
    assert fuzzy_matches("plush pep", NAMES)[0] == "Plush Pepe"
    assert fuzzy_matches("zzzz", NAMES) == []


def test_nft_link_parsing():
    assert parse_nft_link("https://t.me/nft/PlushPepe-123") == ("PlushPepe", 123)
    assert parse_nft_link("t.me/nft/DurovsCap-5") == ("DurovsCap", 5)
    assert parse_nft_link("hello world") is None
    assert parse_nft_link("https://t.me/Tonnel_Network_bot") is None
    opts = collection_options({"Durov's Cap": 1.0, "durov's cap": 1.0, "Plush Pepe": 2.0})
    assert [o[0] for o in opts] == ["Durov's Cap", "Plush Pepe"]
    assert slug_to_index("DurovsCap", opts) == 0 and slug_to_index("Nope", opts) is None


def test_preview_rule_counts_on_fixture_tape():
    def obs(lid, price, hours_ago, model="Albino"):
        t = NOW - hours_ago * 3600
        return {"listing_id": lid, "price_ton": price, "collection": "Lunar Snake", "model": model, "backdrop": None,
                "symbol": None, "number": 1, "first_seen": t, "last_seen": t}

    tape = [obs("a", 1.10, 20), obs("b", 1.65, 18), obs("c", 1.05, 6), obs("d", 0.90, 4), obs("old", 0.5, 30)]
    snaps = {"lunar snake|albino||": [(NOW - 24 * 3600, 1.70, 4, "cluster"), (NOW - 12 * 3600, 1.72, 4, "cluster")]}
    rule = BuyRule("Lunar Snake", 3, min_profit_ton=0.3)
    p = preview_rule(rule, tape, snaps, FeeModel(10, 0, 0), NOW, 24, locked_ids={"d"})
    assert p.alerts == 2 and p.locked == 1 and not p.approximate
    assert p.text == "In the last 24 h this would have alerted 2× (avg −37%, best +0.6 TON net); 1 was locked."
    short = preview_rule(rule, tape[:4], snaps, FeeModel(10, 0, 0), NOW, 24)
    assert short.text.startswith("I've only been watching for 20 h.")
    assert preview_rule(BuyRule("Hex Pot", 3), tape, snaps, FeeModel(), NOW).text.startswith("No history yet")
    approx = preview_rule(BuyRule("Lunar Snake", 3), [obs("z", 1.0, 2, model="Viper")], {}, FeeModel(), NOW,
                          current_floor=1.5, tape_start=NOW - 48 * 3600)
    assert approx.approximate and "Approximate" in approx.text


# --- /doctor with injected fakes --------------------------------------------------------------


def test_doctor_checks_with_fakes(tmp_path):
    session_file = tmp_path / "tonnel.session"
    session_file.write_text("x")
    os.chmod(session_file, 0o644)

    async def no_2fa():
        return True, False

    async def go():
        s = settings(TG_API_ID=1, TG_API_HASH="h")
        deps = DoctorDeps(s, None, no_2fa, session_file, now=NOW)
        fa = await check_2fa(deps)
        assert fa.status == "fail" and fa.fix == "Telegram → Settings → Privacy and Security → Two-Step Verification"
        mode = await check_session_mode(deps)
        if os.name == "nt":
            # Windows has no POSIX modes; the check reports the profile-private default.
            assert mode.status == "ok"
        else:
            assert mode.status == "warn" and mode.fix == "chmod 600 data/*.session"
            os.chmod(session_file, 0o600)
            assert (await check_session_mode(deps)).status == "ok"
        assert (await check_session(deps)).status == "ok"
        missing = DoctorDeps(s, None, no_2fa, tmp_path / "none.session", now=NOW)
        sess = await check_session(missing)
        assert sess.status == "fail" and sess.fix == "python -m tonnel_autobuy login"

    asyncio.run(go())


def test_doctor_market_auth_without_tonnel_auth():
    class NoAuth(MockClient):
        has_auth = False

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(client=NoAuth("demo", now=NOW))
        try:
            check = await check_market_auth(DoctorDeps(engine.settings, engine, None, None, now=clock()))
            assert check.status == "warn" and "public listings only" in check.text and "login" in check.fix
        finally:
            await storage.close()

    asyncio.run(go())


def test_doctor_without_credentials_shows_fail_lines_not_a_crash():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(ADMIN_IDS="", BOT_TOKEN=""))
        try:
            started = asyncio.get_running_loop().time()
            checks = await run_doctor(DoctorDeps(engine.settings, engine, None, None, now=clock()))
            assert asyncio.get_running_loop().time() - started < 10
            text = format_doctor(checks)
            assert "✗ Bot config" in text and "✗ Telegram session: not logged in" in text
            assert "! Fees are assumptions" in text
            assert len(checks) >= 12 and all(isinstance(c, Check) and (c.status == "ok" or c.fix or c.status == "warn")
                                             for c in checks)
            assert "Market reachable (120 ms)" in text
            assert "✓ Clock: skew +0 s" in text  # read from the reachability answer, not before it
        finally:
            await storage.close()

    asyncio.run(go())


def test_doctor_clock_warns_when_no_date_header_was_seen():
    from tonnel_autobuy.client import MockClient

    class NoDate(MockClient):
        def _answered(self) -> None:
            pass

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(client=NoDate("demo"))
        try:
            text = format_doctor(await run_doctor(DoctorDeps(engine.settings, engine, None, None, now=clock())))
            assert "! Clock: not checked: no market Date header seen" in text and "NTP" in text
        finally:
            await storage.close()

    asyncio.run(go())
