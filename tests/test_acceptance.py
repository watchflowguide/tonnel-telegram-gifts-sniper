"""End-to-end acceptance checks on the F1-F8 demo fixtures (offline)."""

import asyncio

from helpers import HC, JB, LS, SA, demo_engine, settings

from tonnel_autobuy.bot import BotContext, status_text
from tonnel_autobuy.fx import TonRate
from tonnel_autobuy.peers import FakeTgValue, Peers, TgValueService, build_snapshot, write_snapshot
from tonnel_autobuy.pricing import FeeModel


async def _live_two_cycles(**kw):
    s = settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=20, CONFIRM_ABOVE_TON=100, **kw)
    engine, storage, client, notifier, clock = await demo_engine(s, force_dry=False)
    await engine.arm_live()
    await engine.run_cycle()
    client.next_cycle()
    clock.advance(120)
    await engine.run_cycle()
    return engine, storage, client, notifier


def test_f1_floor_is_cluster_with_confidence_label():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            card = next(m for m in notifier.messages if m.startswith("DEAL Lunar Snake #4410"))
            assert card.split("\n")[0] == (
                "DEAL Lunar Snake #4410 1.00 TON · floor 1.60 (cluster, 4 comparables, high) · you pay 1.10 incl. "
                "10% fee · resold at floor: +0.50 TON net (+45%) · break-even 1.45")
            assert "Floor 1.60 (high, 4 comparables)" in card and "PRACTICE: would buy" in card
        finally:
            await storage.close()

    asyncio.run(go())


def test_traps_locks_and_losers_never_reach_buy_even_live():
    async def go():
        engine, storage, client, notifier = await _live_two_cycles()
        try:
            for lid in (LS(4412), JB(53), JB(54), LS(4416)):  # F2, F3, F5, F4
                assert lid not in client.bought
            assert LS(4410) in client.bought and LS(4417) in client.bought  # F1, F6 (auto, healthy)
            f2 = await storage.decisions_for(LS(4412))
            assert f2[0].reason_code == "LOSES_AFTER_FEES" and f2[0].detail["text"] == "loses 0.07 TON after fees"
            assert not any("#4412" in m for m in notifier.messages)  # never alerted
            f3 = await storage.decisions_for(JB(53))
            assert f3[0].reason_code == "LOCKED" and "6 d" in f3[0].detail["text"]
            assert (await storage.decisions_for(JB(54)))[0].reason_code == "TRAP_ASSET"
            f4 = next(m for m in notifier.messages if m.startswith("DEAL Lunar Snake #4416"))
            assert "Asking first: price is 10% of floor" in f4 and "Waiting for your tap" in f4
        finally:
            await storage.close()

    asyncio.run(go())


def test_f7_f8_downgrade_with_second_opinion(tmp_path):
    async def go():
        portals = tmp_path / "floors_portals.json"
        engine, storage, client, notifier, clock = await demo_engine(settings(PEER_FLOOR_FILES=str(portals),
                                                                              CONFIRM_ABOVE_TON=100))
        write_snapshot(portals, build_snapshot("portals", FeeModel(0, 5, 0.1),
                                               {"Spy Agaric": {"floor": 4.6}, "Lunar Snake": {"floor": 1.7}}, clock()))
        tg = TgValueService(FakeTgValue({"HomemadeCake-1234": {"currency": "USD", "average_price": 300,
                                                                "last_sale_price": 325, "listed_count": 9}}),
                            clock=clock)
        engine.peers = Peers(engine.settings, "tonnel", tg=tg, rate=TonRate("x", fixed=1.25), clock=clock)
        try:
            await engine.run_cycle()
            f7 = next(m for m in notifier.messages if m.startswith("DEAL Spy Agaric #777"))
            assert "You pay 5.00 TON (incl. 10% fee)" in f7
            assert "Asking first: cheaper on Portals (4.60 all-in)" in f7
            assert "Elsewhere: Portals 4.60 (all-in)" in f7
            f8 = next(m for m in notifier.messages if m.startswith("DEAL Homemade Cake #1234"))
            assert "Asking first: above Telegram's last sale (2.60 TON)" in f8
            assert "Floor 4.00 (low, 1 comparable)" in f8
            for lid in (SA(777), HC(1234)):
                card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == lid)
                assert card.payload["mode"] == "confirm"  # auto rules were downgraded, not blocked
        finally:
            await storage.close()

    asyncio.run(go())


def test_status_fee_line_unverified():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            text = await status_text(BotContext(engine.settings, storage, engine, client))
            assert "Fees: buy 10% · sell 0% · withdraw 0 TON (unverified)" in text
            assert "Top skip reasons (24 h): OVER_MAX" in text
            assert text.startswith("Mode: PRACTICE")
            verified = await status_text(BotContext(settings(FEES_VERIFIED=True), storage, engine, client))
            assert "(unverified)" not in verified
        finally:
            await storage.close()

    asyncio.run(go())
