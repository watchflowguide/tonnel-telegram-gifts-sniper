"""Second opinion: Telegram's own valuation and the same gift on your other markets.

Three optional sources, each failing silently and cached:
A) Telegram valuation via payments.getUniqueStarGiftValueInfo (Telethon user session);
B) other bots' floors_<market>.json snapshot files (no network, no imports between repos);
C) Portals public collection floors (opt-in, PEER_PORTALS_PUBLIC).

Snapshot schema (shared with other bots you run, documented in the README):
{schema:1, market, ts, fees:{buy_pct, sell_pct, withdraw_ton},
 collections:{<key>:{name, floor, n, models:{<key>: floor}}}}
Keys are lowercase alnum (Tonnel strips its '(1.5%)' rarity suffix first). Floors are raw listed
prices; the fees travel inside the file. No secrets or sessions are ever written.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .fx import TonRate
from .pricing import DealMath, FeeModel

if TYPE_CHECKING:
    from .config import Settings
    from .models import Listing
    from .rules import BuyRule
    from .tg_market import TgMarket

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MARKET_TITLES = {"portals": "Portals", "mrkt": "MRKT", "tonnel": "Tonnel", "getgems": "Getgems", "telegram": "Telegram"}
# TODO(verify): Portals fees, used only for its public floors (snapshot files carry their own fees).
PORTALS_DEFAULT_FEES = FeeModel(0.0, 5.0, 0.1)
# ISO 4217 minor-unit exponents that differ from 2 (Telegram amounts are in the smallest unit).
CURRENCY_EXP = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "UGX": 0, "XAF": 0, "XOF": 0,
                "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3, "IQD": 3, "LYD": 3}

_RARITY_SUFFIX = re.compile(r"\s*\(\s*\d+(?:[.,]\d+)?\s*%\s*\)\s*$")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def _load_aliases() -> dict[str, str]:
    try:
        data = json.loads((Path(__file__).with_name("aliases.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, str)}


ALIASES = _load_aliases()


def peer_key(name: str | None) -> str:
    """"Durov's Cap", 'DurovsCap' and "durov's cap" share one key; aliases map plural/variant names."""
    if not name:
        return ""
    key = _NON_ALNUM.sub("", _RARITY_SUFFIX.sub("", name).lower())
    return ALIASES.get(key, key)


# --- snapshot files -----------------------------------------------------------------------


def build_snapshot(market: str, fees: FeeModel, floors: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    """floors: {display name: {"floor": float, "n": int, "models": {model name: floor}}}."""
    collections: dict[str, Any] = {}
    for name, info in floors.items():
        key = peer_key(name)
        if not key or info.get("floor") is None:
            continue
        entry = collections.setdefault(key, {"name": name, "floor": float(info["floor"]), "n": 0, "models": {}})
        entry["floor"] = min(entry["floor"], float(info["floor"]))
        entry["n"] = max(entry["n"], int(info.get("n") or 0))
        for model, floor in (info.get("models") or {}).items():
            if floor is not None and peer_key(model):
                mk = peer_key(model)
                entry["models"][mk] = min(float(floor), entry["models"].get(mk, math.inf))
    return {
        "schema": SCHEMA_VERSION,
        "market": market,
        "ts": now,
        "fees": {"buy_pct": fees.buy_pct, "sell_pct": fees.sell_pct, "withdraw_ton": fees.withdraw_ton},
        "collections": collections,
    }


def write_snapshot(path: str | Path, snapshot: dict[str, Any]) -> None:
    """Atomic write: temp file in the same directory, then os.replace. Readers never see a partial file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass(slots=True)
class PeerSource:
    market: str
    path: str
    status: str  # ok | missing | invalid | stale | own | off
    age_s: float | None = None
    data: dict[str, Any] | None = None
    note: str = ""

    @property
    def title(self) -> str:
        return MARKET_TITLES.get(self.market, self.market.title())

    def fees(self) -> FeeModel:
        f = (self.data or {}).get("fees") or {}
        try:
            return FeeModel(float(f.get("buy_pct", 0)), float(f.get("sell_pct", 0)), float(f.get("withdraw_ton", 0)))
        except (TypeError, ValueError):
            return FeeModel()


def _market_from_path(path: str) -> str:
    m = re.search(r"floors_([a-z0-9]+)\.json$", path)
    return m.group(1) if m else Path(path).stem


