import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest
from helpers import JB, NOW, L, demo_engine

from tonnel_autobuy.client import demo_listings, parse_listings
from tonnel_autobuy.config import Settings
from tonnel_autobuy.models import Listing
from tonnel_autobuy.pricing import (
    FeeModel,
    FloorEstimate,
    assess,
    classify,
    cluster_floor,
    derive_sell_fee,
    estimate_floor,
    fee_model_from_settings,
    too_good_flag,
)
from tonnel_autobuy.rules import BuyRule, RuleParseError, match, parse_addrule_args
from tonnel_autobuy.storage import Storage

TONNEL = FeeModel(10, 0, 0)


def pool(*prices, model="Albino"):
    return [L(f"p{i}", p, model=model) for i, p in enumerate(prices)]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# --- robust floor -----------------------------------------------------------------------


def test_cluster_floor_ignores_one_troll_ask():
    # F1: 1.00 (the candidate), 3.00 troll, cluster at 1.60/1.62/1.65
    candidate = L("c", 1.00)
    est = estimate_floor(candidate, [candidate, *pool(3.00, 1.60, 1.62, 1.65)], None, [], NOW, min_comparables=3)
    assert est.value == pytest.approx(1.60)
    assert est.source == "cluster" and est.n == 4 and est.confidence == "high"


def test_lone_cheap_ask_is_not_the_floor():
    candidate = L("c", 1.40)
    est = estimate_floor(candidate, pool(0.50, 2.00, 2.05, 2.10), None, [], NOW)
    assert est.value == pytest.approx(2.00)
    assert cluster_floor([0.5, 2.0, 2.05, 2.1]) == 2.0


def test_thin_book_gives_low_confidence():
    candidate = L("c", 1.0)
    est = estimate_floor(candidate, pool(1.5), 1.4, [], NOW)
    assert est.source == "thin" and est.value == 1.5 and est.confidence == "low"
    assert estimate_floor(candidate, [], 1.4, [], NOW).source == "api"
    assert estimate_floor(candidate, [], None, [], NOW).value is None
    two = estimate_floor(candidate, pool(1.5, 3.0), None, [], NOW)
    assert two.source == "thin" and two.value == pytest.approx(2.25) and two.confidence == "low"


def test_two_decoy_listings_never_make_a_med_floor_that_passes_the_default_rule():
    """Regression: a floor not backed by a cluster of at least 3 listings is `low`. Two decoy asks (thin, or a
    2-listing 'cluster' with CLUSTER_K=1) used to give `med`, which passes the default min_confidence=med."""
    candidate = L("c", 1.0)
    rule = BuyRule("Lunar Snake", 5, model="Albino", min_discount_pct=10)
    assert rule.min_confidence == "med"
    for k in (1, 2):
        est = estimate_floor(candidate, pool(2.0, 2.05), None, [], NOW, k=k, min_comparables=2)
        assert est.confidence == "low", (k, est)
        deal = assess(candidate, est, TONNEL, classify(candidate, "tonnel", NOW))
        result = match(rule, candidate, deal)
        assert not result.ok and result.code == "LOW_CONFIDENCE", (k, result)
    # Two decoys far below a real cluster cannot borrow its confidence either: the floor is the cluster.
    est = estimate_floor(candidate, pool(1.2, 1.25, 3.0, 3.05, 3.1), None, [], NOW, k=1, min_comparables=3)
    assert est.value == pytest.approx(1.2) and est.confidence == "low"
    # Three listings in the band are enough for `med`, and a deep book for `high`.
    assert estimate_floor(candidate, pool(2.0, 2.05, 2.1), None, [], NOW).confidence == "med"
    assert estimate_floor(candidate, pool(2.0, 2.05, 2.1, 2.2, 2.3), None, [], NOW).confidence == "high"


def test_snapshot_jump_downgrades_confidence():
    candidate = L("c", 1.0)
    book = pool(1.60, 1.62, 1.65, 1.66, 1.70)
    calm = estimate_floor(candidate, book, None, [(NOW - 1800, 1.58), (NOW, 1.60)], NOW)
    assert calm.confidence == "high" and calm.note == ""
    jumpy = estimate_floor(candidate, book, None, [(NOW - 1800, 2.40), (NOW - 60, 1.60)], NOW)
    assert jumpy.confidence == "med"
    assert jumpy.note == "floor -33% in 1h"
    old = estimate_floor(candidate, book, None, [(NOW - 7200, 2.40), (NOW, 1.60)], NOW)
    assert old.confidence == "high"


# --- fees ------------------------------------------------------------------------------------


