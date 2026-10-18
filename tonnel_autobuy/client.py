"""Tonnel marketplace API client (+ offline MockClient).

All Tonnel-specific endpoint/payload knowledge lives in this module.
Reference used: community lib `tonnelmp` 1.2 (read only, not a dependency) and a
live smoke test of `pageGifts` on 2026-10-28.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .config import Settings, mask
from .models import BuyResult, FloorTable, Listing, norm
from .rules import BuyRule
from .tg_auth import InitDataProvider

log = logging.getLogger(__name__)

ORIGIN = "https://market.tonnel.network"
LISTINGS_URL = "https://gifts2.tonnel.network/api/pageGifts"
FLOORS_URL = "https://gifts2.tonnel.network/api/filterStats"
BALANCE_URL = "https://gifts2.tonnel.network/api/balance/info"
BUY_URL = "https://gifts.coffin.meme/api/buyGift/{gift_id}"

# Sort values are JSON *strings* inside the JSON body.
SORTS = {
    "latest": '{"message_post_time":-1,"gift_id":-1}',
    "price_asc": '{"price":1,"gift_id":-1}',
    "price_desc": '{"price":-1,"gift_id":-1}',
}

# TODO(verify): passphrase of the CryptoJS-AES "wtf" field (encrypted unix timestamp)
# required by write endpoints on gifts.coffin.meme. Taken from tonnelmp 1.2; it is a
# front-end constant and may be rotated.
WTF_PASSPHRASE = b"yowtfisthispieceofshitiiit"

_EXACT_NAMES = {"jack-in-the-box": "Jack-in-the-Box"}


# ---------------------------------------------------------------- errors


class TonnelError(Exception):
    """Generic API/network failure (engine backs off and retries)."""


class RateLimited(TonnelError):
    """HTTP 429 or a Cloudflare 403 page."""


class AuthError(TonnelError):
    """initData rejected ("Invalid auth data", 401)."""


class ApiRefused(TonnelError):
    """Tonnel answered and refused the request: an explicit {"status":"error"} body or an HTTP 4xx (other than
    408). A buy that ends this way did not execute. Every other TonnelError on a buy (network error, timeout,
    5xx, unreadable body) leaves the outcome unknown."""


# ---------------------------------------------------------------- pure helpers


def tonnel_title(text: str) -> str:
    """Capitalize the first letter of each word, like the Tonnel front-end does."""
    text = text.strip()
    if text.casefold() in _EXACT_NAMES:
        return _EXACT_NAMES[text.casefold()]
    for word in re.findall(r"\w+(?:'\w+)?", text):
        text = text.replace(word, word[0].upper() + word[1:], 1)
    return text


def _attr_filter(value: str) -> Any:
    # Values with rarity "(1.5%)" are matched exactly, otherwise by name prefix.
    if "(" in value:
        return tonnel_title(value)
    return {"$regex": f"^{re.escape(tonnel_title(value))} \\("}


@dataclass(frozen=True, slots=True)
class SearchFilters:
    collection: str
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    sort: str = "latest"  # "latest" | "price_asc" | "price_desc"
    limit: int = 30
    page: int = 1


def build_filter(f: SearchFilters, asset: str = "TON") -> dict:
    flt: dict[str, Any] = {
        "price": {"$exists": True},
        "buyer": {"$exists": False},
        "asset": asset,
        "refunded": {"$ne": True},
        "export_at": {"$exists": True},  # regular (non-premarket, non-bundle) listings
        "gift_name": tonnel_title(f.collection),
    }
    for attr in ("model", "backdrop", "symbol"):
        value = getattr(f, attr)
        if value:
            flt[attr] = _attr_filter(value)
    return flt


def build_page_payload(f: SearchFilters, user_auth: str = "", asset: str = "TON") -> dict:
    if f.sort not in SORTS:
        raise ValueError(f"unknown sort '{f.sort}', expected one of {list(SORTS)}")
    return {
        "page": f.page,
        "limit": max(1, min(int(f.limit), 30)),
        "sort": SORTS[f.sort],
        "filter": json.dumps(build_filter(f, asset), separators=(",", ":")),
        "ref": 0,
        "price_range": 0,  # verified live: 0 = no price filter (or [min, max])
        "user_auth": user_auth,
    }


def build_single_payload(gift_id: str, user_auth: str = "") -> dict:
    """pageGifts filter for one listing, used to re-check a deal card before a tapped buy.

    Verified live 2026-10-28 (public, no auth): {gift_id, price exists, no buyer} returns the one listing;
    adding status='forsale' (as first planned) returns nothing, so "still for sale" is price + no buyer,
    plus the item's own status field when present.
    """
    flt = {"gift_id": int(gift_id), "price": {"$exists": True}, "buyer": {"$exists": False}}
    return {
        "page": 1,
        "limit": 1,
        "sort": SORTS["latest"],
        "filter": json.dumps(flt, separators=(",", ":")),
        "ref": 0,
        "price_range": 0,
        "user_auth": user_auth,
    }


def find_listing(listings: list[Listing], listing_id: str) -> Listing | None:
    return next((x for x in listings if x.id == str(listing_id)), None)


def parse_listings(data: Any) -> list[Listing]:
    if not isinstance(data, list):
        raise TonnelError(f"unexpected pageGifts response: {str(data)[:200]}")
    out: list[Listing] = []
    for item in data:
        try:
            # Skip bundles (negative id), auctions and anything without a fixed price.
            if int(item.get("gift_id", 0)) < 0 or item.get("auction") or item.get("price") is None:
                continue
            out.append(Listing.from_api(item))
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("skip malformed listing %s: %s", str(item)[:120], exc)
    return out


def parse_filter_stats(data: Any) -> FloorTable:
    """filterStats -> FloorTable.

    TODO(verify): format per tonnelmp docs:
    {"status":"success","data":{"Toy Bear_Wizard (1.5%)":{"floorPrice":14.8,"howMany":36}}}
    floorPrice is the raw listed price (without fee).
    """
    if not isinstance(data, dict) or data.get("status") not in (None, "success"):
        raise TonnelError(f"filterStats error: {str(data)[:200]}")
    table = FloorTable()
    for key, value in (data.get("data") or {}).items():
        if not isinstance(value, dict):
            continue
        collection, _, model = str(key).partition("_")
        floor = value.get("floorPrice", value.get("floorprice"))
        try:
            floor = float(floor) if floor is not None else None
        except (TypeError, ValueError):
            floor = None
        table.add(collection.strip(), model.strip() or None, floor, value.get("howMany", 0))
    return table


def _evp_bytes_to_key(password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16):
    """OpenSSL EVP_BytesToKey with MD5 (what CryptoJS uses for passphrase AES)."""
    derived, prev = b"", b""
    while len(derived) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        derived += prev
    return derived[:key_len], derived[key_len : key_len + iv_len]


def generate_wtf(timestamp: int | None = None, salt: bytes | None = None) -> tuple[str, str]:
    """Return (timestamp, wtf) where wtf = CryptoJS.AES.encrypt(timestamp, PASSPHRASE)."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    ts = str(int(time.time()) if timestamp is None else int(timestamp))
    salt = salt if salt is not None else os.urandom(8)
    key, iv = _evp_bytes_to_key(WTF_PASSPHRASE, salt)
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(ts.encode(), AES.block_size))
    return ts, base64.b64encode(b"Salted__" + salt + ct).decode()