def read_snapshot(path: str, own_market: str, now: float, max_age: float) -> PeerSource:
    guess = _market_from_path(path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PeerSource(guess, path, "missing")
    except (OSError, ValueError) as exc:
        return PeerSource(guess, path, "invalid", note=type(exc).__name__)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION or not isinstance(data.get("collections"), dict):
        return PeerSource(guess, path, "invalid", note="schema mismatch")
    market = str(data.get("market") or guess)
    try:
        age = now - float(data.get("ts", 0))
    except (TypeError, ValueError):
        return PeerSource(market, path, "invalid", note="bad ts")
    if market == own_market:
        return PeerSource(market, path, "own", age, None)
    if age > max_age:
        return PeerSource(market, path, "stale", age, None)
    return PeerSource(market, path, "ok", age, data)


# --- quotes, Telegram value, view -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PeerQuote:
    market: str
    level: str  # model | collection
    floor: float
    all_in_cost: float
    net_if_sold: float
    age_s: float

    @property
    def title(self) -> str:
        return MARKET_TITLES.get(self.market, self.market.title())


@dataclass(frozen=True, slots=True)
class TgValue:
    """Telegram's valuation; fiat amounts already divided by 10**exp. TON fields need a USD rate."""

    currency: str
    value: float | None
    average: float | None
    last_sale: float | None
    last_sale_date: float | None
    floor: float | None
    listed: int | None
    value_is_average: bool = False
    usd_per_ton: float | None = None

    def ton(self, amount: float | None) -> float | None:
        if amount is None or self.currency != "USD" or not self.usd_per_ton:
            return None
        return amount / self.usd_per_ton

    @property
    def guard_ton(self) -> float | None:
        """max(average, last sale) in TON, or None when it cannot be converted."""
        vals = [self.ton(x) for x in (self.average, self.last_sale)]
        vals = [v for v in vals if v is not None]
        return max(vals) if vals else None

    @property
    def guard_basis(self) -> tuple[str, float] | None:
        """('average' | 'last sale', TON) for the larger of the two, so the card names the one it used."""
        vals = [(name, self.ton(x)) for name, x in (("average", self.average), ("last sale", self.last_sale))]
        vals = [(name, v) for name, v in vals if v is not None]
        return max(vals, key=lambda nv: nv[1]) if vals else None

    def line(self, now: float) -> str:
        def amt(x: float | None) -> str:
            ton = self.ton(x)
            if ton is not None:
                return f"~{ton:.2f} TON" if ton < 10 else f"~{ton:.1f} TON"
            return f"{x:,.2f} {self.currency}" if x is not None else "n/a"

        parts = [f"avg {amt(self.average if self.average is not None else self.value)}"]
        if self.last_sale is not None:
            ago = f" ({(now - self.last_sale_date) / 86400:.0f} d ago)" if self.last_sale_date else ""
            parts.append(f"last sale {amt(self.last_sale)}{ago}")
        if self.listed is not None:
            parts.append(f"{self.listed} listed")
        return "Telegram: " + " · ".join(parts)


def minor_to_major(amount: Any, currency: str) -> float | None:
    if amount in (None, ""):
        return None
    try:
        return int(amount) / 10 ** CURRENCY_EXP.get(currency.upper(), 2)
    except (TypeError, ValueError):
        return None


def parse_tg_value(info: dict[str, Any], usd_per_ton: float | None) -> TgValue:
    """payments.UniqueStarGiftValueInfo as a dict -> TgValue.

    TODO(verify): the docs never define 'same type'; floor/average/listed are treated as collection context.
    """
    cur = str(info.get("currency") or "USD").upper()
    return TgValue(
        currency=cur,
        value=minor_to_major(info.get("value"), cur),
        average=minor_to_major(info.get("average_price"), cur),
        last_sale=minor_to_major(info.get("last_sale_price"), cur),
        last_sale_date=float(info["last_sale_date"]) if info.get("last_sale_date") else None,
        floor=minor_to_major(info.get("floor_price"), cur),
        listed=int(info["listed_count"]) if info.get("listed_count") is not None else None,
        value_is_average=bool(info.get("value_is_average")),
        usd_per_ton=usd_per_ton if cur == "USD" else None,
    )


@dataclass(slots=True)
class PeerView:
    quotes: list[PeerQuote] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # configured markets with no fresh data
    tg: TgValue | None = None
    tg_status: str = "off"

    @property
    def empty(self) -> bool:
        return not self.quotes and not self.missing and self.tg is None

    def elsewhere_line(self) -> str | None:
        if not self.quotes and not self.missing:
            return None
        parts = [f"{q.title} {q.all_in_cost:.2f}" for q in self.quotes]
        parts += [f"{MARKET_TITLES.get(m, m.title())} n/a" for m in self.missing]
        return "Elsewhere: " + " · ".join(parts) + " (all-in)"


def best_quote_for(source: PeerSource, collection: str, model: str | None, level: str) -> PeerQuote | None:
    coll = (source.data or {}).get("collections", {}).get(peer_key(collection))
    if not coll:
        return None
    if level == "model":
        floor = (coll.get("models") or {}).get(peer_key(model))
    else:
        floor = coll.get("floor")
    if floor is None:
        return None
    fees = source.fees()
    return PeerQuote(source.market, level, float(floor), fees.cost(float(floor)), fees.net_exit(float(floor)),
                     source.age_s or 0.0)


def guards(view: PeerView | None, deal: DealMath, *, cheaper_pct: float = 3.0, tg_guard: bool = True) -> list[tuple[str, str]]:
    """Reasons to ask first. They only ever turn auto into confirm; they never block."""
    out: list[tuple[str, str]] = []
    if view is None:
        return out
    cheaper = [q for q in view.quotes if q.all_in_cost < deal.cost * (1 - cheaper_pct / 100)]
    if cheaper:
        q = min(cheaper, key=lambda x: x.all_in_cost)
        out.append(("CHEAPER_ELSEWHERE", f"cheaper on {q.title} ({q.all_in_cost:.2f} all-in)"))
    if tg_guard and view.tg is not None and deal.confidence == "low":
        basis = view.tg.guard_basis
        if basis is not None and deal.cost > basis[1]:
            out.append(("ABOVE_TG_VALUE", f"above Telegram's {basis[0]} ({basis[1]:.2f} TON)"))
    return out


def best_exit(own_market: str, own_net: float | None, quotes: list[PeerQuote], own_withdraw: float) -> tuple[str, float] | None:
    """exit='best': highest net_if_sold across our floor and peers (moving out costs our withdraw fee)."""
    options: list[tuple[str, float]] = []
    if own_net is not None:
        options.append((own_market, own_net))
    options += [(q.market, q.net_if_sold - own_withdraw) for q in quotes]
    return max(options, key=lambda x: x[1]) if options else None


# --- Telegram valuation service ---------------------------------------------------------------

TgFetch = Callable[[str], Awaitable[dict[str, Any] | None]]


class TgValueService:
    """Cache per slug (TTL), at most `max_per_min` calls, FloodWait disables lookups until it passes."""

    def __init__(self, fetch: TgFetch | None, *, ttl: float = 600, max_per_min: int = 10,
                 clock: Callable[[], float] = time.time) -> None:
        self.fetch = fetch
        self.ttl = ttl
        self.max_per_min = max_per_min
        self.clock = clock
        self.calls = 0
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self._recent: deque[float] = deque()
        self.disabled_until = 0.0
        self.last_error: str | None = None

    def status(self) -> str:
        if self.fetch is None:
            return "n/a (no Telegram session)"
        now = self.clock()
        if now < self.disabled_until:
            return f"paused by FloodWait for {self.disabled_until - now:.0f}s"
        return f"ok ({self.calls} calls, {len(self._cache)} cached)" + (f", last error {self.last_error}" if self.last_error else "")

    async def get(self, slug: str | None) -> dict[str, Any] | None:
        if self.fetch is None or not slug:
            return None
        now = self.clock()
        hit = self._cache.get(slug)
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        if now < self.disabled_until:
            return None
        while self._recent and now - self._recent[0] >= 60:
            self._recent.popleft()
        if len(self._recent) >= self.max_per_min:
            return None
        self._recent.append(now)
        self.calls += 1
        try:
            info = await self.fetch(slug)
        except Exception as exc:
            seconds = getattr(exc, "seconds", None)
            if type(exc).__name__.startswith("FloodWait") or isinstance(seconds, (int, float)):
                self.disabled_until = now + float(seconds or 60)
                self.last_error = f"FloodWait {seconds}s"
            else:
                self.last_error = type(exc).__name__
            return None
        self._cache[slug] = (now, info)
        return info


class TelethonTgValue:
    """Real fetcher: GetUniqueStarGiftValueInfoRequest(slug) on the logged-in Telethon session."""

    def __init__(self, session: Any, timeout: float = 5.0) -> None:
        self.session = session  # tg_auth.TelegramSession
        self.timeout = timeout

    async def __call__(self, slug: str) -> dict[str, Any] | None:
        from telethon.tl.functions.payments import GetUniqueStarGiftValueInfoRequest

        client = await self.session.client()
        res = await asyncio.wait_for(client(GetUniqueStarGiftValueInfoRequest(slug=slug)), self.timeout)
        date = getattr(res, "last_sale_date", None)
        return {
            "currency": getattr(res, "currency", "USD"),
            "value": getattr(res, "value", None),
            "value_is_average": getattr(res, "value_is_average", False),
            "last_sale_price": getattr(res, "last_sale_price", None),
            "last_sale_date": date.timestamp() if hasattr(date, "timestamp") else date,
            "floor_price": getattr(res, "floor_price", None),
            "average_price": getattr(res, "average_price", None),
            "listed_count": getattr(res, "listed_count", None),
            "fragment_listed_count": getattr(res, "fragment_listed_count", None),
        }


class FakeTgValue:
    """Fixture fetcher for mock mode and tests: slug -> value info in cents."""

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self.data = data
        self.calls: list[str] = []

    async def __call__(self, slug: str) -> dict[str, Any] | None:
        self.calls.append(slug)
        return self.data.get(slug)


# --- Portals public floors ------------------------------------------------------------------


def parse_portals_collections(data: Any) -> dict[str, Any]:
    """GET portal-market.com/api/collections (public, verified 2026-10-28) -> snapshot 'collections'."""
    items = data.get("collections", data) if isinstance(data, dict) else data
    out: dict[str, Any] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name, floor = item.get("name"), item.get("floor_price")
        try:
            floor_f = float(floor) if floor not in (None, "") else None
        except (TypeError, ValueError):
            floor_f = None
        if name and floor_f:
            out[peer_key(name)] = {"name": name, "floor": floor_f, "n": int(item.get("listed_count") or 0), "models": {}}
    return out


class PortalsPublic:
    def __init__(self, url: str, ttl: float = 300, fetch: Callable[[str], Awaitable[Any]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        from .fx import get_json

        self.url = url
        self.ttl = ttl
        self._fetch = fetch or get_json
        self.clock = clock
        self._data: dict[str, Any] | None = None
        self._at = 0.0
        self.last_error: str | None = None

    async def source(self) -> PeerSource:
        now = self.clock()
        if self._data is None or now - self._at > self.ttl:
            try:
                cols = parse_portals_collections(await self._fetch(self.url))
                f = PORTALS_DEFAULT_FEES
                self._data = {"schema": 1, "market": "portals", "ts": now, "collections": cols,
                              "fees": {"buy_pct": f.buy_pct, "sell_pct": f.sell_pct, "withdraw_ton": f.withdraw_ton}}
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
            self._at = now
        if self._data is None:
            return PeerSource("portals", self.url, "invalid", note=self.last_error or "")
        return PeerSource("portals", self.url, "ok", now - float(self._data["ts"]), self._data, note="public API")


# --- orchestration -----------------------------------------------------------------------------


class Peers:
    def __init__(
        self,
        settings: "Settings",
        own_market: str = "tonnel",
        *,
        tg: TgValueService | None = None,
        rate: TonRate | None = None,
        portals: PortalsPublic | None = None,
        clock: Callable[[], float] = time.time,
        rewrite_ts: bool = False,
        tg_market: "TgMarket | None" = None,
    ) -> None:
        self.settings = settings
        self.own_market = own_market
        self.tg = tg
        self.rate = rate
        self.portals = portals
        self.clock = clock
        self.rewrite_ts = rewrite_ts  # mock: fixture files are 'fresh' unless they say otherwise
        self.tg_market = tg_market  # Telegram's own resale market (TG_MARKET=true), see tg_market.py
        self._sources: tuple[float, list[PeerSource]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.peer_floor_files or self.portals or self.tg
                    or (self.tg_market is not None and self.tg_market.enabled))

    def file_sources(self, now: float) -> list[PeerSource]:
        if self._sources and now - self._sources[0] < 5:
            return self._sources[1]
        out = [read_snapshot(p, self.own_market, now, self.settings.PEER_MAX_AGE_SEC) for p in self.settings.peer_floor_files]
        self._sources = (now, out)
        return out

    async def sources(self, now: float) -> list[PeerSource]:
        out = list(self.file_sources(now))
        fresh_portals = any(s.market == "portals" and s.status == "ok" for s in out)
        if self.portals is not None and not fresh_portals:
            out = [s for s in out if s.market != "portals"] + [await self.portals.source()]
        if self.tg_market is not None:
            out = [s for s in out if s.market != "telegram" or s.status == "ok"] + [self.tg_market.source(now)]
        return out

    async def view(self, listing: "Listing", rule: "BuyRule", deal: DealMath, timeout: float | None = None) -> PeerView:
        """Snapshot files of other bots are local reads and always count. The network parts (Portals public API,
        Telegram valuation) share `timeout` (default PEERS_TIMEOUT_SEC; the engine passes what is left of
        its per-cycle budget); on timeout or any failure they just hide their lines."""
        now = self.clock()
        view = PeerView()
        level = "model" if rule.model else "collection"
        files = self.file_sources(now)
        remote_portals = self.portals is not None and not any(s.market == "portals" and s.status == "ok" for s in files)
        for src in files:
            if not (remote_portals and src.market == "portals"):
                self._add_source(view, src, listing, level)
        budget = self.settings.PEERS_TIMEOUT_SEC if timeout is None else min(timeout, self.settings.PEERS_TIMEOUT_SEC)
        try:
            if budget <= 0:
                raise TimeoutError("no time left in this cycle's peers budget")
            await asyncio.wait_for(self._remote(view, listing, level, remote_portals), budget)
        except Exception as exc:  # timeout or anything unexpected: no network second opinion this time
            log.debug("peers view failed: %s", exc)
            if remote_portals and "portals" not in view.missing and not any(q.market == "portals" for q in view.quotes):
                view.missing.append("portals")
            if self.tg is not None and self.settings.TG_VALUE_ENABLED and view.tg is None:
                view.tg_status = "timeout"
        return view

    @staticmethod
    def _add_source(view: PeerView, src: PeerSource, listing: "Listing", level: str) -> None:
        if src.status == "own":
            return
        quote = best_quote_for(src, listing.collection, listing.model, level) if src.status == "ok" else None
        if quote:
            view.quotes.append(quote)
        elif src.market not in view.missing:
            view.missing.append(src.market)

    async def _remote(self, view: PeerView, listing: "Listing", level: str, remote_portals: bool) -> None:
        if remote_portals and self.portals is not None:
            self._add_source(view, await self.portals.source(), listing, level)
        if self.tg_market is not None and self.tg_market.enabled:
            await self.tg_market.quote(listing.collection, listing.model if level == "model" else None)
            self._add_source(view, self.tg_market.source(self.clock()), listing, level)
        if self.tg is not None and self.settings.TG_VALUE_ENABLED:
            slug = listing.slug
            info = await self.tg.get(slug)
            # No valuation, no rate. The cached rate (the engine refreshes it in the background each cycle) is
            # used when there is one, so a buy never waits for the rate request.
            rate = None
            if self.rate is not None and info:
                rate = self.rate.value if self.rate.value is not None else await self.rate.usd_per_ton()
            view.tg = parse_tg_value(info, rate) if info else None
            view.tg_status = "ok" if info else self.tg.status()

    async def status_lines(self) -> list[str]:
        now = self.clock()
        lines = []
        for src in await self.sources(now):
            age = f"{src.age_s:.0f}s old" if src.age_s is not None else "no data"
            extra = f" ({src.note})" if src.note else ""
            word = {"ok": "fresh", "stale": "stale", "missing": "missing", "invalid": "unreadable",
                    "own": "own market, ignored"}.get(src.status, src.status)
            lines.append(f"{src.title}: {word}, {age}{extra} · {src.path}")
        if not lines:
            lines.append("Other markets: off (PEER_FLOOR_FILES is empty)")
        lines.append(f"Telegram value: {self.tg.status() if self.tg else 'off'}")
        lines.append(f"Telegram resale: {self.tg_market.status() if self.tg_market else 'off (TG_MARKET=false)'}")
        lines.append(f"TON rate: {self.rate.status() if self.rate else 'off'}")
        return lines


def load_fixture_snapshot(path: str | Path, now: float, *, keep_age: bool = True) -> dict[str, Any]:
    """Mock helper: load a fixture snapshot and rewrite ts to now (keeping a negative 'age_s' offset)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    offset = float(data.pop("age_s", 0)) if keep_age else 0.0
    data["ts"] = now - offset
    return data
