"""Domain models shared by client, rules and engine."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_RARITY_RE = re.compile(r"^(.*?)\s*\(\s*[\d.]+\s*%\s*\)\s*$")


def strip_rarity(value: str | None) -> str | None:
    """'Wizard (1.5%)' -> 'Wizard'. Tonnel appends rarity to model/backdrop/symbol."""
    if value is None:
        return None
    m = _RARITY_RE.match(value)
    return (m.group(1) if m else value).strip()


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 buy', '2 buys', '0 comparables', '1 comparable listing' (count and noun always agree)."""
    return f"{n} {word if n == 1 else many or word + 's'}"


def norm(value: str | None) -> str:
    """Normalized attribute for comparisons: no rarity suffix, casefolded."""
    return (strip_rarity(value) or "").casefold().strip()


_SLUG_STRIP = re.compile(r"[^A-Za-z0-9]")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def alnum_key(value: str | None) -> str:
    """'Durov's Cap', 'DurovsCap' and 'durov's cap (1%)' -> 'durovscap' (t.me/nft slugs, cross-market keys)."""
    return _NON_ALNUM.sub("", (strip_rarity(value) or "").lower())


def nft_slug(collection: str | None, number: int | None) -> str | None:
    """t.me/nft slug: alnum collection name + '-' + number. TODO(verify) for unusual names."""
    if not collection or number is None:
        return None
    return f"{_SLUG_STRIP.sub('', collection)}-{number}"


def nft_url(collection: str | None, number: int | None) -> str | None:
    slug = nft_slug(collection, number)
    return f"https://t.me/nft/{slug}" if slug else None


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Listing:
    id: str  # Tonnel gift_id (NOT the Telegram gift number)
    collection: str
    model: str | None
    backdrop: str | None
    symbol: str | None
    number: int | None  # Telegram gift number (gift_num)
    price_ton: float  # listed price, WITHOUT the marketplace fee
    listed_at: datetime | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    def cost_ton(self, fee_pct: float) -> float:
        """What the buyer actually pays: listed price + fee."""
        return round(self.price_ton * (1.0 + fee_pct / 100.0), 9)

    @property
    def nft_url(self) -> str | None:
        return nft_url(self.collection, self.number)

    @property
    def url(self) -> str | None:
        return self.nft_url

    @property
    def slug(self) -> str | None:
        """Telegram slug for payments.getUniqueStarGiftValueInfo (same as the t.me/nft link)."""
        return nft_slug(self.collection, self.number)

    @property
    def key(self) -> str:
        """Dedupe key: a relist / price change of the same gift is a new event."""
        return f"{self.id}:{self.price_ton:.9g}"

    @property
    def short_title(self) -> str:
        num = f" #{self.number}" if self.number is not None else ""
        return f"{self.collection}{num}"

    def title(self) -> str:
        attrs = " / ".join(a for a in (self.model, self.backdrop, self.symbol) if a)
        num = f" #{self.number}" if self.number is not None else ""
        return f"{self.collection}{num}" + (f" [{attrs}]" if attrs else "")

    @classmethod
    def from_api(cls, d: dict) -> Listing:
        """Build from a Tonnel `pageGifts` item.

        Example item (seen live 2026-10-28):
        {"gift_num":14153,"gift_id":10477918,"name":"Toy Bear","model":"Kumamon (1%)",
         "asset":"TON","symbol":"Pear (0.5%)","backdrop":"Amber (1%)","price":3662,
         "status":"forsale","auction":null,"export_at":"2026-10-18T20:47:32.000Z", ...}
        """
        price = d.get("price")
        if price is None:
            raise ValueError(f"listing {d.get('gift_id')} has no price")
        if not math.isfinite(float(price)):  # json accepts NaN/Infinity, and NaN passes every `>` check
            raise ValueError(f"listing {d.get('gift_id')} has no usable price ({price!r})")
        # TODO(verify): no explicit listing timestamp is returned; the "latest" sort
        # uses `message_post_time`, which may or may not appear in items.
        listed_at = _parse_dt(d.get("message_post_time")) or _parse_dt(d.get("listed_at"))
        return cls(
            id=str(d["gift_id"]),
            collection=str(d.get("name") or d.get("gift_name") or ""),
            model=d.get("model"),
            backdrop=d.get("backdrop"),
            symbol=d.get("symbol"),
            number=_to_int(d.get("gift_num")),
            price_ton=float(price),
            listed_at=listed_at,
            raw=d,
        )


@dataclass(slots=True)
class FloorTable:
    """Floors from the API (raw listed prices, without fee)."""

    collections: dict[str, float] = field(default_factory=dict)  # norm(collection) -> floor
    models: dict[tuple[str, str], float] = field(default_factory=dict)  # (coll, model) -> floor
    counts: dict[str, int] = field(default_factory=dict)  # norm(collection) -> listings
    display: dict[str, str] = field(default_factory=dict)  # norm(collection) -> pretty name
    model_names: dict[tuple[str, str], str] = field(default_factory=dict)  # (coll, model) -> name w/o rarity

    def get(self, collection: str, model: str | None = None) -> float | None:
        if model:
            hit = self.models.get((norm(collection), norm(model)))
            if hit is not None:
                return hit
        return self.collections.get(norm(collection))

    def add(self, collection: str, model: str | None, floor: float | None, count: int = 0) -> None:
        if floor is None:
            return
        c = norm(collection)
        self.display.setdefault(c, collection)
        cur = self.collections.get(c)
        if cur is None or floor < cur:
            self.collections[c] = floor
        self.counts[c] = self.counts.get(c, 0) + int(count or 0)
        if model:
            key = (c, norm(model))
            self.model_names.setdefault(key, strip_rarity(model) or model)
            cur_m = self.models.get(key)
            if cur_m is None or floor < cur_m:
                self.models[key] = floor

    def __len__(self) -> int:
        return len(self.collections)

    def named(self) -> dict[str, float]:
        """{display name: collection floor} (the wizard's collection list)."""
        return {self.display.get(k, k): v for k, v in self.collections.items()}

    def models_of(self, collection: str) -> list[tuple[str, float, int]]:
        """(model name, floor, 0) for a collection, cheapest first."""
        c = norm(collection)
        out = [(self.model_names.get(k, k[1]), f, 0) for k, f in self.models.items() if k[0] == c]
        return sorted(out, key=lambda x: (x[1], x[0]))


@dataclass(slots=True)
class BuyResult:
    ok: bool
    message: str = ""
    raw: dict = field(default_factory=dict)
    uncertain: bool = False  # e.g. timeout: the buy may or may not have happened