def build_buy_payload(listing: Listing, auth: str, asset: str = "TON",
                      timestamp: int | None = None) -> dict:
    ts, wtf = generate_wtf(timestamp)
    # TODO(verify): price is the RAW listed price (without the 10% fee) per tonnelmp docs;
    # the server adds the fee itself. Confirm in the web app's network tab.
    return {"authData": auth, "asset": asset, "price": float(listing.price_ton),
            "timestamp": ts, "wtf": wtf}


def check_api_error(data: Any) -> None:
    """Tonnel answers HTTP 200 with {"status":"error","message":...} on failures."""
    if isinstance(data, dict) and data.get("status") == "error":
        msg = str(data.get("message") or "unknown error")
        if "auth" in msg.lower():
            raise AuthError(msg)
        raise ApiRefused(msg)


def classify_buy_response(data: Any) -> BuyResult:
    """Classify a 2xx buy response. Fails closed for the limits: only {"status":"success"} is a buy, and an
    explicit {"status":"error"} never gets here (check_api_error raised it as a definite failure). Any other
    2xx body (another status, a dict without status, a list, a scalar) may be a purchase in a shape we do
    not know, so it is 'uncertain': it is recorded as 'unknown' and counts toward the budget and hourly cap.
    TODO(verify): the success shape; tonnelmp only says "status: success or error"."""
    if isinstance(data, dict) and data.get("status") == "success":
        return BuyResult(True, str(data.get("message", "")), raw=data)
    shown = json.dumps(data, default=str)[:200]
    return BuyResult(False, f"unrecognized buy response {shown} (result unknown: counted toward your limits, "
                            "check Tonnel)", raw=data if isinstance(data, dict) else {"body": data}, uncertain=True)


