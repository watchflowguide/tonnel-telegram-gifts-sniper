import asyncio

import pytest

from tonnel_autobuy.client import MockClient, mock_rules
from tonnel_autobuy.config import Settings
from tonnel_autobuy.engine import Engine, backoff_delay, check_limits
from tonnel_autobuy.models import Listing
from tonnel_autobuy.notifier import Notifier
from tonnel_autobuy.storage import Storage
from tonnel_autobuy.tg_auth import extract_init_data


def L(id, price):
    return Listing(id=str(id), collection="Toy Bear", model=None, backdrop=None, symbol=None,
                   number=None, price_ton=price, listed_at=None)


def test_check_limits():
    kw = dict(max_price=20, daily_budget=50, spent_today=0, max_per_hour=3, buys_last_hour=0)
    assert check_limits(10, **kw) is None
    assert "MAX_PRICE_TON" in check_limits(21, **kw)
    assert "daily budget" in check_limits(10, **{**kw, "spent_today": 45})
    assert "hourly" in check_limits(10, **{**kw, "buys_last_hour": 3})


def test_backoff_delay():
    assert backoff_delay(0, 2, 60, jitter=False) == 2
    assert backoff_delay(1, 2, 60, jitter=False) == 4
    assert backoff_delay(3, 2, 60, jitter=False) == 16
    assert backoff_delay(20, 2, 60, jitter=False) == 60
    assert backoff_delay(5, 2, 60) <= 60


def test_extract_init_data():
    url = ("https://market.tonnel.network/#tgWebAppData=query_id%3DAAH%26user%3D%257B%2522id"
           "%2522%253A1%257D%26auth_date%3D1700000000%26hash%3Dabc&tgWebAppVersion=8.0"
           "&tgWebAppPlatform=android")
    assert extract_init_data(url) == (
        "query_id=AAH&user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc")
    with pytest.raises(ValueError):
        extract_init_data("https://example.com/#foo=bar")


def test_settings_admin_ids():
    s = Settings(_env_file=None, ADMIN_IDS="1, 2;3")
    assert s.admin_ids == [1, 2, 3]
    assert Settings(_env_file=None).DRY_RUN is True


class Capture(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def send(self, text, **kw):  # deal cards also pass html/keyboard/silent/plain
        self.sent.append(kw.get("plain") or text)
        return []


def test_mock_pipeline_dry_buy_and_dedupe(tmp_path):
    async def run():
        settings = Settings(_env_file=None, DRY_RUN=False)  # force_dry must still win
        storage = await Storage(str(tmp_path / "t.sqlite3")).open()
        for r in mock_rules():
            await storage.add_rule(r)
        client, notes = MockClient(), Capture()
        engine = Engine(settings, client, storage, notes, force_dry=True)
        client.next_cycle()
        r1 = await engine.run_cycle()
        client.next_cycle()
        r2 = await engine.run_cycle()
        buys = await storage.recent_purchases()
        await storage.close()
        return r1, r2, buys, notes.sent, client.bought

    r1, r2, buys, sent, bought = asyncio.run(run())
    assert len(r1.matched) == 1 and r1.matched[0].listing.id == "900001"
    assert r1.rejected  # Plush Pepe etc. rejected by rules
    assert r2.new == 1 and not r2.matched  # only the fresh listing is evaluated
    assert len(buys) == 1 and buys[0]["dry"] == 1 and buys[0]["status"] == "dry"
    # one card per practice buy (no separate "DRY: would buy" message any more)
    assert sum(s.startswith("DEAL") and "PRACTICE: would buy" in s for s in sent) == 1
    assert bought == []  # dry never calls the client


def test_pause_and_limits(tmp_path):
    async def run():
        settings = Settings(_env_file=None, MAX_BUYS_PER_HOUR=0)
        storage = await Storage(str(tmp_path / "t.sqlite3")).open()
        for r in mock_rules():
            await storage.add_rule(r)
        client, notes = MockClient(), Capture()
        engine = Engine(settings, client, storage, notes)
        await storage.set_paused(True)
        paused = await engine.run_cycle()
        await storage.set_paused(False)
        client.next_cycle()
        rep = await engine.run_cycle()
        buys = await storage.recent_purchases()
        await storage.close()
        return paused, rep, buys, notes.sent

    paused, rep, buys, sent = asyncio.run(run())
    assert paused.paused
    assert len(rep.matched) == 1 and buys == []
    assert any(s.startswith("SKIP (hourly limit") for s in sent)


def test_bot_dispatcher_builds_offline(tmp_path):
    from tonnel_autobuy.bot import BotContext, build_dispatcher, create_bot
    from tonnel_autobuy.config import ConfigError

    async def run():
        storage = await Storage(str(tmp_path / "b.sqlite3")).open()
        try:
            s = Settings(_env_file=None, ADMIN_IDS="42", BOT_TOKEN="123456:" + "A" * 35)
            client = MockClient()
            engine = Engine(s, client, storage, Notifier())
            dp = build_dispatcher(BotContext(s, storage, engine, client))
            bot = create_bot(s)
            await bot.session.close()
            names = {h.callback.__name__ for r in dp.sub_routers for h in r.message.handlers}
            with pytest.raises(ConfigError):
                build_dispatcher(BotContext(Settings(_env_file=None), storage, engine, client))
            return names
        finally:
            await storage.close()

    names = asyncio.run(run())
    for cmd in ("start", "status", "pause", "resume", "rules", "addrule", "delrule", "dry",
                "balance", "buys", "floor"):
        assert f"cmd_{cmd}" in names


def test_live_path_calls_buy_with_mock_client(tmp_path):
    async def run():
        settings = Settings(_env_file=None, DRY_RUN=False)
        storage = await Storage(str(tmp_path / "l.sqlite3")).open()
        for r in mock_rules():
            await storage.add_rule(r)
        client, notes = MockClient(), Capture()
        engine = Engine(settings, client, storage, notes)
        assert await engine.is_dry()  # DRY_RUN=false alone is not live: the gate must be armed
        assert await engine.arm_live() is None  # /dry off + confirm
        client.next_cycle()
        await engine.run_cycle()
        await asyncio.sleep(0)  # let the background MATCH alert run
        await storage.set_dry(True)  # /dry on
        dry_now = await engine.is_dry()
        buys = await storage.recent_purchases()
        await storage.close()
        return client.bought, buys, notes.sent, dry_now

    bought, buys, sent, dry_now = asyncio.run(run())
    assert bought == ["900001"]
    assert buys[0]["dry"] == 0 and buys[0]["status"] == "ok"
    assert any(s.startswith("BOUGHT:") for s in sent)
    assert any(s.startswith("DEAL") and "BOUGHT in" in s for s in sent)  # the deal card replaced "MATCH:"
    assert dry_now is True
