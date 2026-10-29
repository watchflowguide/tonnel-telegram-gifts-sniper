"""Telegram Mini App initData for Tonnel via a Telethon user session.

Telethon is imported lazily so that `mock` and tests never need it.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
from pathlib import Path

from .config import ConfigError, Settings, mask

log = logging.getLogger(__name__)


def extract_init_data(webview_url: str) -> str:
    """Take `tgWebAppData` from the web-view URL fragment and unquote it once.

    The fragment looks like `#tgWebAppData=<urlencoded>&tgWebAppVersion=...`.
    """
    parsed = urllib.parse.urlparse(webview_url)
    for source in (parsed.fragment, parsed.query):
        for part in source.split("&"):
            key, _, value = part.partition("=")
            if key == "tgWebAppData" and value:
                return urllib.parse.unquote(value)
    raise ValueError("tgWebAppData not found in web view URL")


def _new_client(settings: Settings):
    if not settings.has_tg_credentials:
        missing = "TG_API_ID" if not settings.TG_API_ID else "TG_API_HASH"
        raise ConfigError(
            f"{missing} is not set in .env (get it at https://my.telegram.org), "
            "or set TONNEL_INIT_DATA manually"
        )
    from telethon import TelegramClient  # lazy

    settings.ensure_data_dir()
    return TelegramClient(settings.TG_SESSION, settings.TG_API_ID, settings.TG_API_HASH)


async def login(settings: Settings) -> None:
    """Interactive first login (phone + code [+ 2FA password]). Creates the session file."""
    client = _new_client(settings)
    await client.start()  # prompts in the terminal
    me = await client.get_me()
    log.info("Logged in as id=%s username=%s", me.id, me.username)
    await client.disconnect()


class TelegramSession:
    """One shared, lazily connected Telethon client (initData, Telegram valuation, wizard link lookup).

    Sharing avoids two clients locking the same .session SQLite file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    @property
    def configured(self) -> bool:
        return self.settings.has_tg_credentials

    async def client(self):
        if not Path(self.settings.TG_SESSION + ".session").is_file():
            raise ConfigError(
                f"Telegram session '{self.settings.TG_SESSION}.session' not found: "
                "run `python -m tonnel_autobuy login` first"
            )
        if self._client is None:
            self._client = _new_client(self.settings)
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise ConfigError(
                f"Telegram session '{self.settings.TG_SESSION}.session' is not logged in: "
                "run `python -m tonnel_autobuy login` first"
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


async def probe_session(settings: Settings, session: "TelegramSession | None" = None,
                        timeout: float = 5.0) -> tuple[bool, bool | None]:
    """(authorized, has_password) for /doctor. Never logs or returns the phone number."""
    from telethon.tl.functions.account import GetPasswordRequest

    own = session is None
    session = session or TelegramSession(settings)
    try:
        try:
            client = await asyncio.wait_for(session.client(), timeout)
        except ConfigError:
            return False, None
        pwd = await asyncio.wait_for(client(GetPasswordRequest()), timeout)
        return True, bool(getattr(pwd, "has_password", False))
    finally:
        if own:
            await session.close()


async def lookup_gift(session: "TelegramSession", slug: str, timeout: float = 5.0) -> dict | None:
    """payments.getUniqueStarGift(slug) -> {collection, number, model, backdrop, symbol} for the wizard."""
    from telethon.tl.functions.payments import GetUniqueStarGiftRequest

    client = await session.client()
    res = await asyncio.wait_for(client(GetUniqueStarGiftRequest(slug=slug)), timeout)
    gift = getattr(res, "gift", None)
    if gift is None:
        return None
    out = {"collection": getattr(gift, "title", None), "number": getattr(gift, "num", None)}
    for attr in getattr(gift, "attributes", []) or []:
        kind = type(attr).__name__  # StarGiftAttributeModel / Backdrop / Pattern
        name = getattr(attr, "name", None)
        if kind.endswith("Model"):
            out["model"] = name
        elif kind.endswith("Backdrop"):
            out["backdrop"] = name
        elif kind.endswith("Pattern"):
            out["symbol"] = name
    return out


async def request_init_data(settings: Settings, session: "TelegramSession | None" = None) -> str:
    """Open the Tonnel Mini App as the user and return fresh initData."""
    if session is not None:
        return await _request_init_data(settings, await session.client())
    if not Path(settings.TG_SESSION + ".session").is_file():
        raise ConfigError(
            f"Telegram session '{settings.TG_SESSION}.session' not found: "
            "run `python -m tonnel_autobuy login` first"
        )
    client = _new_client(settings)
    await client.connect()
    try:
        return await _request_init_data(settings, client)
    finally:
        await client.disconnect()


async def _request_init_data(settings: Settings, client) -> str:
    from telethon.tl import functions, types  # lazy

    if not await client.is_user_authorized():
        raise ConfigError(
            f"Telegram session '{settings.TG_SESSION}.session' is not logged in: "
            "run `python -m tonnel_autobuy login` first"
        )
    bot = await client.get_input_entity(settings.TONNEL_BOT_USERNAME)
    try:
        # TODO(verify): Mini App short name of @Tonnel_Network_bot is assumed to be "gifts".
        res = await client(
            functions.messages.RequestAppWebViewRequest(
                peer=bot,
                app=types.InputBotAppShortName(
                    bot_id=bot, short_name=settings.TONNEL_APP_SHORT_NAME
                ),
                platform="android",
                write_allowed=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - fall back to the menu-button web view
        log.warning("RequestAppWebView failed (%s), falling back to RequestWebView", exc)
        res = await client(
            functions.messages.RequestWebViewRequest(
                peer=bot,
                bot=bot,
                platform="android",
                from_bot_menu=False,
                url=settings.TONNEL_WEBAPP_URL,
            )
        )
    init_data = extract_init_data(res.url)
    log.info("Got Tonnel initData %s", mask(init_data, 24))
    return init_data


class InitDataProvider:
    """Caches initData; refreshes after TTL or when `invalidate()` is called (401/403)."""

    def __init__(self, settings: Settings, session: TelegramSession | None = None):
        self._s = settings
        self.session = session
        self._value: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def age(self) -> float | None:
        """Seconds since initData was fetched (None before the first fetch or with TONNEL_INIT_DATA)."""
        return None if self._value is None else time.monotonic() - self._fetched_at

    @property
    def configured(self) -> bool:
        return self._s.has_auth_source

    @property
    def static(self) -> bool:
        return bool(self._s.TONNEL_INIT_DATA)

    def invalidate(self) -> None:
        if self.static:
            log.warning("Tonnel rejected TONNEL_INIT_DATA; it cannot be refreshed - update .env")
        self._value = None

    async def get(self, force: bool = False) -> str:
        if self.static:
            return self._s.TONNEL_INIT_DATA
        async with self._lock:
            fresh = time.monotonic() - self._fetched_at < self._s.INIT_DATA_TTL
            if self._value and fresh and not force:
                return self._value
            self._value = await request_init_data(self._s, self.session)
            self._fetched_at = time.monotonic()
            return self._value