# ---------------------------------------------------------------- interface


class MarketClient(Protocol):
    has_auth: bool
    name: str

    async def search_listings(self, filters: SearchFilters) -> list[Listing]: ...
    async def collection_floors(self) -> FloorTable: ...
    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None: ...
    async def buy(self, listing: Listing) -> BuyResult: ...
    async def balance(self) -> float: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------- real client


class TonnelClient:
    name = "tonnel"

    def __init__(self, settings: Settings, auth: InitDataProvider | None = None):
        from curl_cffi.requests import AsyncSession  # lazy: mock never needs it

        self._s = settings
        self._auth = auth or InitDataProvider(settings)
        kwargs: dict[str, Any] = {"impersonate": "chrome", "timeout": settings.HTTP_TIMEOUT}
        if settings.PROXY:
            kwargs["proxy"] = settings.PROXY
        self._http = AsyncSession(**kwargs)
        self._headers = {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
        }
        # Clock-skew and latency info for /doctor (Date header of the last response).
        self.last_server_date: float | None = None
        self.last_response_at: float | None = None
        self.last_latency_ms: float | None = None

    @property
    def has_auth(self) -> bool:
        return self._auth.configured

    async def close(self) -> None:
        await self._http.close()

    async def _post(self, url: str, payload: dict) -> Any:
        started = time.perf_counter()
        try:
            r = await self._http.post(url, json=payload, headers=self._headers)
        except Exception as exc:  # curl_cffi RequestsError, timeouts, DNS...
            raise TonnelError(f"network error on {url}: {exc}") from exc
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        self.last_response_at = time.time()
        try:
            date = r.headers.get("date")
            self.last_server_date = parsedate_to_datetime(date).timestamp() if date else None
        except (TypeError, ValueError):
            self.last_server_date = None
        if r.status_code == 429:
            raise RateLimited(f"429 on {url}")
        if r.status_code == 401:
            raise AuthError(f"401 on {url}")
        if r.status_code == 403:
            if "json" in (r.headers.get("content-type") or ""):
                raise AuthError(f"403 on {url}: {r.text[:200]}")
            raise RateLimited(f"403 on {url} (likely Cloudflare)")
        if 400 <= r.status_code < 500 and r.status_code != 408:
            raise ApiRefused(f"HTTP {r.status_code} on {url}: {r.text[:200]}")
        if r.status_code >= 400:
            raise TonnelError(f"HTTP {r.status_code} on {url}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as exc:
            raise TonnelError(f"non-JSON response from {url}: {r.text[:200]}") from exc
        check_api_error(data)
        return data

    async def _post_auth(self, url: str, build) -> Any:
        """POST with initData; on AuthError refresh initData once and retry."""
        for attempt in (1, 2):
            auth = await self._auth.get(force=attempt == 2)
            try:
                return await self._post(url, build(auth))
            except AuthError:
                self._auth.invalidate()
                if attempt == 2 or self._auth.static:
                    raise
                log.warning("initData rejected, refreshing")
        raise AssertionError("unreachable")

    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        # Verified live: pageGifts works with user_auth="" (no Telegram account needed).
        # TODO(verify): whether passing real initData changes rate limits.
        payload = build_page_payload(filters, "", self._s.TONNEL_ASSET)
        return parse_listings(await self._post(LISTINGS_URL, payload))

    async def collection_floors(self) -> FloorTable:
        data = await self._post_auth(FLOORS_URL, lambda auth: {"authData": auth})
        return parse_filter_stats(data)

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        """Re-fetch one listing (pageGifts filtered by gift_id); None when it is no longer for sale.
        Network errors raise TonnelError: the caller must not buy blind."""
        data = await self._post(LISTINGS_URL, build_single_payload(listing_id))
        hit = find_listing(parse_listings(data), listing_id)
        if hit is not None and (hit.raw or {}).get("status") not in (None, "forsale"):
            return None
        return hit

    async def balance(self) -> float:
        # TODO(verify): response {"status":"success","balance":<TON>,"usdtBalance":...}
        data = await self._post_auth(BALANCE_URL, lambda auth: {"authData": auth})
        return float(data.get("balance") or 0.0)

    async def buy(self, listing: Listing) -> BuyResult:
        url = BUY_URL.format(gift_id=listing.id)
        asset = self._s.TONNEL_ASSET
        try:
            data = await self._post_auth(url, lambda auth: build_buy_payload(listing, auth, asset))
        except AuthError as exc:
            return BuyResult(False, f"auth error: {exc}")
        except RateLimited as exc:
            return BuyResult(False, f"rate limited: {exc}")
        except ApiRefused as exc:  # an explicit refusal: nothing was bought
            return BuyResult(False, str(exc))
        except TonnelError as exc:
            # Fail closed: a network error, a timeout, a 5xx, an unreadable body or anything else that is not
            # an explicit refusal may have bought the gift. Recorded 'unknown', it counts toward the limits.
            return BuyResult(False, f"{exc} (result unknown: counted toward your limits, check Tonnel)",
                             uncertain=True)
        log.info("buy %s -> %s", listing.id, mask(json.dumps(data), 200))
        return classify_buy_response(data)


# ---------------------------------------------------------------- mock client


def _fx(gift_id, num, name, model, backdrop, symbol, price, minutes_ago, **extra):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    raw = {
        "gift_num": num, "gift_id": gift_id, "name": name, "model": model,
        "backdrop": backdrop, "symbol": symbol, "asset": "TON", "price": price,
        "status": "forsale", "auction": None, "limited": False,
        "export_at": "2026-01-01T00:00:00.000Z",
        "message_post_time": t.isoformat().replace("+00:00", "Z"),
    }
    raw.update(extra)
    return raw


def mock_fixtures() -> list[dict]:
    return [
        # Far below floor (next cheapest is 14.0) -> alert + DRY buy with the demo rules.
        _fx(900001, 1001, "Toy Bear", "Wizard (1.5%)", "Black (1%)", "Star (0.3%)", 7.5, 1),
        _fx(900002, 1002, "Toy Bear", "Zebra (1.5%)", "Amber (1%)", "Pear (0.5%)", 14.0, 30),
        _fx(900003, 1003, "Toy Bear", "Kumamon (1%)", "Jade Green (2%)", "Car (0.2%)", 14.6, 45),
        _fx(900004, 1004, "Toy Bear", "Tricolor (1.3%)", "Pistachio (2%)", "Love Bulb (0.5%)", 15.2, 90),
        # Rejected by the Plush Pepe demo rule (cost above max, discount too small).
        _fx(900101, 501, "Plush Pepe", "Cozy Galaxy (0.5%)", "Onyx Black (1%)", "Illuminati (0.3%)", 2950.0, 5),
        _fx(900102, 502, "Plush Pepe", "Gucci Bag (1%)", "Black (1%)", "Star (0.3%)", 3010.0, 60),
        _fx(900103, 503, "Plush Pepe", "Sunset (1.2%)", "Amber (1%)", "Pear (0.5%)", 3100.0, 120),
    ]


def mock_rules() -> list[BuyRule]:
    """Scaffold demo rules (old tests); tuned to mock_fixtures()."""
    return [
        BuyRule(collection="Toy Bear", max_price_ton=12, min_discount_pct=25),
        BuyRule(collection="Plush Pepe", max_price_ton=2500, min_discount_pct=10),
    ]


# --- demo scenario: the named fixtures F1-F8 used by `python -m tonnel_autobuy mock` --------------
# Prices are raw listed prices (Tonnel adds its 10% buy fee on top). gift_id = collection base + number.

_LS, _DC, _JB, _SA, _HC = 810000, 820000, 830000, 840000, 850000


def demo_listings(now: float) -> tuple[list[dict], list[dict]]:
    """(initial book, listings revealed by advance()). `now` positions the export_at lock date."""
    unlock = datetime.fromtimestamp(now + 6 * 86400 - 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    def g(base, num, name, model, backdrop, symbol, price, minutes_ago=30, **extra):
        return _fx(base + num, num, name, model, backdrop, symbol, price, minutes_ago, **extra)

    ls, dc, jb, sa, hc = "Lunar Snake", "Desk Calendar", "Jelly Bunny", "Spy Agaric", "Homemade Cake"
    albino, cobra = "Albino (1.2%)", "Cobra (2%)"
    initial = [
        # F1 'troll ask': one model at 1.00, 3.00, 1.60, 1.62, 1.65 -> cluster floor 1.60, not 3.00.
        g(_LS, 4410, ls, albino, "Onyx Black (1%)", "Star (0.3%)", 1.00, 5),
        g(_LS, 4411, ls, albino, "Ivory White (2%)", "Moon (0.5%)", 3.00, 40),
        g(_LS, 4413, ls, albino, "Mint Green (1.5%)", "Leaf (0.8%)", 1.60, 50),
        g(_LS, 4414, ls, albino, "Sky Blue (2%)", "Pin (1%)", 1.62, 60),
        g(_LS, 4415, ls, albino, "Coral Red (1%)", "Clip (0.4%)", 1.65, 70),
        # F2 'loses after fees': 0.97 against a cluster floor of 1.00: cost 1.067 with the 10% buy fee.
        g(_LS, 4412, ls, cobra, "Onyx Black (1%)", "Moon (0.5%)", 0.97, 10),
        g(_LS, 4420, ls, cobra, "Ivory White (2%)", "Star (0.3%)", 1.00, 80),
        g(_LS, 4421, ls, cobra, "Mint Green (1.5%)", "Leaf (0.8%)", 1.02, 90),
        g(_LS, 4422, ls, cobra, "Sky Blue (2%)", "Pin (1%)", 1.05, 95),
        # Confirm-mode deal (repriced before the tap) and a near miss just over the rule's max.
        g(_DC, 3002, dc, "Classic (2%)", "Sky Blue (2%)", "Pin (1%)", 2.20, 12),
        g(_DC, 3001, dc, "Classic (2%)", "Coral Red (1%)", "Clip (0.4%)", 2.60, 14),
        *[g(_DC, 3010 + i, dc, "Retro (1%)", "Onyx Black (1%)", "Star (0.3%)", p, 100 + i)
          for i, p in enumerate((3.20, 3.25, 3.30, 3.35, 3.40))],
        # Watch-mode deal, F3 'locked' (export_at in 6 days) and F5 'trap' (priced in USDT).
        g(_JB, 55, jb, "Pinky (1%)", "Ivory White (2%)", "Moon (0.5%)", 1.50, 15),
        g(_JB, 50, jb, "Pinky (1%)", "Onyx Black (1%)", "Star (0.3%)", 2.00, 110),
        g(_JB, 51, jb, "Pinky (1%)", "Mint Green (1.5%)", "Leaf (0.8%)", 2.05, 115),
        g(_JB, 52, jb, "Pinky (1%)", "Sky Blue (2%)", "Pin (1%)", 2.10, 120),
        g(_JB, 53, jb, "Pinky (1%)", "Coral Red (1%)", "Clip (0.4%)", 1.20, 16, export_at=unlock),
        g(_JB, 54, jb, "Pinky (1%)", "Onyx Black (1%)", "Moon (0.5%)", 1.10, 17, asset="USDT"),
        # F7 'cheaper elsewhere': 5.00 all-in here (4.55 + 10%), 4.60 all-in on Portals (fixture snapshot).
        g(_SA, 777, sa, "Amanita (1%)", "Onyx Black (1%)", "Star (0.3%)", 4.55, 18),
        *[g(_SA, 780 + i, sa, "Amanita (1%)", "Ivory White (2%)", "Moon (0.5%)", p, 130 + i)
          for i, p in enumerate((5.50, 5.55, 5.60, 5.65, 5.70))],
        # F8 'thin floor above Telegram': one comparable only; Telegram's average is lower.
        g(_HC, 1234, hc, "Choco (2%)", "Sky Blue (2%)", "Pin (1%)", 3.00, 19),
        g(_HC, 1240, hc, "Choco (2%)", "Mint Green (1.5%)", "Leaf (0.8%)", 4.00, 140),
    ]
    later = [
        # F4 'too good': 10% of the floor -> alert only, never auto-bought.
        g(_LS, 4416, ls, albino, "Onyx Black (1%)", "Moon (0.5%)", 0.16, 0),
        # F6 'healthy deal': ends in a practice buy.
        g(_LS, 4417, ls, albino, "Mint Green (1.5%)", "Star (0.3%)", 1.20, 0),
    ]
    return initial, later


DEMO_RULES = [
    BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, min_profit_ton=0.1, source="demo"),
    BuyRule("Lunar Snake", 1.08, model="Cobra", source="demo"),
    BuyRule("Desk Calendar", 2.75, min_discount_pct=15, mode="confirm", source="demo"),
    BuyRule("Jelly Bunny", 5.0, min_discount_pct=10, mode="watch", source="demo"),
    BuyRule("Spy Agaric", 5.5, source="demo"),
    BuyRule("Homemade Cake", 3.5, min_confidence="low", source="demo"),
]


class MockClient:
    """Offline fake with fixture data. Call `next_cycle()` (or `advance()`) to evolve the market.

    scenario='legacy' keeps the scaffold fixtures (old tests); scenario='demo' is the F1-F8 book."""

    has_auth = True
    name = "mock"

    def __init__(self, scenario: str = "legacy", now: float | None = None) -> None:
        self.scenario = scenario
        self.cycle = 0
        if scenario == "demo":
            self._items, self._later = demo_listings(time.time() if now is None else now)
        else:
            self._items, self._later = mock_fixtures(), []
        self._items = copy.deepcopy(self._items)
        self.bought: list[str] = []
        self.requests = 0
        self.fail_lookups = False
        self.balance_ton = 100.0
        self.last_server_date: float | None = None
        self.last_response_at: float | None = None
        self.last_latency_ms: float | None = 120.0

    def _answered(self) -> None:
        """A mock market whose clock agrees with ours: /doctor's clock check reads these after a request."""
        self.last_response_at = self.last_server_date = time.time()

    def next_cycle(self) -> None:
        self.cycle += 1
        if self.scenario == "demo":
            if self.cycle >= 2 and self._later:
                self._items.extend(self._later)
                self._later = []
            return
        if self.cycle == 2:
            # The cheap Toy Bear got sold to someone else; a fresh listing appears.
            self._items = [i for i in self._items if i["gift_id"] != 900001]
            self._items.append(
                _fx(900005, 1005, "Toy Bear", "Wizard (1.5%)", "Amber (1%)", "Star (0.3%)", 14.3, 0)
            )

    def advance(self) -> None:
        self.next_cycle()

    def reprice(self, listing_id: str, price_ton: float) -> None:
        for raw in self._items:
            if str(raw["gift_id"]) == str(listing_id):
                raw["price"] = price_ton

    def remove(self, listing_id: str) -> None:
        self._items = [r for r in self._items if str(r["gift_id"]) != str(listing_id)]

    def crash(self, collection: str, model: str | None, factor: float) -> None:
        """Move every listing of a collection/model by `factor` (mock --crash)."""
        for raw in self._items:
            if norm(raw["name"]) == norm(collection) and (not model or norm(raw["model"]) == norm(model)):
                raw["price"] = round(raw["price"] * factor, 4)

    def _current(self) -> list[Listing]:
        return parse_listings([dict(i) for i in self._items])

    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        self.requests += 1
        self._answered()
        res = [x for x in self._current() if norm(x.collection) == norm(filters.collection)]
        for attr in ("model", "backdrop", "symbol"):
            want = getattr(filters, attr)
            if want:
                res = [x for x in res if norm(getattr(x, attr)) == norm(want)]
        if filters.sort == "price_asc":
            res.sort(key=lambda x: (x.price_ton, -int(x.id)))
        elif filters.sort == "price_desc":
            res.sort(key=lambda x: (-x.price_ton, -int(x.id)))
        else:
            res.sort(key=lambda x: x.listed_at or datetime.min.replace(tzinfo=timezone.utc),
                     reverse=True)
        start = (filters.page - 1) * filters.limit
        return res[start : start + filters.limit]

    async def collection_floors(self) -> FloorTable:
        self.requests += 1
        self._answered()
        table = FloorTable()
        for x in self._current():
            if str((x.raw or {}).get("asset", "TON")).upper() == "TON":
                table.add(x.collection, x.model, x.price_ton, 1)
        return table

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        self.requests += 1
        if self.fail_lookups:
            raise TonnelError("network error: mock lookup failure")
        return find_listing(self._current(), listing_id)

    async def balance(self) -> float:
        return self.balance_ton

    async def buy(self, listing: Listing) -> BuyResult:
        self.bought.append(listing.id)
        self.remove(listing.id)
        return BuyResult(True, "mock purchase", raw={"status": "success"})

    async def close(self) -> None:
        return None
