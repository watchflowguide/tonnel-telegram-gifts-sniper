import asyncio
import json
from pathlib import Path

import pytest
from helpers import NOW, Clock, L, demo_engine, settings
from telethon.errors import FloodWaitError

from tonnel_autobuy.fx import TonRate, parse_tonapi_rate
from tonnel_autobuy.peers import (
    FakeTgValue,
    PeerQuote,
    Peers,
    PeerView,
    TgValueService,
    best_exit,
    best_quote_for,
    build_snapshot,
    guards,
    minor_to_major,
    parse_tg_value,
    peer_key,
    read_snapshot,
    write_snapshot,
)
from tonnel_autobuy.pricing import FeeModel, FloorEstimate, assess
from tonnel_autobuy.rules import BuyRule, stricter_mode

FIXTURES = Path(__file__).parent / "fixtures" / "peers"


def snapshot(market="portals", ts=NOW, **colls):
    floors = colls or {"Lunar Snake": {"floor": 1.75, "n": 9, "models": {"Albino": 1.8}}}
    return build_snapshot(market, FeeModel(0, 5, 0.1), floors, ts)


def test_snapshot_round_trip(tmp_path):
    path = tmp_path / "floors_portals.json"
    write_snapshot(path, snapshot())
    src = read_snapshot(str(path), "tonnel", NOW + 10, 600)
    assert src.status == "ok" and src.market == "portals" and src.age_s == 10
    q = best_quote_for(src, "lunar snake", "ALBINO", "model")
    assert (q.floor, q.all_in_cost, q.net_if_sold) == (1.8, 1.8, pytest.approx(1.61))
    assert best_quote_for(src, "Lunar Snake", None, "collection").floor == 1.75
    assert list(tmp_path.iterdir()) == [path]  # no temp files left behind
    assert "session" not in path.read_text() and "token" not in path.read_text()


def test_own_snapshot_strips_rarity_and_keeps_no_secrets(tmp_path):
    async def go():
        s = settings(TONNEL_INIT_DATA="query_id=SECRET&hash=abc", BOT_TOKEN="123:SECRET")
        engine, storage, client, notifier, clock = await demo_engine(s, snapshot_path=tmp_path / "floors_tonnel.json")
        try:
            await engine.run_cycle()
        finally:
            await storage.close()
        text = (tmp_path / "floors_tonnel.json").read_text()
        assert "SECRET" not in text and "session" not in text
        src = read_snapshot(str(tmp_path / "floors_tonnel.json"), "portals", clock(), 600)
        assert src.status == "ok" and src.market == "tonnel"
        assert src.fees() == FeeModel(10, 0, 0)  # the fees travel inside the file
        snake = src.data["collections"]["lunarsnake"]
        assert snake["name"] == "Lunar Snake" and snake["floor"] == 0.97
        assert snake["models"]["albino"] == 1.0 and snake["models"]["cobra"] == 0.97  # "Albino (1.2%)" -> albino
        assert "jellybunny" in src.data["collections"]
        assert read_snapshot(str(tmp_path / "floors_tonnel.json"), "tonnel", clock(), 600).status == "own"

    asyncio.run(go())


