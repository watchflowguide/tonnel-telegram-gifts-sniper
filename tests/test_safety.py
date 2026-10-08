import asyncio

import pytest
from helpers import ADMIN, LS, NOW, Clock, L, demo_engine, settings

from tonnel_autobuy.bot import BotContext, dry_off_gate, safety_for, status_text
from tonnel_autobuy.client import MockClient
from tonnel_autobuy.engine import Engine
from tonnel_autobuy.models import BuyResult
from tonnel_autobuy.notifier import MemoryNotifier
from tonnel_autobuy.rules import BuyRule
from tonnel_autobuy.safety import PRESETS, SafetyState, balance_mismatch, floor_crash, losing_streak, preset_diff, safety_text
from tonnel_autobuy.storage import Position, Storage


def test_live_until_expiry_flips_to_practice():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False, LIVE_ARM_HOURS=24),
                                                                     force_dry=False)
        try:
            assert await engine.arm_live() is None
            assert not await engine.is_dry()
            clock.advance(24 * 3600 - 1)
            assert not await engine.is_dry()
            clock.advance(2)
            assert await engine.is_dry()
            assert await engine.live_until() is None
            assert "Live mode ended after 24 h; back to practice. /dry off to renew." in notifier.messages
            assert await engine.is_dry()  # stays practice
        finally:
            await storage.close()

    asyncio.run(go())


def test_restart_starts_in_practice_unless_gate_open():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.start_in_practice()
            assert await engine.is_dry()
            await engine.arm_live()
            await engine.start_in_practice()
            assert not await engine.is_dry()  # the 24 h gate survives a restart
        finally:
            await storage.close()

    asyncio.run(go())


def test_dry_off_with_no_data_requires_confirm():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.start_in_practice()
            ctx = BotContext(engine.settings, storage, engine, client)
            engine.last_mark_at = clock()  # no search while building the report
            text, kb = await dry_off_gate(ctx, confirm=False)
            assert "type /dry off confirm to go live anyway" in text
            assert "PRACTICE 7d" in text  # the practice report comes first
            labels = [b.text for row in kb.inline_keyboard for b in row]
            assert labels == ["Stay in practice"]
            assert await engine.is_dry()
            text, _ = await dry_off_gate(ctx, confirm=True)
            assert text.startswith("LIVE for 24 h") and not await engine.is_dry()
            assert await engine.live_until() == pytest.approx(clock() + 24 * 3600)
        finally:
            await storage.close()

    asyncio.run(go())


def test_env_lock_still_refuses():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=True), force_dry=False)
        try:
            ctx = BotContext(engine.settings, storage, engine, client)
            text, kb = await dry_off_gate(ctx, confirm=True)
            assert text.startswith("Live mode is locked: DRY_RUN=true") and kb is None
            assert await engine.arm_live() is not None
            await storage.set_dry(False)  # even a stored setting cannot override the lock
            assert await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


def test_crash_breaker_pauses_only_the_affected_rule():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(MAX_BUYS_PER_HOUR=10))
        try:
            await engine.run_cycle()
            clock.advance(120)
            client.crash("Lunar Snake", "Cobra", 0.7)
            await engine.run_cycle()
            assert "Rule #2 paused for 60 min: floor −30% in 1 h" in notifier.messages
            assert await storage.get_float("rule_paused_until:2") == pytest.approx(clock() + 3600)
            assert await storage.get_float("rule_paused_until:1") is None
            assert floor_crash([(NOW - 600, 1.0), (NOW, 0.75)], NOW, 20) == pytest.approx(25)
            assert floor_crash([(NOW - 600, 1.0), (NOW, 0.85)], NOW, 20) is None
        finally:
            await storage.close()

    asyncio.run(go())


def test_losing_streak_flips_to_practice():
    def p(i, cost, net):
        return Position(i, "l", "C", None, "t", cost, cost, 1, False, "ok", NOW, None, None, mark_net=net)

    assert losing_streak([p(3, 1.0, 0.8), p(2, 1.0, 0.85), p(1, 1.0, 0.7)], 3, 10)
    assert not losing_streak([p(3, 1.0, 0.8), p(2, 1.0, 0.95), p(1, 1.0, 0.7)], 3, 10)
    assert not losing_streak([p(1, 1.0, 0.5)], 3, 10)

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.arm_live()
            for i in range(3):
                pid = await storage.add_purchase(L(f"g{i}", 1.0), cost_ton=1.1, floor_ton=None, rule_id=1, dry=False,
                                                 status="ok")
                await storage.add_mark(pid, clock(), 0.8, 0.8, "high")
            await engine.tick(marks_search=False)
            assert await engine.is_dry()
            assert any("3 losing live buys in a row" in m for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())


