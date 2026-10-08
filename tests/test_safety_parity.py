"""Regression tests for the safety fixes shared by the gift-trio bots (portals-autobuy, mrkt-autobuy and
tonnel-autobuy): the fail-closed live gate, a rule edit landing mid-buy, the graceful stop, expiring
state-changing buttons, rules.json max validation, the shared TON-rate request and the /buys wording."""

import asyncio
import json
import math

import pytest
from helpers import ADMIN, LS, NOW, Clock, L, demo_engine, settings

from tonnel_autobuy.bot import (
    BotContext,
    gate_action,
    preset_action,
    purchase_tag,
    resume_prompt,
    rule_action,
    shadow_action,
)
from tonnel_autobuy.callbacks import BUTTON_TTL_SEC, GateCB, button_fresh
from tonnel_autobuy.client import MockClient
from tonnel_autobuy.fx import TonRate
from tonnel_autobuy.pricing import FloorEstimate, FeeModel, assess
from tonnel_autobuy.rules import (
    BuyRule,
    RuleParseError,
    load_rules_file,
    match,
    near_miss,
    parse_addrule_args,
    rule_from_dict,
)

SNAKE = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="demo")]


class HookedClient(MockClient):
    """A hook runs inside balance(), i.e. after the first switch check and before the buy request."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.on_balance = None

    async def balance(self):
        if self.on_balance:
            hook, self.on_balance = self.on_balance, None
            await hook()
        return 1000.0


async def live_engine(rules=SNAKE, **over):
    clock = Clock()
    engine, storage, client, notifier, _ = await demo_engine(
        settings(DRY_RUN=False, **over), rules=rules, force_dry=False, clock=clock,
        client=HookedClient("demo", now=clock()))
    return engine, storage, client, notifier, clock


# --- the live gate fails closed ---------------------------------------------------------------------------


def test_fresh_database_with_dry_run_false_starts_in_practice_and_never_calls_buy():
    async def go():
        engine, storage, client, _, _ = await live_engine()
        try:
            await engine.start_in_practice()
            assert await engine.is_dry()
            report = await engine.run_cycle()
            assert client.bought == [] and report.bought == 0 and report.dry_bought == 1
            assert all(p["dry"] for p in await storage.recent_purchases())
        finally:
            await storage.close()

    asyncio.run(go())


@pytest.mark.parametrize("stored", ["inf", "nan", "garbage", str(NOW + 10 * 86400)])
def test_live_until_that_is_not_a_valid_gate_is_practice(stored):
    async def go():
        engine, storage, client, notifier, _ = await live_engine()
        try:
            await storage.set_setting("live_until", stored)
            await storage.set_dry(False)
            assert await engine.is_dry()
            await engine.run_cycle()
            assert client.bought == []
            if stored != "garbage":  # unreadable: simply no gate; the others are cleared with a notice
                assert await storage.get_setting("live_until") is None
                assert any("not valid" in m for m in notifier.messages)
            # A properly armed gate still works.
            assert await engine.arm_live() is None and not await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


# --- the rule as stored right before money moves ------------------------------------------------------------


@pytest.mark.parametrize("edit", [{"mode": "watch"}, {"max_price_ton": 1.0}, {"min_discount_pct": 60.0},
                                  {"enabled": False}])
def test_rule_edit_during_the_balance_call_stops_the_auto_buy(edit):
    async def go():
        engine, storage, client, _, _ = await live_engine()
        try:
            await engine.arm_live()
            rule_id = (await storage.list_rules())[0].id

            async def change():
                await storage.update_rule(rule_id, **edit)

            client.on_balance = change
            report = await engine.run_cycle()
            assert client.bought == [] and report.bought == 0, edit
            assert [p for p in await storage.recent_purchases() if not p["dry"]] == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_preset_minimum_discount_landing_mid_buy_stops_the_auto_buy():
    async def go():
        engine, storage, client, _, _ = await live_engine()
        try:
            await engine.arm_live()

            async def preset():
                await storage.set_setting("limit:MIN_DISCOUNT_PCT", 80)

            client.on_balance = preset
            await engine.run_cycle()
            assert client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_unchanged_rule_still_buys_live():
    async def go():
        engine, storage, client, _, _ = await live_engine()
        try:
            await engine.arm_live()
            report = await engine.run_cycle()
            assert client.bought == [LS(4410)] and report.bought == 1
        finally:
            await storage.close()

    asyncio.run(go())


# --- graceful stop ------------------------------------------------------------------------------------------


def test_drain_waits_for_the_buy_in_flight_and_refuses_new_ones():
    class SlowBuy(MockClient):
        async def buy(self, listing):
            self.bought.append(listing.id)
            await asyncio.sleep(0.05)
            return await super().buy(listing)

    async def go():
        clock = Clock()
        engine, storage, client, _, _ = await demo_engine(
            settings(DRY_RUN=False), rules=SNAKE, force_dry=False, clock=clock, client=SlowBuy("demo", now=clock()))
        try:
            await engine.arm_live()
            cycle = asyncio.create_task(engine.run_cycle())
            while not client.bought:
                await asyncio.sleep(0.005)
            assert await engine.drain(5)  # returns once the buy in flight has its answer and its row
            [p] = await storage.recent_purchases()
            assert p["status"] == "ok"
            await cycle
            spend = await engine._spend(SNAKE[0], L("x", 1.0), assess(L("x", 1.0), FloorEstimate(
                2.0, "cluster", 5, "high"), FeeModel(10, 0, 0), []), clock(), practice=True, detail="")
            assert spend.kind == "refused" and "shutting down" in spend.text
        finally:
            await storage.close()

    asyncio.run(go())


def test_drain_gives_up_after_its_timeout():
    async def go():
        engine, storage, _, _, _ = await live_engine()
        try:
            await engine._spend_lock.acquire()  # a buy that never answers
            assert not await engine.drain(0.05)
            engine._spend_lock.release()
        finally:
            await storage.close()

    asyncio.run(go())


# --- buttons that change state expire ---------------------------------------------------------------------


def test_button_fresh_fails_closed():
    assert button_fresh(int(NOW), NOW + 10, BUTTON_TTL_SEC)
    assert not button_fresh(int(NOW), NOW + BUTTON_TTL_SEC + 1, BUTTON_TTL_SEC)
    assert not button_fresh(0, NOW, BUTTON_TTL_SEC)  # no time on the button: stale
    assert not button_fresh(int(NOW + 3600), NOW, BUTTON_TTL_SEC)  # from the future


def test_old_resume_button_cannot_undo_a_newer_panic_or_breaker():
    async def go():
        engine, storage, client, _, clock = await live_engine()
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            old = int(clock())  # a [Confirm resume] or breaker [Resume] sent now...
            old_nonce = await engine.issue_resume_nonce()
            clock.advance(BUTTON_TTL_SEC + 1)
            await engine.panic()  # ...and a /panic later
            text = await gate_action(ctx, "resume", old, old_nonce)
            assert "out of date" in text and await engine.is_panic() and await engine.is_paused()
            _, kb = await resume_prompt(ctx)  # a fresh /resume
            fresh = GateCB.unpack(kb.inline_keyboard[0][0].callback_data)
            text = await gate_action(ctx, "resume", fresh.t, fresh.n)
            assert text.startswith("Resumed") and not await engine.is_panic() and not await engine.is_paused()
            assert "practice" in text  # /panic cleared the gate: practice until a new /dry off
            assert "out of date" in await gate_action(ctx, "live", int(clock()))  # legacy button never arms LIVE
        finally:
            await storage.close()

    asyncio.run(go())


def test_crash_resume_button_works_only_while_its_pause_runs():
    async def go():
        engine, storage, client, _, clock = await live_engine(CRASH_PAUSE_MIN=60)
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            sent = int(clock())
            await storage.set_setting("rule_paused_until:1", clock() + 3600)
            clock.advance(3601)
            await storage.set_setting("rule_paused_until:1", clock() + 3600)  # a newer crash pause
            assert "out of date" in await rule_action(ctx, "resume", 1, sent)
            assert await storage.get_float("rule_paused_until:1") is not None
            assert await rule_action(ctx, "resume", 1, int(clock())) == "Rule #1 resumed"
            assert await storage.get_float("rule_paused_until:1") is None
        finally:
            await storage.close()

    asyncio.run(go())


def test_hindsight_and_preset_buttons_expire():
    async def go():
        engine, storage, client, _, clock = await live_engine()
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            rule_id = (await storage.list_rules())[0].id
            await storage.update_rule(rule_id, state="shadow")
            sent = int(clock())
            clock.advance(86400 + 1)
            assert "out of date" in await shadow_action(ctx, "live", rule_id, sent)
            assert "out of date" in await shadow_action(ctx, "del", rule_id, sent)
            assert (await storage.get_rule(rule_id)).state == "shadow"
            assert "is live" in await shadow_action(ctx, "live", rule_id, int(clock()))

            sent = int(clock())
            clock.advance(BUTTON_TTL_SEC + 1)
            assert "out of date" in await preset_action(ctx, "ok", "careful", sent)
            assert await storage.settings_with_prefix("limit:") == {}
            assert await preset_action(ctx, "ok", "careful", int(clock())) == "Preset careful applied"
            assert await storage.settings_with_prefix("limit:") != {}
        finally:
            await storage.close()

    asyncio.run(go())


def test_near_miss_buttons_expire_with_their_card():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            near = (await storage.list_pending(("near",)))[0]
            clock.advance(near.expires_at - clock() + 1)
            res = await engine.near_action("r", near.token, ADMIN)
            assert not res.ok and "Expired" in res.text
            assert (await storage.get_rule(near.rule_id)).max_price_ton == 2.75
        finally:
            await storage.close()

    asyncio.run(go())


# --- rules.json: a max of 0 is rejected, and an old stored row cannot crash the cycle ---------------------


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", "abc"])
def test_rules_json_rejects_a_max_that_is_not_positive(value, tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"collection": "Lunar Snake", "max_price_ton": value}]), encoding="utf-8")
    with pytest.raises(RuleParseError):
        load_rules_file(path)
    with pytest.raises(RuleParseError):
        parse_addrule_args(f"Lunar Snake max={value}")
    assert rule_from_dict({"collection": "Lunar Snake", "max": 2.5}).max_price_ton == 2.5


def test_near_miss_never_divides_by_a_zero_max():
    listing = L("n", 2.60)
    deal = assess(listing, FloorEstimate(3.2, "cluster", 6, "high"), FeeModel(10, 0, 0), [])
    for bad in (0.0, -1.0, math.nan):
        assert near_miss(BuyRule("Lunar Snake", bad, model="Albino"), listing, deal) is None

    async def go():  # a stored row with max 0 (written before validation): the cycle still completes
        engine, storage, _, _, _ = await demo_engine()
        try:
            await storage.update_rule(3, max_price_ton=0.0)
            report = await engine.run_cycle()
            assert report.rules >= 1
        finally:
            await storage.close()

    asyncio.run(go())


# --- second opinion: a buy never waits for the TON rate request -------------------------------------------


def test_ton_rate_shares_one_request_between_callers():
    calls = []

    async def fetch(url):
        calls.append(url)
        await asyncio.sleep(0.02)
        return {"rates": {"TON": {"prices": {"USD": 1.25}}}}

    async def go():
        rate = TonRate("https://example.invalid/rates", ttl=600, fetch=fetch)
        values = await asyncio.gather(*(rate.usd_per_ton() for _ in range(5)))
        assert values == [1.25] * 5 and len(calls) == 1

    asyncio.run(go())


# --- /buys always names the mode ---------------------------------------------------------------------------


def test_buys_tag_names_practice_or_live():
    assert purchase_tag(True, "dry") == "PRACTICE bought"
    assert purchase_tag(False, "ok") == "LIVE bought"
    assert purchase_tag(False, "unknown") == "LIVE result unknown"
    assert purchase_tag(False, "failed") == "LIVE failed"


# --- a price that is not a number fails closed ------------------------------------------------------------


def test_nan_or_infinite_price_never_passes_the_filters_or_the_limits():
    from tonnel_autobuy.client import parse_listings
    from tonnel_autobuy.engine import check_limits_detail
    from tonnel_autobuy.pricing import classify

    raw = json.loads('[{"gift_id": 1, "gift_num": 1, "name": "Lunar Snake", "asset": "TON", "price": NaN},'
                     ' {"gift_id": 2, "gift_num": 2, "name": "Lunar Snake", "asset": "TON", "price": Infinity},'
                     ' {"gift_id": 3, "gift_num": 3, "name": "Lunar Snake", "asset": "TON", "price": 1.5}]')
    assert [x.id for x in parse_listings(raw)] == ["3"]  # json accepts NaN/Infinity; the parser drops them
    for bad in (math.nan, math.inf):
        listing = L("x", bad)
        assert any(f.code == "BAD_PRICE" and f.blocking for f in classify(listing, "tonnel", NOW))
        deal = assess(listing, FloorEstimate(2.0, "cluster", 5, "high"), FeeModel(10, 0, 0), [])
        assert match(BuyRule("Lunar Snake", 5, model="Albino"), listing, deal).code == "BAD_PRICE"
        hit = check_limits_detail(bad, max_price=10, daily_budget=50, spent_today=0, max_per_hour=3, buys_last_hour=0)
        assert hit is not None and hit[0] == "BAD_PRICE"
