import base64
import json

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from tonnel_autobuy.client import (
    WTF_PASSPHRASE,
    AuthError,
    SearchFilters,
    TonnelError,
    _evp_bytes_to_key,
    build_buy_payload,
    build_page_payload,
    check_api_error,
    generate_wtf,
    parse_filter_stats,
    parse_listings,
    tonnel_title,
)
from tonnel_autobuy.models import Listing

# Real item shape from a live pageGifts call (2026-10-28).
LIVE_ITEM = {
    "gift_num": 10327, "customEmojiId": "5291976600089289958", "gift_id": 10516834,
    "name": "Toy Bear", "model": "Tricolor (1.3%)", "asset": "TON", "symbol": "Car (0.2%)",
    "backdrop": "Pistachio (2%)", "availabilityIssued": 0, "availabilityTotal": 0,
    "backdropData": {}, "message_in_channel": 0, "can_transfer_since": "2025-03-07T15:26:02.000Z",
    "price": 35.3, "dutchAuctionData": None, "status": "forsale", "limited": False,
    "crafted": False, "underLoan": False, "auction": None,
    "export_at": "2026-11-06T00:23:19.000Z", "premarketData": {},
}


def test_tonnel_title():
    assert tonnel_title("plush pepe") == "Plush Pepe"
    assert tonnel_title("durov's cap") == "Durov's Cap"
    assert tonnel_title("jack-in-the-box") == "Jack-in-the-Box"


def test_build_page_payload_latest_with_attrs():
    f = SearchFilters(collection="toy bear", model="wizard", backdrop="Black (1%)", sort="latest",
                      limit=50)
    p = build_page_payload(f, user_auth="", asset="TON")
    assert p["sort"] == '{"message_post_time":-1,"gift_id":-1}'
    assert p["limit"] == 30  # API max
    assert p["page"] == 1 and p["ref"] == 0 and p["price_range"] == 0 and p["user_auth"] == ""
    flt = json.loads(p["filter"])
    assert flt["gift_name"] == "Toy Bear"
    assert flt["price"] == {"$exists": True}
    assert flt["buyer"] == {"$exists": False}
    assert flt["asset"] == "TON"
    assert flt["model"] == {"$regex": "^Wizard \\("}
    assert flt["backdrop"] == "Black (1%)"  # with rarity -> exact match
    assert "symbol" not in flt


def test_build_page_payload_price_asc_and_bad_sort():
    p = build_page_payload(SearchFilters(collection="Toy Bear", sort="price_asc"))
    assert p["sort"] == '{"price":1,"gift_id":-1}'
    with pytest.raises(ValueError):
        build_page_payload(SearchFilters(collection="Toy Bear", sort="nope"))


def test_parse_listings_live_shape_and_skips():
    bundle = dict(LIVE_ITEM, gift_id=-5)
    auction = dict(LIVE_ITEM, gift_id=2, auction={"id": "x"})
    noprice = dict(LIVE_ITEM, gift_id=3, price=None)
    out = parse_listings([LIVE_ITEM, bundle, auction, noprice])
    assert len(out) == 1
    x = out[0]
    assert isinstance(x, Listing)
    assert x.id == "10516834" and x.number == 10327 and x.collection == "Toy Bear"
    assert x.price_ton == 35.3 and x.model == "Tricolor (1.3%)"
    assert x.cost_ton(10) == pytest.approx(38.83)
    assert x.nft_url == "https://t.me/nft/ToyBear-10327"
    with pytest.raises(TonnelError):
        parse_listings({"status": "error"})


def test_parse_filter_stats():
    data = {"status": "success", "data": {
        "Toy Bear_Wizard (1.5%)": {"floorPrice": 19, "howMany": 36},
        "Toy Bear_Zebra (1.5%)": {"floorPrice": 14.84, "howMany": 10},
        "Plush Pepe_Gucci Bag (1%)": {"floorprice": 900, "howMany": 2},
    }}
    t = parse_filter_stats(data)
    assert t.get("toy bear") == 14.84
    assert t.get("Toy Bear", "wizard") == 19
    assert t.get("Toy Bear", "unknown") == 14.84  # falls back to collection floor
    assert t.get("Plush Pepe", "Gucci Bag (1%)") == 900
    assert t.counts["toy bear"] == 46
    with pytest.raises(TonnelError):
        parse_filter_stats({"status": "error", "message": "x"})


def test_generate_wtf_is_cryptojs_compatible():
    salt = b"12345678"
    ts, wtf = generate_wtf(timestamp=1700000000, salt=salt)
    assert ts == "1700000000"
    raw = base64.b64decode(wtf)
    assert raw[:8] == b"Salted__" and raw[8:16] == salt
    key, iv = _evp_bytes_to_key(WTF_PASSPHRASE, salt)
    plain = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(raw[16:]), 16)
    assert plain == b"1700000000"


def test_build_buy_payload_uses_raw_price():
    x = Listing.from_api(LIVE_ITEM)
    p = build_buy_payload(x, "AUTH", timestamp=1700000000)
    assert p["price"] == 35.3 and p["asset"] == "TON" and p["authData"] == "AUTH"
    assert p["timestamp"] == "1700000000" and p["wtf"]


def test_check_api_error():
    check_api_error([{"ok": 1}])
    check_api_error({"status": "success"})
    with pytest.raises(AuthError):
        check_api_error({"status": "error", "message": "Invalid auth data"})
    with pytest.raises(TonnelError):
        check_api_error({"status": "error", "message": "Gift already sold"})
