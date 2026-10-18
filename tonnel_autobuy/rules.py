"""Buy rules: definition, pure matching, parsing of /addrule and rules.json.

`match()` (and `BuyRule.match`) is a pure function. It takes net deal math (pricing.DealMath) and checks,
in this order: attributes, blocking flags, lock, max price on cost (incl. the buy fee), confidence,
discount, profit. The result carries a machine-readable reason code plus human text.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .models import Listing, norm, plural
from .pricing import CONFIDENCE, DealMath, conf_rank, legacy_deal

MODES = ("auto", "confirm", "watch")  # strictness grows left to right
EXITS = ("here", "best")
STATES = ("shadow", "live", "paused")

# Reason codes for match(); human text lives next to them in MatchResult.reason.
OK = "OK"
REASON_CODES = (
    "DISABLED",
    "MISMATCH",
    "BAD_PRICE",
    "TRAP_ASSET",
    "TRAP_BUNDLE",
    "TRAP_AUCTION",
    "TRAP_LUCKY",
    "LOCKED",
    "OVER_MAX",
    "LOW_CONFIDENCE",
    "LOW_DISCOUNT",
    "LOSES_AFTER_FEES",
    "LOW_PROFIT",
)


@dataclass(frozen=True, slots=True)
class MatchResult:
    ok: bool
    reason: str
    discount_pct: float | None = None
    code: str = OK
    deal: DealMath | None = None


@dataclass(slots=True)
class BuyRule:
    collection: str
    max_price_ton: float  # compared with the COST (listed price + buy fee)
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    min_discount_pct: float = 0.0  # vs floor; 0 = no discount requirement
    id: int | None = None
    enabled: bool = True
    source: str = "bot"  # bot | file | demo
    min_profit_ton: float = 0.0
    min_profit_pct: float = 0.0
    min_confidence: str = "med"  # low | med | high
    allow_locked_days: float = 0.0  # 0 = never buy locked gifts
    mode: str = "auto"  # auto | confirm | watch
    exit: str = "here"  # here | best
    state: str = "live"  # shadow | live | paused (shadow = practice-only even in LIVE)
    promoted_at: float | None = None

    def group_key(self) -> tuple[str, str, str, str]:
        """Rules with the same key share the same API searches."""
        return (norm(self.collection), norm(self.model), norm(self.backdrop), norm(self.symbol))

    def filter_key(self) -> tuple[str, str, str, str]:
        return self.group_key()

    def filter_key_str(self) -> str:
        return "|".join(self.group_key())

    @property
    def pins_attributes(self) -> bool:
        return bool(self.model or self.backdrop or self.symbol)

    def describe(self) -> str:
        parts = [f"#{self.id}" if self.id is not None else "#-", self.collection]
        for k in ("model", "backdrop", "symbol"):
            v = getattr(self, k)
            if v:
                parts.append(f"{k}={v}")
        parts.append(f"max={self.max_price_ton:g}")
        if self.min_discount_pct:
            parts.append(f"discount>={self.min_discount_pct:g}%")
        if self.min_profit_ton:
            parts.append(f"profit={self.min_profit_ton:g}")
        if self.min_profit_pct:
            parts.append(f"profit%={self.min_profit_pct:g}")
        if self.min_confidence != "med":
            parts.append(f"conf={self.min_confidence}")
        if self.allow_locked_days:
            parts.append(f"locked={self.allow_locked_days:g}")
        if self.mode != "auto":
            parts.append(f"mode={self.mode}")
        if self.exit != "here":
            parts.append(f"exit={self.exit}")
        if self.state != "live":
            parts.append(f"[{self.state}]")
        if not self.enabled:
            parts.append("(disabled)")
        return " ".join(parts)

    def match(self, listing: Listing, floor: "float | DealMath | None", fee_pct: float = 0.0) -> MatchResult:
        """Decide whether `listing` satisfies this rule.

        `floor` is net deal math (DealMath) or, for scaffold-era callers, a bare comparable floor in raw
        listed prices (without fee); then discount = (floor - price) / floor and cost = price x (1 + fee).
        """
        deal = floor if isinstance(floor, DealMath) else legacy_deal(listing, floor, fee_pct)
        return match(self, listing, deal)

    def to_dict(self) -> dict:
        return asdict(self)


def discount_pct(price_ton: float, floor_ton: float | None) -> float | None:
    """How far below the floor the price is, in percent (negative = above floor)."""
    if floor_ton is None or floor_ton <= 0:
        return None
    return (floor_ton - price_ton) / floor_ton * 100.0


def stricter_mode(*modes: str) -> str:
    return max(modes, key=lambda m: MODES.index(m) if m in MODES else 0)


def match(rule: BuyRule, listing: Listing, deal: "DealMath | float | None" = None) -> MatchResult:
    """Pure decision. Order: attributes, blocking flags, lock, max price on cost, confidence,
    discount, profit. `deal` is a DealMath (net math) or, for old callers, a bare floor (no fee)."""
    if not isinstance(deal, DealMath):
        deal = legacy_deal(listing, deal)
    disc = deal.discount_pct

    def no(code: str, text: str) -> MatchResult:
        return MatchResult(False, text, disc, code, deal)

    if not rule.enabled:
        return no("DISABLED", "rule disabled")
    if norm(listing.collection) != norm(rule.collection):
        return no("MISMATCH", "collection mismatch")
    for attr in ("model", "backdrop", "symbol"):
        want = getattr(rule, attr)
        if want and norm(getattr(listing, attr)) != norm(want):
            return no("MISMATCH", f"{attr} mismatch")
    if not math.isfinite(listing.price_ton) or listing.price_ton <= 0:
        return no("BAD_PRICE", "invalid price")
    for flag in deal.blocking:
        code = flag.code if flag.code in REASON_CODES else "BAD_PRICE"
        return no(code, flag.text)
    if deal.has("LOCKED"):
        days = deal.lock_days
        if rule.allow_locked_days <= 0 or days is None or days > rule.allow_locked_days:
            flag = deal.flag("LOCKED")
            return no("LOCKED", f"locked: {flag.text if flag else 'unlock date unknown'}")
    if deal.cost > rule.max_price_ton + 1e-9:
        if abs(deal.cost - deal.price) < 1e-9:
            return no("OVER_MAX", f"price {deal.price:g} > max {rule.max_price_ton:g}")
        return no("OVER_MAX", f"cost {deal.cost:.2f} incl. fees > max {rule.max_price_ton:g}")
    if conf_rank(deal.confidence) < conf_rank(rule.min_confidence):
        return no("LOW_CONFIDENCE",
                  f"floor confidence {deal.confidence} < {rule.min_confidence} ({plural(deal.n, 'comparable')})")
    if rule.min_discount_pct > 0:
        if disc is None:
            return no("LOW_DISCOUNT", "no floor to compare against")
        if disc < rule.min_discount_pct:
            return no("LOW_DISCOUNT", f"discount {disc:.1f}% < {rule.min_discount_pct:g}%")
    if deal.profit_ton is None:
        if rule.min_profit_ton > 0 or rule.min_profit_pct > 0:
            return no("LOW_PROFIT", "no floor to estimate profit")
    elif deal.profit_ton < 0:
        return no("LOSES_AFTER_FEES", f"loses {abs(deal.profit_ton):.2f} TON after fees")
    elif deal.profit_ton < rule.min_profit_ton:
        return no("LOW_PROFIT", f"profit {deal.profit_ton:+.2f} TON < {rule.min_profit_ton:g} TON")
    elif rule.min_profit_pct > 0 and (deal.profit_pct or 0) < rule.min_profit_pct:
        return no("LOW_PROFIT", f"profit {deal.profit_pct or 0:+.1f}% < {rule.min_profit_pct:g}%")
    return MatchResult(True, "ok", disc, OK, deal)


@dataclass(slots=True, frozen=True)
class NearMiss:
    check: str  # max | discount | profit
    value: float
    limit: float
    miss_pct: float
    new_max: float | None = None

    def text(self) -> str:
        if self.check == "max":
            return f"Missed by a hair: {self.value:.2f} TON, your max is {self.limit:.2f}"
        if self.check == "discount":
            return f"Missed by a hair: discount {self.value:.1f}%, your minimum is {self.limit:g}%"
        return f"Missed by a hair: profit {self.value:+.2f} TON, your minimum is {self.limit:g} TON"


def near_miss(rule: BuyRule, listing: Listing, deal: DealMath, *, pct: float = 5.0) -> NearMiss | None:
    """A listing that fails exactly one numeric check (max on cost, discount or profit) by <= pct."""
    relaxed = replace(rule, max_price_ton=math.inf, min_discount_pct=0.0, min_profit_ton=0.0, min_profit_pct=0.0)
    if not match(relaxed, listing, deal).ok:
        return None
    if not math.isfinite(rule.max_price_ton) or rule.max_price_ton <= 0:  # an old stored row: never divide by it
        return None
    misses: list[NearMiss] = []
    if deal.cost > rule.max_price_ton + 1e-9:
        new_max = math.ceil(round(deal.cost / 0.05, 6)) * 0.05
        misses.append(NearMiss("max", deal.cost, rule.max_price_ton,
                               (deal.cost - rule.max_price_ton) / rule.max_price_ton * 100, round(new_max, 2)))
    if rule.min_discount_pct > 0 and deal.discount_pct is not None and deal.discount_pct < rule.min_discount_pct:
        misses.append(NearMiss("discount", deal.discount_pct, rule.min_discount_pct,
                               (rule.min_discount_pct - deal.discount_pct) / rule.min_discount_pct * 100))
    if rule.min_profit_ton > 0 and deal.profit_ton is not None and deal.profit_ton < rule.min_profit_ton:
        misses.append(NearMiss("profit", deal.profit_ton, rule.min_profit_ton,
                               (rule.min_profit_ton - deal.profit_ton) / rule.min_profit_ton * 100))
    if len(misses) != 1 or misses[0].miss_pct > pct:
        return None
    return misses[0]


# ---------------------------------------------------------------- parsing

# "profit%" must come before "profit" so the regex alternation sees it whole.
_KEYS = ("max", "model", "backdrop", "symbol", "discount", "profit%", "profit", "conf", "locked", "mode", "exit")
_SPLIT_RE = re.compile(r"\s+(?=(?:%s)=)" % "|".join(re.escape(k) for k in _KEYS), re.IGNORECASE)


class RuleParseError(ValueError):
    pass


def _num(value: str, key: str) -> float:
    try:
        n = float(value.replace(",", ".").rstrip("%"))
    except ValueError as exc:
        raise RuleParseError(f"{key}= must be a number, got '{value}'") from exc
    if n < 0:
        raise RuleParseError(f"{key}= must be >= 0")
    return n


def _choice(key: str, value: str, allowed: tuple[str, ...]) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise RuleParseError(f"{key} must be one of {'|'.join(allowed)}, got '{value}'")
    return value


ADDRULE_FORMAT = ("<collection> max=<ton> [model=..] [backdrop=..] [symbol=..] [discount=<pct>] [profit=<ton>] "
                  "[profit%=<pct>] [conf=low|med|high] [locked=<days>] [mode=auto|confirm|watch] [exit=here|best]")


def parse_addrule_args(text: str) -> BuyRule:
    """Parse `/addrule` arguments.

    Format: `<collection> max=<ton> [model=..] [backdrop=..] [symbol=..] [discount=<pct>] [profit=<ton>]
    [profit%=<pct>] [conf=low|med|high] [locked=<days>] [mode=auto|confirm|watch] [exit=here|best]`.
    Collection and values may contain spaces: `Plush Pepe max=900 model=Gucci Bag`.
    """
    text = (text or "").strip()
    if not text:
        raise RuleParseError(f"usage: /addrule {ADDRULE_FORMAT}")
    chunks = _SPLIT_RE.split(text)
    collection = chunks[0].strip().strip("\"'")
    if not collection or "=" in collection.split()[0]:
        raise RuleParseError("collection name is required before key=value options")
    opts: dict[str, str] = {}
    for chunk in chunks[1:]:
        key, _, value = chunk.partition("=")
        key = key.strip().lower()
        value = value.strip().strip("\"'")
        if not value:
            raise RuleParseError(f"{key}= is empty")
        opts[key] = value
    if "max" not in opts:
        raise RuleParseError("max=<ton> is required")
    max_price = _num(opts["max"], "max")
    if not math.isfinite(max_price) or max_price <= 0:
        raise RuleParseError("max= must be > 0")
    discount = _num(opts["discount"], "discount") if "discount" in opts else 0.0
    if discount >= 100:
        raise RuleParseError("discount= must be below 100")
    return BuyRule(
        collection=collection,
        max_price_ton=max_price,
        model=opts.get("model"),
        backdrop=opts.get("backdrop"),
        symbol=opts.get("symbol"),
        min_discount_pct=discount,
        min_profit_ton=_num(opts["profit"], "profit") if "profit" in opts else 0.0,
        min_profit_pct=_num(opts["profit%"], "profit%") if "profit%" in opts else 0.0,
        min_confidence=_choice("conf", opts.get("conf", "med"), CONFIDENCE),
        allow_locked_days=_num(opts["locked"], "locked") if "locked" in opts else 0.0,
        mode=_choice("mode", opts.get("mode", "auto"), MODES),
        exit=_choice("exit", opts.get("exit", "here"), EXITS),
    )


STATE_UNSET = ""  # a rules.json entry without "state"; resolved by Storage.replace_rules_from_source


# rules.json keys: each long name and its /addrule short name mean the same thing.
_FILE_ALIASES: dict[str, tuple[str, str]] = {
    "max_price_ton": ("max_price_ton", "max"),
    "min_discount_pct": ("min_discount_pct", "discount"),
    "min_profit_ton": ("min_profit_ton", "profit"),
    "min_profit_pct": ("min_profit_pct", "profit%"),
    "min_confidence": ("min_confidence", "conf"),
    "allow_locked_days": ("allow_locked_days", "locked"),
}
_FILE_KEYS = frozenset({"collection", "model", "backdrop", "symbol", "mode", "exit", "state", "enabled"}
                       | {k for pair in _FILE_ALIASES.values() for k in pair})
_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def rule_from_dict(d: dict, source: str = "file") -> BuyRule:
    """A rules.json entry. Accepts the long keys (`max_price_ton`) and every /addrule short key (`max`,
    `discount`, `profit`, `profit%`, `conf`, `locked`). Fails loudly instead of loosening a rule: an unknown
    key (a typo would otherwise drop a filter), both spellings of one key, a negative or non-numeric limit
    and an `enabled` that is not a clear true/false are all errors. Keys starting with `_` are comments."""
    if not isinstance(d, dict):
        raise RuleParseError(f"rule must be an object, got {type(d).__name__}")
    collection = d.get("collection")
    where = f"rule for {collection!r}" if collection else f"rule {d}"
    unknown = sorted(str(k) for k in d if str(k) not in _FILE_KEYS and not str(k).startswith("_"))
    if unknown:
        raise RuleParseError(f"{where}: unknown key(s) {', '.join(unknown)}; known keys: "
                             f"{', '.join(sorted(_FILE_KEYS))}")

    def get(name: str) -> Any:
        given = [k for k in _FILE_ALIASES.get(name, (name,)) if d.get(k) is not None]
        if len(given) > 1:
            raise RuleParseError(f"{where}: give either {given[0]} or {given[1]}, not both")
        return d[given[0]] if given else None

    def num(name: str, default: float = 0.0) -> float:
        value = get(name)
        if value is None or value == "":
            return default
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise RuleParseError(f"{where}: {name} must be a number (got {value!r})") from None
        if not math.isfinite(n) or n < 0:
            raise RuleParseError(f"{where}: {name} must be a number >= 0 (got {value!r})")
        return n

    max_price = get("max_price_ton")
    if not collection or max_price is None:
        raise RuleParseError(f"rule needs 'collection' and 'max_price_ton': {d}")
    max_price = num("max_price_ton")
    if max_price <= 0:  # same rule as /addrule; "enabled": false switches a rule off
        raise RuleParseError(f"{where}: max_price_ton must be > 0 (got {max_price:g}); "
                             'use "enabled": false to switch a rule off')
    discount = num("min_discount_pct")
    if discount >= 100:
        raise RuleParseError(f"{where}: min_discount_pct must be below 100 (got {discount:g})")
    enabled = d.get("enabled", True)
    if isinstance(enabled, str) and enabled.strip().lower() in _TRUE + _FALSE:
        enabled = enabled.strip().lower() in _TRUE
    elif isinstance(enabled, (int, float)) and not isinstance(enabled, bool) and enabled in (0, 1):
        enabled = bool(enabled)
    if not isinstance(enabled, bool):
        raise RuleParseError(f"{where}: enabled must be true or false (got {enabled!r})")
    return BuyRule(
        collection=str(collection),
        max_price_ton=max_price,
        model=d.get("model") or None,
        backdrop=d.get("backdrop") or None,
        symbol=d.get("symbol") or None,
        min_discount_pct=discount,
        enabled=enabled,
        source=source,
        min_profit_ton=num("min_profit_ton"),
        min_profit_pct=num("min_profit_pct"),
        min_confidence=_choice("min_confidence", str(get("min_confidence") or "med"), CONFIDENCE),
        allow_locked_days=num("allow_locked_days"),
        mode=_choice("mode", str(d.get("mode") or "auto"), MODES),
        exit=_choice("exit", str(d.get("exit") or "here"), EXITS),
        # No "state" key: STATE_UNSET. The loader then applies SHADOW_NEW_RULES to new rules and keeps the
        # stored state (e.g. a rule promoted with [Go live]) of existing ones.
        state=_choice("state", str(d["state"]), STATES) if d.get("state") else STATE_UNSET,
    )


def file_rule_keys(rules: list[BuyRule]) -> list[str]:
    """Stable identity of each rules.json entry: its filter key, plus '#2', '#3'... for a repeated filter.
    Changing max, discount, mode etc. keeps the rule's id; changing collection/model/backdrop/symbol
    makes a new rule."""
    seen: dict[str, int] = {}
    keys = []
    for rule in rules:
        base = rule.filter_key_str()
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def load_rules_file(path: str | Path) -> list[BuyRule]:
    """Load rules from an optional JSON file: a list of rule objects (or {"rules": [...]})."""
    p = Path(path)
    if not p.is_file():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise RuleParseError(f"{p}: expected a JSON list of rules")
    return [rule_from_dict(item) for item in data]


def rule_fields(rule: BuyRule) -> dict[str, Any]:
    return asdict(rule)