def test_fee_model_tonnel_buy_fee():
    assert TONNEL.cost(0.97) == pytest.approx(1.067)
    assert TONNEL.net_exit(1.0) == pytest.approx(1.0)
    defaults = fee_model_from_settings(Settings(_env_file=None))
    assert (defaults.buy_pct, defaults.sell_pct, defaults.withdraw_ton) == (10, 0, 0)


def test_fee_pct_is_a_deprecated_alias_of_buy_fee_pct():
    assert Settings(_env_file=None, FEE_PCT=7).BUY_FEE_PCT == 7  # old .env keeps working
    both = Settings(_env_file=None, FEE_PCT=7, BUY_FEE_PCT=3)
    assert both.BUY_FEE_PCT == 3 and both.FEE_PCT == 3  # BUY_FEE_PCT wins and FEE_PCT mirrors it


def test_fee_model_portals_withdraw():
    portals = FeeModel(buy_pct=0, sell_pct=5, withdraw_ton=0.1)
    assert portals.cost(2.0) == pytest.approx(2.0)
    assert portals.net_exit(2.0) == pytest.approx(1.8)


def test_mrkt_sell_fee_derived_from_price_without_fee():
    # MRKT shows a price with and without the seller fee; the gap is the fee (pure math, same in all repos)
    assert derive_sell_fee(2.0, 1.9) == pytest.approx(5.0)
    assert derive_sell_fee(1.0, None) is None
    assert derive_sell_fee(1.0, 2.0) is None


@pytest.mark.parametrize("fees", [FeeModel(10, 0, 0), FeeModel(0, 5, 0.1), FeeModel(0, 5, 0), FeeModel(3, 8, 0.05)])
def test_break_even_formula(fees):
    floor = 2.0
    be = fees.break_even(floor)
    assert be == pytest.approx((floor * (1 - fees.sell_pct / 100) - fees.withdraw_ton) / (1 + fees.buy_pct / 100))
    assert fees.cost(be) == pytest.approx(fees.net_exit(floor))  # paying the break-even price nets zero


def test_assess_deal_math_with_the_tonnel_fee():
    deal = assess(L("x", 1.0), FloorEstimate(1.6, "cluster", 4, "high"), TONNEL, [])
    assert deal.cost == pytest.approx(1.1) and deal.net_exit == pytest.approx(1.6)
    assert deal.profit_ton == pytest.approx(0.5) and deal.profit_pct == pytest.approx(45.45, abs=0.01)
    assert deal.break_even_price == pytest.approx(1.4545, abs=1e-4) and deal.discount_pct == pytest.approx(37.5)


# --- traps and locks ----------------------------------------------------------------------------


def test_classify_every_fixture_raw():
    initial, later = demo_listings(NOW)
    listings = parse_listings(initial + later)
    assert len(listings) == len(initial + later)
    flags = {x.id: [f.code for f in classify(x, "tonnel", NOW)] for x in listings}
    assert flags[JB(53)] == ["LOCKED"]
    assert flags[JB(54)] == ["TRAP_ASSET"]
    locked = classify(next(x for x in listings if x.id == JB(53)), "tonnel", NOW)[0]
    assert locked.days == 6 and not locked.blocking and "6 d" in locked.text
    assert all(codes == [] for lid, codes in flags.items() if lid not in (JB(53), JB(54)))


def test_classify_fails_closed():
    assert [f.code for f in classify(L("z", 0), "tonnel", NOW)] == ["BAD_PRICE"]
    missing = classify(Listing("m", "Lunar Snake", None, None, None, 1, 1.0, None, raw={}), "tonnel", NOW)
    assert [(f.code, f.blocking) for f in missing] == [("BAD_PRICE", True)] and "unit unknown" in missing[0].text
    usdt = classify(L("u", 1.0, asset="USDT"), "tonnel", NOW)
    assert [(f.code, f.blocking) for f in usdt] == [("TRAP_ASSET", True)] and "USDT" in usdt[0].text
    assert classify(L("u", 1.0, asset="USDT"), "tonnel", NOW, asset="USDT") == []  # TONNEL_ASSET decides
    assert [f.code for f in classify(L("-5", 1.0, gift_id=-5), "tonnel", NOW)] == ["TRAP_BUNDLE"]
    assert [f.code for f in classify(L("a", 1.0, auction={"id": 1}), "tonnel", NOW)] == ["TRAP_AUCTION"]
    future = L("f", 1.0, export_at=iso(NOW + 2.5 * 86400))
    assert classify(future, "tonnel", NOW)[0].days == 3
    assert classify(future, "tonnel", NOW, export_locks=False) == []  # EXPORT_AT_LOCKS=false
    assert classify(L("p", 1.0, export_at=iso(NOW - 60)), "tonnel", NOW) == []  # already past


