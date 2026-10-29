"""TON -> USD rate (tonapi, keyless, cached). Never blocks a decision: failure means 'no rate'."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[Any]]


def parse_tonapi_rate(data: Any, currency: str = "USD") -> float | None:
    """{"rates": {"TON": {"prices": {"USD": 1.246}}}} -> 1.246 (verified shape, 2026-10-28)."""
    try:
        value = float(data["rates"]["TON"]["prices"][currency.upper()])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


async def get_json(url: str, timeout: float = 3.0) -> Any:
    """Keyless public GET with curl_cffi (already a dependency for the Tonnel API). Raises on HTTP errors."""
    from curl_cffi.requests import AsyncSession  # lazy: mock and tests never need it

    async with AsyncSession(impersonate="chrome", timeout=timeout) as session:
        resp = await session.get(url)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} from {url}")
        return resp.json()


class TonRate:
    """USD per TON. `fixed` pins the rate (mock); otherwise GET TON_RATE_URL every `ttl` seconds."""

    def __init__(self, url: str, ttl: float = 600, *, fixed: float | None = None, fetch: Fetch | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = url
        self.ttl = ttl
        self.fixed = fixed
        self._fetch = fetch or get_json
        self._clock = clock
        self._value: float | None = fixed
        self._at = 0.0
        self.last_error: str | None = None
        self._inflight: asyncio.Task | None = None  # one request at a time; concurrent callers share it

    @property
    def value(self) -> float | None:
        return self._value

    async def usd_per_ton(self) -> float | None:
        if self.fixed is not None:
            return self.fixed
        now = self._clock()
        if self._value is not None and now - self._at < self.ttl:
            return self._value
        if self.last_error and now - self._at < 60:  # do not hammer a failing endpoint
            return self._value
        task = self._inflight
        if task is None or task.done() or task.get_loop() is not asyncio.get_running_loop():
            task = self._inflight = asyncio.get_running_loop().create_task(self._refresh(now), name="ton-rate-fetch")
        return await asyncio.shield(task)  # a caller that gives up (timeout) does not cancel the shared request

    async def _refresh(self, now: float) -> float | None:
        try:
            rate = parse_tonapi_rate(await self._fetch(self.url))
            self.last_error = None if rate else "unexpected response"
            if rate:
                self._value = rate
        except Exception as exc:  # network, JSON, HTTP status
            self.last_error = f"{type(exc).__name__}"
            log.debug("TON rate failed: %s", exc)
        self._at = now
        return self._value

    def status(self) -> str:
        if self.fixed is not None:
            return f"fixed {self.fixed:g} USD/TON"
        if self._value is None:
            return f"n/a ({self.last_error or 'not fetched yet'})"
        return f"{self._value:.3f} USD/TON" + (f" (last refresh failed: {self.last_error})" if self.last_error else "")


def usd(amount_ton: float | None, rate: float | None) -> str:
    if amount_ton is None or not rate:
        return ""
    return f"≈ ${amount_ton * rate:,.2f}"