def test_truncated_or_invalid_file_is_ignored(tmp_path):
    good = json.dumps(snapshot())
    bad = tmp_path / "floors_portals.json"
    bad.write_text(good[: len(good) // 2])
    assert read_snapshot(str(bad), "tonnel", NOW, 600).status == "invalid"
    other = tmp_path / "floors_mrkt.json"
    other.write_text(json.dumps({"schema": 2, "market": "mrkt", "collections": {}}))
    assert read_snapshot(str(other), "tonnel", NOW, 600).status == "invalid"
    assert read_snapshot(str(tmp_path / "floors_getgems.json"), "tonnel", NOW, 600).status == "missing"


def test_names_share_one_key():
    assert peer_key("Durov's Cap") == peer_key("DurovsCap") == peer_key("durov's cap") == "durovscap"
    assert peer_key("Plush Pepe (1.5%)") == "plushpepe"  # Tonnel rarity suffix
    assert peer_key("Plush Pepes") == "plushpepe"  # alias from aliases.json


def test_stale_and_own_market_files_are_ignored(tmp_path):
    stale = tmp_path / "floors_mrkt.json"
    write_snapshot(stale, snapshot("mrkt", ts=NOW - 3600))
    own = tmp_path / "floors_tonnel.json"
    write_snapshot(own, snapshot("tonnel"))
    assert read_snapshot(str(stale), "tonnel", NOW, 600).status == "stale"
    assert read_snapshot(str(own), "tonnel", NOW, 600).status == "own"

    async def go():
        s = settings(PEER_FLOOR_FILES=f"{stale},{own}")
        peers = Peers(s, "tonnel", clock=lambda: NOW)
        view = await peers.view(L("x", 1.0), BuyRule("Lunar Snake", 5), None)
        assert view.quotes == [] and view.missing == ["mrkt"]
        assert view.elsewhere_line() == "Elsewhere: MRKT n/a (all-in)"
        lines = await peers.status_lines()
        assert any("MRKT: stale" in line for line in lines) and any("own market" in line for line in lines)

    asyncio.run(go())


def test_deleting_a_sibling_folder_only_removes_that_market(tmp_path):
    portals = tmp_path / "portals" / "floors_portals.json"
    write_snapshot(portals, snapshot("portals"))
    gone = tmp_path / "mrkt-autobuy-deleted" / "floors_mrkt.json"

    async def go():
        peers = Peers(settings(PEER_FLOOR_FILES=f"{portals},{gone}"), "tonnel", clock=lambda: NOW)
        view = await peers.view(L("x", 1.0), BuyRule("Lunar Snake", 5), None)
        assert [q.market for q in view.quotes] == ["portals"] and view.missing == ["mrkt"]

    asyncio.run(go())


def test_fiat_conversion_uses_currency_exponent():
    assert minor_to_major(530, "USD") == 5.30
    assert minor_to_major(530, "JPY") == 530
    assert minor_to_major(5300, "KWD") == 5.3
    tg = parse_tg_value({"currency": "USD", "average_price": 662, "last_sale_price": 650, "listed_count": 41}, 1.25)
    assert tg.ton(tg.average) == pytest.approx(5.296)
    assert tg.guard_ton == pytest.approx(5.296)
    no_rate = parse_tg_value({"currency": "USD", "average_price": 662}, None)
    assert no_rate.guard_ton is None and "6.62 USD" in no_rate.line(NOW)
    assert parse_tonapi_rate({"rates": {"TON": {"prices": {"USD": 1.246}}}}) == 1.246
    assert parse_tonapi_rate({"error": 1}) is None


def test_ton_rate_failure_returns_none():
    async def boom(url):
        raise OSError("offline")

    rate = TonRate("http://x", fetch=boom)
    assert asyncio.run(rate.usd_per_ton()) is None
    assert "n/a" in rate.status()


def test_flood_wait_disables_lookups_for_its_duration():
    clock = Clock()

    async def flood(slug):
        raise FloodWaitError(request=None, capture=30)

    svc = TgValueService(flood, clock=clock)

    async def go():
        assert await svc.get("A-1") is None
        assert svc.calls == 1 and svc.disabled_until == clock() + 30
        assert await svc.get("B-2") is None
        assert svc.calls == 1  # no call while disabled
        clock.advance(31)
        await svc.get("B-2")
        assert svc.calls == 2

    asyncio.run(go())


def test_two_lookups_of_one_slug_make_one_call_and_cap_per_minute():
    clock = Clock()
    fake = FakeTgValue({f"S-{i}": {"currency": "USD", "value": 100} for i in range(20)})
    svc = TgValueService(fake, max_per_min=10, clock=clock)

    async def go():
        assert await svc.get("S-1") == {"currency": "USD", "value": 100}
        await svc.get("S-1")
        assert fake.calls == ["S-1"]
        for i in range(2, 20):
            await svc.get(f"S-{i}")
        assert len(fake.calls) == 10  # TG_VALUE_MAX_PER_MIN
        clock.advance(61)
        await svc.get("S-15")
        assert len(fake.calls) == 11

    asyncio.run(go())


def test_no_session_means_na():
    svc = TgValueService(None)
    assert asyncio.run(svc.get("A-1")) is None
    assert svc.status().startswith("n/a")


def _deal(price, floor, conf="high"):
    return assess(L("d", price), FloorEstimate(floor, "cluster", 5, conf), FeeModel(10, 0, 0), [])


def test_guards_only_downgrade_auto():
    view = PeerView([PeerQuote("portals", "collection", 4.6, 4.6, 4.27, 10)])
    hits = guards(view, _deal(4.55, 6.0))  # 5.00 all-in here
    assert hits == [("CHEAPER_ELSEWHERE", "cheaper on Portals (4.60 all-in)")]
    assert guards(view, _deal(4.3, 6.0)) == []  # 4.73 all-in: within 3%
    assert stricter_mode("auto", "confirm") == "confirm"
    assert stricter_mode("watch", "confirm") == "watch"  # never loosens watch
    assert stricter_mode("confirm", "auto") == "confirm"
    tg = parse_tg_value({"currency": "USD", "average_price": 300, "last_sale_price": 325}, 1.25)
    low = guards(PeerView(tg=tg), _deal(3.0, 4.0, conf="low"))
    assert low == [("ABOVE_TG_VALUE", "above Telegram's last sale (2.60 TON)")]  # names the one it used
    assert guards(PeerView(tg=tg), _deal(3.0, 4.0, conf="med")) == []


def test_exit_best_picks_highest_net():
    quotes = [PeerQuote("portals", "model", 6.0, 6.0, 5.6, 1), PeerQuote("mrkt", "model", 5.5, 5.5, 5.225, 1)]
    assert best_exit("tonnel", 5.2, quotes, 0.0) == ("portals", 5.6)
    assert best_exit("tonnel", 5.9, quotes, 0.0) == ("tonnel", 5.9)
    assert best_exit("tonnel", 5.2, quotes, 0.5) == ("tonnel", 5.2)  # moving out costs our withdraw fee


def test_fee_normalized_ranking_cheaper_sticker_with_higher_fee_loses(tmp_path):
    a = tmp_path / "floors_mrkt.json"
    write_snapshot(a, build_snapshot("mrkt", FeeModel(10, 0, 0), {"Spy Agaric": {"floor": 4.5}}, NOW))
    b = tmp_path / "floors_portals.json"
    write_snapshot(b, build_snapshot("portals", FeeModel(0, 5, 0.1), {"Spy Agaric": {"floor": 4.6}}, NOW))
    qa = best_quote_for(read_snapshot(str(a), "tonnel", NOW, 600), "Spy Agaric", None, "collection")
    qb = best_quote_for(read_snapshot(str(b), "tonnel", NOW, 600), "Spy Agaric", None, "collection")
    assert qa.floor < qb.floor and qa.all_in_cost > qb.all_in_cost


def test_engine_without_peers_behaves_the_same_and_makes_no_extra_requests():
    async def run(with_peers):
        engine, storage, client, notifier, clock = await demo_engine()
        if with_peers:
            engine.peers = Peers(settings(), "tonnel", tg=TgValueService(None), clock=clock)
        try:
            report = await engine.run_cycle()
            return report.summary(), client.requests, [m for m in notifier.messages]
        finally:
            await storage.close()

    plain = asyncio.run(run(False))
    empty_peers = asyncio.run(run(True))
    assert plain[0] == empty_peers[0] and plain[1] == empty_peers[1]
    assert not any("Elsewhere" in m or "Telegram:" in m for m in empty_peers[2])
    # one filterStats call + two searches (newest, cheapest) per rule group, exactly like the scaffold
    assert plain[1] == 1 + 2 * 6


def test_fixture_files_parse():
    for name in ("floors_portals.json", "floors_mrkt.json"):
        data = json.loads((FIXTURES / name).read_text())
        assert data["schema"] == 1 and data["collections"]