def test_panic_expires_pending_cards_and_blocks_buys():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            pending = await storage.list_pending(("pending",))
            assert pending
            n = await engine.panic()
            assert n == len(pending) and await storage.list_pending(("pending",)) == []
            assert "Everything stopped" in notifier.messages[-1]
            res = await engine.tap("b", pending[0].token, ADMIN)
            assert not res.ok and "panic" in res.text
            assert await engine.is_dry() and await engine.is_paused()
            assert await engine.arm_live() is not None  # /dry off refused until /resume
            await engine.resume()
            assert await engine.is_dry()  # still practice until a new /dry off
            assert await engine.arm_live() is None
        finally:
            await storage.close()

    asyncio.run(go())


def test_panic_flag_persists_across_reopen(tmp_path):
    async def go():
        path = str(tmp_path / "db.sqlite3")
        storage = await Storage(path).open()
        engine = Engine(settings(DRY_RUN=False), MockClient(), storage, MemoryNotifier())
        await engine.panic()
        await storage.close()
        storage = await Storage(path).open()
        try:
            engine = Engine(settings(DRY_RUN=False), MockClient(), storage, MemoryNotifier())
            assert await engine.is_panic() and await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


def test_safety_text_reflects_settings():
    s = settings(MAX_PRICE_TON=10, DAILY_BUDGET_TON=30, MAX_BUYS_PER_HOUR=3, CRASH_PCT=20, LOSS_STREAK_N=3)
    st = SafetyState(live=True, locked=False, live_until=NOW + 3600, panic=False, paused=False, spent_today=6.4,
                     buys_last_hour=1, max_price=10, daily_budget=30, max_per_hour=3)
    text = safety_text(st, s)
    assert "Mode: LIVE until" in text and "practice after that" in text
    assert "Today (UTC day) I can spend up to 30 TON (6.4 used, 23.6 left)." in text
    assert "One gift: max 10 TON incl. fees. At most 3 buys per hour" in text
    assert "I never withdraw, transfer, list or sell." in text
    assert "Breakers: floor crash 20%/1h, 3 losing buys in a row" in text

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(s)
        try:
            await engine.run_cycle()  # one practice buy of 1.10 TON all-in
            state = await engine.safety_state()
            assert (state.max_price, state.daily_budget, state.max_per_hour) == (10, 30, 3)
            assert state.locked and not state.live and state.spent_today == pytest.approx(1.1 + 3.3)
            ctx = BotContext(engine.settings, storage, engine, client)
            assert (await safety_for(ctx)).startswith("Mode: PRACTICE, locked by DRY_RUN=true in .env.")
            assert (await status_text(ctx)).startswith("Mode: PRACTICE")  # /status starts with /safety
        finally:
            await storage.close()

    asyncio.run(go())


def test_limits_use_cost_including_buy_fee():
    async def go():
        rules = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="demo")]
        s = settings(BUY_FEE_PCT=10, DAILY_BUDGET_TON=1.05)
        engine, storage, client, notifier, clock = await demo_engine(s, rules=rules)
        try:
            await engine.run_cycle()  # 814410 lists at 1.00 but costs 1.10 all-in > 1.05 budget
            assert await storage.recent_purchases() == []
            assert any(m.startswith("SKIP (daily budget: spent 0.00 + 1.10 > 1.05)") for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())


def test_balance_mismatch_and_auto_pause_on_executor_errors():
    assert balance_mismatch(20.0, 8.0, 2.0, 0.5) == (12.0, 2.0)
    assert balance_mismatch(20.0, 17.8, 2.0, 0.5) is None
    assert balance_mismatch(None, 8.0, 0, 0.5) is None

    class Failing(MockClient):
        async def buy(self, listing):
            self.bought.append(listing.id)
            return BuyResult(False, "Gift already sold")

    async def go():
        clock = Clock()
        client = Failing("demo", now=clock())
        engine, storage, _, notifier, _ = await demo_engine(settings(DRY_RUN=False, AUTO_PAUSE_ERRORS=2,
                                                                     MAX_BUYS_PER_HOUR=10),
                                                            force_dry=False, client=client, clock=clock)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert len(client.bought) >= 2 and await engine.is_paused()
            assert any("buys failed in a row" in m for m in notifier.messages)
            assert any(m.startswith("BUY FAILED: Lunar Snake #4410") for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())


def test_shadow_rule_never_calls_buy_even_live():
    async def go():
        rules = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot", state="shadow")]
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), rules=rules,
                                                                     force_dry=False)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert client.bought == []
            [p] = await storage.recent_purchases()
            assert p["dry"] and p["message"] == "SHADOW_RULE" and p["listing_id"] == LS(4410)
            assert (await storage.get_rule(1)).state == "shadow"
            await engine.promote(1)  # the only path to live
            assert (await storage.get_rule(1)).state == "live"
        finally:
            await storage.close()

    asyncio.run(go())


