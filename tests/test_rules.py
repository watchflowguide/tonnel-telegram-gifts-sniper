import json

import pytest

from tonnel_autobuy.models import Listing, strip_rarity
from tonnel_autobuy.rules import (
    BuyRule,
    RuleParseError,
    load_rules_file,
    parse_addrule_args,
)


def L(price, collection="Toy Bear", model="Wizard (1.5%)", backdrop="Black (1%)",
      symbol="Star (0.3%)", id="1"):
    return Listing(id=id, collection=collection, model=model, backdrop=backdrop, symbol=symbol,
                   number=7, price_ton=price, listed_at=None)


def test_strip_rarity():
    assert strip_rarity("Wizard (1.5%)") == "Wizard"
    assert strip_rarity("Jade Green (2%)") == "Jade Green"
    assert strip_rarity("Plain") == "Plain"
    assert strip_rarity(None) is None


def test_match_ok_with_discount_and_fee():
    r = BuyRule(collection="toy bear", max_price_ton=12, min_discount_pct=25)
    res = r.match(L(7.5), floor=14.0, fee_pct=10)
    assert res.ok
    assert res.discount_pct == pytest.approx(46.43, abs=0.01)


def test_match_price_cap_uses_cost_incl_fee():
    r = BuyRule(collection="Toy Bear", max_price_ton=10)
    assert r.match(L(9.0), floor=None, fee_pct=0).ok
    res = r.match(L(9.5), floor=None, fee_pct=10)  # cost 10.45
    assert not res.ok and "cost" in res.reason


def test_match_discount_too_small():
    r = BuyRule(collection="Toy Bear", max_price_ton=100, min_discount_pct=20)
    res = r.match(L(13.0), floor=14.0)
    assert not res.ok and "discount" in res.reason


def test_match_needs_floor_when_discount_required():
    r = BuyRule(collection="Toy Bear", max_price_ton=100, min_discount_pct=5)
    assert not r.match(L(1.0), floor=None).ok
    assert BuyRule(collection="Toy Bear", max_price_ton=100).match(L(1.0), floor=None).ok


@pytest.mark.parametrize("attr,value", [("model", "Zebra"), ("backdrop", "Amber"),
                                         ("symbol", "Pear")])
def test_match_attribute_mismatch(attr, value):
    r = BuyRule(collection="Toy Bear", max_price_ton=100, **{attr: value})
    res = r.match(L(5), floor=10)
    assert not res.ok and attr in res.reason


def test_match_attributes_ignore_rarity_and_case():
    r = BuyRule(collection="TOY BEAR", max_price_ton=100, model="wizard", backdrop="black (1%)",
                symbol="Star")
    assert r.match(L(5), floor=10).ok


def test_match_collection_mismatch_and_disabled():
    r = BuyRule(collection="Plush Pepe", max_price_ton=100)
    assert not r.match(L(5), floor=10).ok
    r2 = BuyRule(collection="Toy Bear", max_price_ton=100, enabled=False)
    assert not r2.match(L(5), floor=10).ok


def test_parse_addrule_full():
    r = parse_addrule_args("Plush Pepe max=900 model=Gucci Bag backdrop=Black symbol=Star discount=15")
    assert r.collection == "Plush Pepe"
    assert r.max_price_ton == 900
    assert r.model == "Gucci Bag"
    assert r.backdrop == "Black"
    assert r.symbol == "Star"
    assert r.min_discount_pct == 15


def test_parse_addrule_minimal_and_decimal_comma():
    r = parse_addrule_args("Toy Bear max=12,5")
    assert r.collection == "Toy Bear" and r.max_price_ton == 12.5
    assert r.model is None and r.min_discount_pct == 0


@pytest.mark.parametrize("text", ["", "Toy Bear", "max=10", "Toy Bear max=abc",
                                   "Toy Bear max=0", "Toy Bear max=5 discount=-1"])
def test_parse_addrule_errors(text):
    with pytest.raises(RuleParseError):
        parse_addrule_args(text)


def test_load_rules_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps([
        {"collection": "Toy Bear", "max_price_ton": 12, "min_discount_pct": 25},
        {"collection": "Plush Pepe", "max": 900, "model": "Gucci Bag", "discount": 10},
    ]))
    rules = load_rules_file(p)
    assert [r.collection for r in rules] == ["Toy Bear", "Plush Pepe"]
    assert rules[1].max_price_ton == 900 and rules[1].min_discount_pct == 10
    assert load_rules_file(tmp_path / "missing.json") == []
