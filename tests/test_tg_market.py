"""Telegram's own resale market (TG_MARKET): quote math, rate limit, FloodWait, auto-disable, peers wiring."""

import asyncio

import pytest
from helpers import NOW, Clock, L, demo_engine, settings
from telethon.errors import FloodWaitError

from tonnel_autobuy import tg_market
from tonnel_autobuy.fx import TonRate
from tonnel_autobuy.peers import Peers, read_snapshot
from tonnel_autobuy.pricing import FeeModel, FloorEstimate, assess
from tonnel_autobuy.rules import BuyRule
from tonnel_autobuy.tg_market import (MockTgMarket, TelethonTgMarket, TgMarket, build_tg_market, offer_ton,
                                      quote_from_offers)

OFFERS = {"Lunar Snake": [{"model": "Albino (1.2%)", "ton": 1.95}, {"model": "Albino", "ton": 2.10},
                          {"model": "Cobra", "stars": 150}, {"model": "Cobra", "ton": 1.40}]}


def test_offer_prices_in_ton_and_stars():
    assert offer_ton({"ton": 1.5}, stars_usd=0.013, usd_per_ton=3.0) == 1.5
    assert offer_ton({"stars": 300}, stars_usd=0.013, usd_per_ton=3.0) == pytest.approx(1.3)
    assert offer_ton({"stars": 300}, stars_usd=0.013, usd_per_ton=None) is None  # never guessed without a rate
    q = quote_from_offers("Lunar Snake", "Albino", OFFERS["Lunar Snake"], 4, NOW, stars_usd=0.013, usd_per_ton=1.25)
    assert (q.floor_ton, q.second_ton, q.listed) == (1.95, 2.10, 2)  # rarity suffixes do not split a model
    whole = quote_from_offers("Lunar Snake", None, OFFERS["Lunar Snake"], 40, NOW, stars_usd=0.013, usd_per_ton=1.25)
    assert whole.floor_ton == 1.40 and whole.listed == 40


def test_rate_limit_cache_and_floodwait():
    clock = Clock()
    fake = MockTgMarket(OFFERS)
    market = TgMarket(fake, min_interval=5, ttl=600, rate=TonRate("x", fixed=1.25), clock=clock)

    async def go():
        assert (await market.quote("Lunar Snake", "Albino")).floor_ton == 1.95
        assert (await market.quote("Lunar Snake", "Cobra")).floor_ton == 1.40
        assert fake.calls == ["Lunar Snake"]  # one request, then the 10 min cache
        assert await market.quote("Desk Calendar") is None  # within min_interval: no second request
        assert fake.calls == ["Lunar Snake"]
        clock.advance(6)
        await market.quote("Desk Calendar")
        assert fake.calls == ["Lunar Snake", "Desk Calendar"]

        async def flood(collection, limit):
            raise FloodWaitError(request=None, capture=30)

        market.fetch = flood
        clock.advance(601)
        assert (await market.quote("Lunar Snake")).floor_ton == 1.40  # stale cache beats nothing
        assert "FloodWait" in market.status()
        market.fetch = fake
        clock.advance(10)
        assert (await market.quote("Jelly Bunny")) is None and fake.calls[-1] == "Desk Calendar"  # still paused
        clock.advance(30)
        await market.quote("Jelly Bunny")
        assert fake.calls[-1] == "Jelly Bunny"

    asyncio.run(go())


def test_disables_itself_when_telethon_lacks_the_tl_methods(monkeypatch):
    from telethon.tl.functions import payments

    monkeypatch.delattr(payments, "GetResaleStarGiftsRequest")
    assert TelethonTgMarket.missing_methods() == ["GetResaleStarGiftsRequest"]
    market = build_tg_market(settings(TG_MARKET=True), session=object())
    assert market is not None and not market.enabled
    assert "lacks GetResaleStarGiftsRequest" in market.status()
    assert asyncio.run(market.quote("Lunar Snake")) is None
    assert build_tg_market(settings(TG_MARKET=False), session=object()) is None  # opt-in only
    no_session = build_tg_market(settings(TG_MARKET=True), session=None)
    assert not no_session.enabled and "no Telegram session" in no_session.status()


def test_peers_view_and_snapshot_include_telegram(tmp_path):
    clock = Clock()
    rate = TonRate("x", fixed=1.25)
    market = TgMarket(MockTgMarket(OFFERS), min_interval=0, rate=rate, clock=clock)
    peers = Peers(settings(PEER_FLOOR_FILES=""), "tonnel", rate=rate, clock=clock, tg_market=market)
    assert peers.enabled

    async def go():
        listing = L("1", 1.0, model="Albino")
        deal = assess(listing, FloorEstimate(1.6, "cluster", 6, "high"), FeeModel(10, 0, 0), [])
        view = await peers.view(listing, BuyRule("Lunar Snake", 2.5, model="Albino"), deal)
        [q] = [q for q in view.quotes if q.market == "telegram"]
        assert q.level == "model" and q.floor == 1.95
        assert q.net_if_sold == pytest.approx(1.95 * (1 - tg_market.TG_MARKET_FEES.sell_pct / 100))
        assert "Telegram 1.95" in view.elsewhere_line()
        lines = await peers.status_lines()
        assert any(line.startswith("Telegram resale: ok") for line in lines)

    asyncio.run(go())
    from tonnel_autobuy.peers import write_snapshot

    path = tmp_path / "floors_telegram.json"
    write_snapshot(path, market.snapshot(clock()))
    src = read_snapshot(str(path), "tonnel", clock(), 600)
    assert src.status == "ok" and src.market == "telegram"
    coll = src.data["collections"]["lunarsnake"]
    assert coll["floor"] == 1.40 and coll["models"]["albino"] == 1.95  # the collection floor is not one model's


def test_engine_writes_floors_telegram_json_and_ignores_tg_market_when_off(tmp_path):
    async def go():
        clock = Clock()
        rate = TonRate("x", fixed=1.25)
        market = TgMarket(MockTgMarket(OFFERS), min_interval=0, rate=rate, clock=clock)
        peers = Peers(settings(), "tonnel", rate=rate, clock=clock, tg_market=market)
        engine, storage, *_ = await demo_engine(clock=clock, peers=peers, snapshot_path=tmp_path / "floors_tonnel.json")
        try:
            await engine.run_cycle()  # the Lunar Snake candidates fetch a Telegram quote
            clock.advance(120)  # past FLOOR_CACHE_TTL (snapshot cadence), within the 10 min quote cache
            await engine.run_cycle()  # the next snapshot write includes floors_telegram.json
            src = read_snapshot(str(tmp_path / "floors_telegram.json"), "tonnel", clock(), 10**6)
            assert src.status == "ok" and "lunarsnake" in src.data["collections"]
        finally:
            await storage.close()

        off = Peers(settings(), "tonnel", clock=clock, tg_market=None)
        engine, storage, *_ = await demo_engine(clock=clock, peers=off, snapshot_path=tmp_path / "b" / "floors_tonnel.json")
        try:
            await engine.run_cycle()
            assert not (tmp_path / "b" / "floors_telegram.json").exists()
        finally:
            await storage.close()

    asyncio.run(go())