def test_preset_diff_and_cap():
    s = settings(MAX_PRICE_TON=10, DAILY_BUDGET_TON=50, MAX_BUYS_PER_HOUR=3)
    current = {"MAX_PRICE_TON": 10, "DAILY_BUDGET_TON": 50, "MAX_BUYS_PER_HOUR": 3, "MIN_DISCOUNT_PCT": 0}
    careful = preset_diff(current, "careful", s)
    assert careful[0] == "MAX_PRICE_TON: 10 → 5  ↓"
    bold = preset_diff(current, "bold", s)
    assert bold[0] == "MAX_PRICE_TON: 10 → 10 (capped by .env)"
    assert set(PRESETS) == {"careful", "normal", "bold"}

    async def go():
        engine, storage, client, notifier, clock = await demo_engine(s)
        try:
            await storage.set_setting("limit:MAX_PRICE_TON", 5)
            await storage.set_setting("limit:DAILY_BUDGET_TON", 500)  # a preset can never raise .env limits
            limits = await engine.effective_limits()
            assert (limits.max_price_ton, limits.daily_budget_ton) == (5, 50)
        finally:
            await storage.close()

    asyncio.run(go())


def test_shadow_rule_writes_shadow_fills_and_marks_them_net_of_fees():
    from tonnel_autobuy import shadow

    async def go():
        rules = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot", state="shadow")]
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False, SELL_FEE_PCT=5),
                                                                     rules=rules, force_dry=False)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert client.bought == []  # a shadow rule never reaches the executor, even LIVE
            [fill] = await storage.shadow_fills(rule_id=1)
            [p] = await storage.recent_purchases()
            assert fill["subject_ref"] == LS(4410) and fill["meta"]["purchase_id"] == p["id"]
            assert fill["entry_price"] == 1.0 and fill["executable_price"] == pytest.approx(1.1)
            assert fill["entry_costs"] == pytest.approx(0.1) and fill["outcome"] == "open"
            # a ledger mark at floor 1.60: net exit 1.60 x (1 - 5%) = 1.52 > 1.10 all-in
            await storage.add_mark(p["id"], clock() + 1, 1.60, engine.fees.net_exit(1.60), "high")
            assert await shadow.mark(storage, clock() + 2, sniped_window=120) == 1
            [fill] = await storage.shadow_fills(rule_id=1)
            assert fill["mark_price"] == pytest.approx(1.52) and fill["outcome"] == "gain"
            st = shadow.summarize_fills([fill])
            assert st.n == 1 and st.net == pytest.approx(0.42)
            # the listing vanished right after the fill...
            await storage.db.execute("UPDATE observations SET last_seen=? WHERE listing_id=?", (fill["ts"] + 30, LS(4410)))
            # ...but the bot stopped polling (paused, /panic, shut down): no evidence, not sniped
            await shadow.mark(storage, fill["ts"] + 600, sniped_window=120)
            assert (await storage.shadow_fills(rule_id=1))[0]["outcome"] == "gain"
            # later answers for the same collection no longer list it: someone else bought it
            await storage.db.execute("UPDATE observations SET last_seen=? WHERE listing_id<>?", (fill["ts"] + 300, LS(4410)))
            await shadow.mark(storage, fill["ts"] + 600, sniped_window=120)
            assert (await storage.shadow_fills(rule_id=1))[0]["outcome"] == "sniped"
        finally:
            await storage.close()

    asyncio.run(go())
    assert shadow_outcomes() == ["gain", "loss", "flat", "open"]


def shadow_outcomes():
    from tonnel_autobuy.shadow import outcome_for

    return [outcome_for(1.0, 1.2), outcome_for(1.0, 0.9), outcome_for(1.0, 1.001), outcome_for(1.0, None)]


def test_panic_lives_in_kv_and_old_settings_flag_migrates(tmp_path):
    async def go():
        path = str(tmp_path / "db.sqlite3")
        storage = await Storage(path).open()
        await storage.set_setting("panic", "1")  # an earlier build kept the flag in settings
        await storage.close()
        storage = await Storage(path).open()
        try:
            assert await storage.kv_get("panic") == "1" and await storage.get_setting("panic") is None
            engine = Engine(settings(DRY_RUN=False), MockClient(), storage, MemoryNotifier())
            assert await engine.is_panic()
            await engine.resume()
            assert await storage.kv_get("panic") == "0" and not await engine.is_panic()
        finally:
            await storage.close()

    asyncio.run(go())
