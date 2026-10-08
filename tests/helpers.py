"""Shared offline test helpers (no network, no credentials)."""

from __future__ import annotations

from dataclasses import replace

from tonnel_autobuy.client import DEMO_RULES, MockClient
from tonnel_autobuy.config import Settings
from tonnel_autobuy.engine import Engine
from tonnel_autobuy.models import Listing
from tonnel_autobuy.notifier import MemoryNotifier
from tonnel_autobuy.storage import Storage

ADMIN = 42
NOW = 1_800_000_000.0

# Demo fixture listing ids (gift_id = collection base + number), see client.demo_listings.
LS = lambda n: str(810000 + n)  # noqa: E731 - Lunar Snake
DC = lambda n: str(820000 + n)  # noqa: E731 - Desk Calendar
JB = lambda n: str(830000 + n)  # noqa: E731 - Jelly Bunny
SA = lambda n: str(840000 + n)  # noqa: E731 - Spy Agaric
HC = lambda n: str(850000 + n)  # noqa: E731 - Homemade Cake


class Clock:
    def __init__(self, t: float = NOW) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def settings(**overrides) -> Settings:
    base = dict(_env_file=None, DRY_RUN=True, MAX_PRICE_TON=10, DAILY_BUDGET_TON=50, MAX_BUYS_PER_HOUR=3,
                POLL_INTERVAL=0.01, ADMIN_IDS=str(ADMIN))
    base.update(overrides)
    return Settings(**base)


def L(lid: str, price: float, collection: str = "Lunar Snake", model: str | None = "Albino", number: int | None = 1,
      **raw) -> Listing:
    """A listing as client.parse_listings builds it: the price unit came from the `asset` field."""
    raw.setdefault("asset", "TON")
    return Listing(id=lid, collection=collection, model=model, backdrop=None, symbol=None, number=number,
                   price_ton=price, listed_at=None, raw=raw)


async def demo_engine(s: Settings | None = None, *, rules=None, force_dry: bool = True, clock: Clock | None = None,
                      client: MockClient | None = None, cycle: bool = True, **kw):
    """Engine on the F1-F8 demo book. The client is already on cycle 1 (call client.next_cycle() for F4/F6)."""
    s = s or settings()
    clock = clock or Clock()
    storage = await Storage(":memory:").open()
    for rule in (DEMO_RULES if rules is None else rules):
        await storage.add_rule(replace(rule, id=None))
    client = client or MockClient("demo", now=clock())
    if cycle and client.cycle == 0:
        client.next_cycle()
    notifier = MemoryNotifier()
    engine = Engine(s, client, storage, notifier, force_dry=force_dry, clock=clock, **kw)
    return engine, storage, client, notifier, clock