def test_too_good_flag():
    assert too_good_flag(0.16, 1.6).text == "price is 10% of floor"
    assert too_good_flag(1.0, 1.6) is None and too_good_flag(1.0, None) is None


# --- match() order and reason codes -------------------------------------------------------------


def deal_for(listing, floor=1.6, conf="high", n=5, fees=TONNEL, flags=()):
    flags = list(flags) or classify(listing, "tonnel", NOW)
    tg = too_good_flag(listing.price_ton, floor)
    if tg:
        flags.append(tg)
    return assess(listing, FloorEstimate(floor, "cluster", n, conf), fees, flags)


LOCK = iso(NOW + 6 * 86400 - 3600)


@pytest.mark.parametrize(
    "rule,listing,kw,code",
    [
        (BuyRule("Lunar Snake", 5, model="Cobra"), L("a", 1.0), {}, "MISMATCH"),
        (BuyRule("Lunar Snake", 0.5), L("b", 1.0, asset="USDT"), {}, "TRAP_ASSET"),  # trap before max
        (BuyRule("Lunar Snake", 0.5), L("c", 1.0, export_at=LOCK), {}, "LOCKED"),  # lock before max
        (BuyRule("Lunar Snake", 1.05), L("d", 1.0), {"conf": "low"}, "OVER_MAX"),  # max (on cost) before confidence
        (BuyRule("Lunar Snake", 5, min_discount_pct=50), L("e", 1.0), {"conf": "low"}, "LOW_CONFIDENCE"),
        (BuyRule("Lunar Snake", 5, min_discount_pct=50), L("f", 1.0), {}, "LOW_DISCOUNT"),
        (BuyRule("Lunar Snake", 5), L("g", 0.97), {"floor": 1.0}, "LOSES_AFTER_FEES"),
        (BuyRule("Lunar Snake", 5, min_profit_ton=1), L("h", 1.0), {}, "LOW_PROFIT"),
        (BuyRule("Lunar Snake", 5, min_profit_pct=80), L("i", 1.0), {}, "LOW_PROFIT"),
    ],
)
def test_match_reason_codes_and_order(rule, listing, kw, code):
    res = match(rule, listing, deal_for(listing, **kw))
    assert not res.ok and res.code == code


def test_f2_loses_after_fees_on_tonnel():
    listing = L("f2", 0.97)
    deal = deal_for(listing, floor=1.0)
    assert deal.cost == pytest.approx(1.067)
    res = match(BuyRule("Lunar Snake", 5), listing, deal)
    assert res.code == "LOSES_AFTER_FEES" and res.reason == "loses 0.07 TON after fees"


def test_max_price_compares_cost_incl_buy_fee():
    listing = L("m", 0.95)
    res = match(BuyRule("Lunar Snake", 1.0), listing, deal_for(listing, floor=2.0))
    assert res.code == "OVER_MAX" and "cost 1.04" in res.reason
    # the scaffold-era method signature still works and still uses the fee
    old = BuyRule("Lunar Snake", 1.0).match(listing, 2.0, fee_pct=10)
    assert old.code == "OVER_MAX" and not old.ok


def test_locked_allowed_by_rule_and_too_good_passes_match():
    listing = L("lk", 1.0, export_at=iso(NOW + 3 * 86400 - 60))
    assert match(BuyRule("Lunar Snake", 5, allow_locked_days=5), listing, deal_for(listing)).ok
    assert match(BuyRule("Lunar Snake", 5, allow_locked_days=2), listing, deal_for(listing)).code == "LOCKED"
    cheap = L("tg", 0.16)
    assert match(BuyRule("Lunar Snake", 5), cheap, deal_for(cheap)).ok  # TOO_GOOD is not blocking


# --- /addrule ------------------------------------------------------------------------------------


def test_addrule_parses_new_keys():
    rule = parse_addrule_args("Lunar Snake max=3 profit=0.3 profit%=10 conf=high locked=7 mode=confirm exit=best")
    assert (rule.min_profit_ton, rule.min_profit_pct, rule.min_confidence, rule.allow_locked_days) == (0.3, 10, "high", 7)
    assert (rule.mode, rule.exit, rule.collection) == ("confirm", "best", "Lunar Snake")
    default = parse_addrule_args("Lunar Snake max=3")
    assert (default.min_confidence, default.mode, default.exit, default.allow_locked_days) == ("med", "auto", "here", 0)
    apostrophe = parse_addrule_args("Durov's Cap max=900 model=Gucci Bag profit=1")
    assert (apostrophe.collection, apostrophe.model, apostrophe.min_profit_ton) == ("Durov's Cap", "Gucci Bag", 1)


