"""Telegram's own resale market as one more reference source (opt-in: TG_MARKET=true).

Telegram lists collectible gifts for resale inside the app. payments.getResaleStarGifts (a user-only
MTProto method, so it runs on the Telethon session this bot already holds) gives Telegram's own floor
and listed count for a collection, sorted by price. The quote becomes a peer source named 'telegram':
it shows up in the 'Elsewhere (net)' line, in /compare and in /peers, it can make a rule ask first
(CHEAPER_ELSEWHERE), and it is written to DATA_DIR/floors_telegram.json for other bots. It is only ever
an extra reference: it never triggers a buy on its own and the bot never buys on Telegram.

Limits: at most one request per TG_MARKET_MIN_INTERVAL_SEC (a call inside the interval returns the cached
quote or nothing), quotes cached for 10 min, a FloodWait pauses every call until it has passed. If the
installed Telethon layer lacks the TL methods, the source disables itself with one log line. Mock mode
uses MockTgMarket with fixture resale data and never touches Telethon.

TODO(verify): the response shapes below (StarGift.title, StarGiftUnique.resell_amount, the model
attribute name, StarsTonAmount units) were taken from the TL schema, not from a live call. The brief
quotes TON resale prices as 1/100 TON; the TL schema documents starsTonAmount.amount in nanotons, which
is what TON_AMOUNT_UNIT uses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .peers import PeerSource, build_snapshot, peer_key
from .pricing import FeeModel

if TYPE_CHECKING:
    from .config import Settings

log = logging.getLogger(__name__)

MARKET = "telegram"
CACHE_TTL_SEC = 600.0
TON_AMOUNT_UNIT = 1e9  # TODO(verify): starsTonAmount.amount in nanotons per the TL schema
# TODO(verify): Telegram's resale commission. Used only to turn its floor into a net exit.
TG_MARKET_FEES = FeeModel(0.0, 20.0, 0.0)

# One resale offer: {"model": str | None, "stars": int} or {"model": str | None, "ton": float (TON)}.
Offer = dict[str, Any]
ResaleFetch = Callable[[str, int], Awaitable[tuple[list[Offer], int | None]]]


def offer_ton(offer: Offer, *, stars_usd: float, usd_per_ton: float | None) -> float | None:
    """TON price of one offer. Stars go through STARS_USD_RATE and the TON->USD rate; without a rate a
    Stars-only offer has no TON price (it is skipped, never guessed)."""
    if offer.get("ton") is not None:
        try:
            return float(offer["ton"])
        except (TypeError, ValueError):
            return None
    stars = offer.get("stars")
    if stars is None or not usd_per_ton or usd_per_ton <= 0:
        return None
    try:
        return float(stars) * stars_usd / usd_per_ton
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class TgResaleQuote:
    collection: str
    model: str | None
    floor_ton: float
    second_ton: float | None
    listed: int
    ts: float


def quote_from_offers(collection: str, model: str | None, offers: list[Offer], total: int | None, now: float, *,
                      stars_usd: float, usd_per_ton: float | None) -> TgResaleQuote | None:
    """Cheapest and second-cheapest TON price among the offers of this model (or the whole collection)."""
    want = peer_key(model) if model else ""
    prices = sorted(
        p for o in offers
        if not want or peer_key(o.get("model")) == want
        for p in [offer_ton(o, stars_usd=stars_usd, usd_per_ton=usd_per_ton)]
        if p is not None and p > 0
    )
    if not prices:
        return None
    listed = len(prices) if want or total is None else max(int(total), len(prices))
    return TgResaleQuote(collection, model, prices[0], prices[1] if len(prices) > 1 else None, listed, now)


class TgMarket:
    """Cache, rate limit and FloodWait handling around a ResaleFetch. fetch=None means the source is off."""

    def __init__(self, fetch: ResaleFetch | None, *, limit: int = 50, min_interval: float = 5.0,
                 stars_usd: float = 0.013, rate: Any = None, ttl: float = CACHE_TTL_SEC,
                 clock: Callable[[], float] = time.time, disabled_reason: str | None = None) -> None:
        self.fetch = fetch
        self.limit = limit
        self.min_interval = min_interval
        self.stars_usd = stars_usd
        self.rate = rate  # fx.TonRate or None
        self.ttl = ttl
        self.clock = clock
        self.disabled_reason = disabled_reason if fetch is None else None
        self.calls = 0
        self.last_call = -1e18
        self.disabled_until = 0.0
        self.last_error: str | None = None
        self._cache: dict[str, tuple[float, list[Offer], int | None]] = {}
        self._quotes: dict[tuple[str, str], TgResaleQuote] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.fetch is not None

    def status(self) -> str:
        if self.fetch is None:
            return f"off ({self.disabled_reason or 'TG_MARKET=false'})"
        now = self.clock()
        if now < self.disabled_until:
            return f"paused by FloodWait for {self.disabled_until - now:.0f}s"
        return (f"ok ({self.calls} calls, {len(self._quotes)} quotes)"
                + (f", last error {self.last_error}" if self.last_error else ""))

    async def _offers(self, collection: str) -> tuple[list[Offer], int | None] | None:
        key = peer_key(collection)
        now = self.clock()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1], hit[2]
        if self.fetch is None or now < self.disabled_until or now - self.last_call < self.min_interval:
            return (hit[1], hit[2]) if hit else None  # at most one request per min_interval
        self.last_call = now
        self.calls += 1
        try:
            offers, total = await self.fetch(collection, self.limit)
        except Exception as exc:  # noqa: BLE001 - an extra reference never breaks a cycle
            seconds = getattr(exc, "seconds", None)
            if type(exc).__name__.startswith("FloodWait") or isinstance(seconds, (int, float)):
                self.disabled_until = now + float(seconds or 60)
                self.last_error = f"FloodWait {seconds}s"
            else:
                self.last_error = type(exc).__name__
            return (hit[1], hit[2]) if hit else None
        self.last_error = None
        self._cache[key] = (now, offers, total)
        return offers, total

    async def quote(self, collection: str, model: str | None = None) -> TgResaleQuote | None:
        if self.fetch is None:
            return None
        async with self._lock:
            got = await self._offers(collection)
        if got is None:
            return None
        usd_per_ton = await self.rate.usd_per_ton() if self.rate is not None else None
        now = self.clock()
        # The collection-level quote comes from the same offers (no extra request), so the snapshot's
        # collection floor is always the real collection floor, never one model's.
        whole = quote_from_offers(collection, None, got[0], got[1], now, stars_usd=self.stars_usd,
                                  usd_per_ton=usd_per_ton)
        if whole is None:
            return None
        self._quotes[(peer_key(collection), "")] = whole
        if not model:
            return whole
        q = quote_from_offers(collection, model, got[0], got[1], now, stars_usd=self.stars_usd,
                              usd_per_ton=usd_per_ton)
        if q is not None:
            self._quotes[(peer_key(collection), peer_key(model))] = q
        return q

    def snapshot(self, now: float) -> dict[str, Any] | None:
        """Fresh quotes as a floors snapshot (same schema as floors_tonnel.json), market 'telegram'."""
        floors: dict[str, dict[str, Any]] = {}
        fresh = {k: q for k, q in self._quotes.items() if now - q.ts <= self.ttl}
        for (coll_key, model_key), q in fresh.items():
            if not model_key:
                floors[coll_key] = {"name": q.collection, "floor": q.floor_ton, "n": q.listed, "models": {}}
        for (coll_key, model_key), q in fresh.items():
            if model_key and coll_key in floors:
                floors[coll_key]["models"][q.model] = q.floor_ton
        if not floors:
            return None
        floors = {v.pop("name"): v for v in floors.values()}
        return build_snapshot(MARKET, TG_MARKET_FEES, floors, now)

    def source(self, now: float) -> PeerSource:
        if self.fetch is None:
            return PeerSource(MARKET, "tg_market", "off", note=self.disabled_reason or "TG_MARKET=false")
        snap = self.snapshot(now)
        if snap is None:
            return PeerSource(MARKET, "tg_market", "missing", note="no quote yet")
        oldest = min(q.ts for q in self._quotes.values() if now - q.ts <= self.ttl)
        return PeerSource(MARKET, "tg_market", "ok", now - oldest, snap)


class TelethonTgMarket:
    """Real fetcher: payments.getStarGifts (collection title -> gift id, cached) and
    payments.getResaleStarGifts(gift_id, sort_by_price=True, limit) on the logged-in Telethon session."""

    REQUIRED = ("GetStarGiftsRequest", "GetResaleStarGiftsRequest")

    def __init__(self, session: Any, timeout: float = 5.0) -> None:
        self.session = session  # tg_auth.TelegramSession
        self.timeout = timeout
        self._ids: dict[str, int] | None = None

    @classmethod
    def missing_methods(cls) -> list[str]:
        try:
            from telethon.tl.functions import payments
        except Exception:  # noqa: BLE001 - Telethon not installed or broken
            return list(cls.REQUIRED)
        return [name for name in cls.REQUIRED if not hasattr(payments, name)]

    async def _gift_ids(self, client: Any) -> dict[str, int]:
        if self._ids is None:
            from telethon.tl.functions.payments import GetStarGiftsRequest

            res = await asyncio.wait_for(client(GetStarGiftsRequest(hash=0)), self.timeout)
            ids: dict[str, int] = {}
            for gift in getattr(res, "gifts", None) or []:
                title = getattr(gift, "title", None)  # TODO(verify): StarGift.title on the installed layer
                if title and getattr(gift, "id", None) is not None:
                    ids[peer_key(title)] = int(gift.id)
            self._ids = ids
        return self._ids

    async def __call__(self, collection: str, limit: int) -> tuple[list[Offer], int | None]:
        from telethon.tl.functions.payments import GetResaleStarGiftsRequest

        client = await self.session.client()
        gift_id = (await self._gift_ids(client)).get(peer_key(collection))
        if gift_id is None:
            return [], 0
        res = await asyncio.wait_for(
            client(GetResaleStarGiftsRequest(gift_id=gift_id, offset="", limit=limit, sort_by_price=True)),
            self.timeout)
        offers: list[Offer] = []
        for gift in getattr(res, "gifts", None) or []:
            model = next((getattr(a, "name", None) for a in getattr(gift, "attributes", None) or []
                          if type(a).__name__ == "StarGiftAttributeModel"), None)
            for amount in getattr(gift, "resell_amount", None) or []:
                if type(amount).__name__ == "StarsTonAmount":
                    offers.append({"model": model, "ton": int(amount.amount) / TON_AMOUNT_UNIT})
                    break
                if type(amount).__name__ == "StarsAmount":
                    offers.append({"model": model, "stars": int(amount.amount)})
                    break
        return offers, getattr(res, "count", None)


class MockTgMarket:
    """Fixture fetcher for mock mode and tests: collection -> offers. Never touches Telethon."""

    def __init__(self, data: dict[str, list[Offer]]) -> None:
        self.data = {peer_key(k): v for k, v in data.items()}
        self.calls: list[str] = []

    async def __call__(self, collection: str, limit: int) -> tuple[list[Offer], int | None]:
        self.calls.append(collection)
        offers = list(self.data.get(peer_key(collection), []))
        return offers[:limit], len(offers)


def build_tg_market(settings: "Settings", session: Any = None, *, rate: Any = None,
                    clock: Callable[[], float] = time.time) -> TgMarket | None:
    """None when TG_MARKET=false. Without a Telegram session, or when the Telethon layer lacks the TL
    methods, the source exists but is off, with the reason shown in /peers."""
    if not settings.TG_MARKET:
        return None
    kw = dict(limit=settings.TG_MARKET_LIMIT, min_interval=settings.TG_MARKET_MIN_INTERVAL_SEC,
              stars_usd=settings.STARS_USD_RATE, rate=rate, clock=clock)
    if session is None:
        return TgMarket(None, disabled_reason="no Telegram session (python -m tonnel_autobuy login)", **kw)
    missing = TelethonTgMarket.missing_methods()
    if missing:
        log.warning("TG_MARKET disabled: the installed Telethon layer lacks %s", ", ".join(missing))
        return TgMarket(None, disabled_reason=f"Telethon lacks {', '.join(missing)}", **kw)
    return TgMarket(TelethonTgMarket(session), **kw)