@pytest.mark.parametrize("text", ["X max=1 conf=great", "X max=1 mode=yolo", "X max=1 profit=-1", "X max=1 locked=abc",
                                  "X max=1 exit=moon", "X max=1 discount=100"])
def test_addrule_rejects_bad_new_keys(text):
    with pytest.raises(RuleParseError):
        parse_addrule_args(text)


# --- the engine uses net math end to end ----------------------------------------------------------


def test_engine_cycle_makes_no_extra_requests_and_logs_reasons():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            report = await engine.run_cycle()
            assert client.requests == 1 + 2 * 6  # filterStats + newest/cheapest per rule group, like the scaffold
            assert report.reasons["LOSES_AFTER_FEES"] == 1 and report.reasons["LOCKED"] == 1
            assert report.reasons["TRAP_ASSET"] == 1
        finally:
            await storage.close()

    asyncio.run(go())


# --- migration ------------------------------------------------------------------------------------

SCAFFOLD_SCHEMA = """
CREATE TABLE seen (listing_id TEXT NOT NULL, price REAL NOT NULL, ts REAL NOT NULL, PRIMARY KEY (listing_id, price));
CREATE TABLE purchases (id INTEGER PRIMARY KEY AUTOINCREMENT, listing_id TEXT NOT NULL, collection TEXT NOT NULL,
    model TEXT, backdrop TEXT, symbol TEXT, number INTEGER, price_ton REAL NOT NULL, cost_ton REAL NOT NULL,
    floor_ton REAL, rule_id INTEGER, dry INTEGER NOT NULL, status TEXT NOT NULL, message TEXT, ts REAL NOT NULL);
CREATE INDEX purchases_ts ON purchases (ts);
CREATE TABLE rules (id INTEGER PRIMARY KEY AUTOINCREMENT, collection TEXT NOT NULL, model TEXT, backdrop TEXT,
    symbol TEXT, max_price_ton REAL NOT NULL, min_discount_pct REAL NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, created_ts REAL NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def make_scaffold_db(path, purchases=1):
    con = sqlite3.connect(path)
    con.executescript(SCAFFOLD_SCHEMA)
    con.execute("INSERT INTO rules(collection, max_price_ton, min_discount_pct, created_ts) VALUES ('Toy Bear', 12, 25, 1)")
    for i in range(purchases):
        con.execute(
            "INSERT INTO purchases(listing_id, collection, model, number, price_ton, cost_ton, dry, status, ts)"
            " VALUES (?, 'Toy Bear', 'Wizard (1.5%)', 7, 7.5, 8.25, 1, 'dry', ?)", (f"g{i}", NOW - 3600))
    con.execute("INSERT INTO settings(key, value) VALUES ('paused', '0')")
    con.commit()
    con.close()


def test_migration_from_scaffold_schema(tmp_path):
    path = tmp_path / "old.sqlite3"
    make_scaffold_db(path)

    async def go():
        storage = await Storage(str(path)).open()
        try:
            rules = await storage.list_rules()
            assert len(rules) == 1 and rules[0].max_price_ton == 12 and rules[0].min_discount_pct == 25
            assert (rules[0].mode, rules[0].min_confidence, rules[0].state, rules[0].source) == ("auto", "med", "live", "bot")
            positions = await storage.positions()
            assert positions[0].cost_ton == pytest.approx(8.25) and positions[0].model == "Wizard (1.5%)"
            assert (await storage.recent_purchases())[0]["listing_id"] == "g0"
            for table in ("floor_snapshots", "observations", "decisions", "pending", "mutes", "marks"):
                async with storage.db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                    assert (await cur.fetchone())[0] == 0
            cols = await storage._columns("rules")
            assert {"min_profit_ton", "min_profit_pct", "min_confidence", "allow_locked_days", "mode", "exit"} <= cols
            assert {"closed_at", "exit_price_ton", "exit_note"} <= await storage._columns("purchases")
        finally:
            await storage.close()
        again = await Storage(str(path)).open()  # second open is a no-op migration
        try:
            assert len(await again.list_rules()) == 1
        finally:
            await again.close()

    asyncio.run(go())


def test_scaffold_file_rules_are_adopted_not_duplicated(tmp_path):
    path = tmp_path / "old.sqlite3"
    make_scaffold_db(path)  # this rule came from rules.json via the scaffold's sync_rules

    async def go():
        storage = await Storage(str(path)).open()
        try:
            await storage.replace_rules_from_source("file", [BuyRule("Toy Bear", 12, min_discount_pct=25, mode="confirm")])
            [rule] = await storage.list_rules()
            assert rule.id == 1 and rule.source == "file" and rule.mode == "confirm"
        finally:
            await storage.close()

    asyncio.run(go())
