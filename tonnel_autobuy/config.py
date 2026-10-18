"""Application settings (pydantic-settings, reads `.env`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(Exception):
    """Missing or invalid configuration. Message names the `.env` key."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        env_ignore_empty=True,  # `TG_API_ID=` in .env means "not set"
    )

    # --- Telegram admin bot ---
    BOT_TOKEN: str = ""
    ADMIN_IDS: str = ""  # comma-separated ints

    # --- Telegram user account (only to obtain Mini App initData) ---
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION: str = "data/tonnel"  # Telethon adds ".session"
    TONNEL_BOT_USERNAME: str = "Tonnel_Network_bot"
    TONNEL_APP_SHORT_NAME: str = "gifts"
    TONNEL_WEBAPP_URL: str = "https://market.tonnel.network/"
    # Optional manual override: initData copied from the web app
    # (localStorage key `web-initData` on market.tonnel.network). Not refreshable.
    TONNEL_INIT_DATA: str = ""
    INIT_DATA_TTL: int = 1800  # seconds before initData is re-requested

    # --- Tonnel API ---
    TONNEL_ASSET: str = "TON"
    # Deprecated alias of BUY_FEE_PCT: used only when BUY_FEE_PCT is not set (always mirrors it after load).
    FEE_PCT: float | None = None
    PAGE_LIMIT: int = 30  # max listings per request (API max is 30)
    PROXY: str = ""  # e.g. http://user:pass@host:port
    HTTP_TIMEOUT: float = 10.0
    # TODO(verify): what Tonnel's export_at means. Until verified, a future export_at counts as a lock.
    EXPORT_AT_LOCKS: bool = True

    # --- Engine / limits ---
    DRY_RUN: bool = True
    MAX_PRICE_TON: float = 20.0  # hard cap per item (cost incl. fee)
    DAILY_BUDGET_TON: float = 50.0  # per UTC day (cost incl. fee)
    MAX_BUYS_PER_HOUR: int = 3
    POLL_INTERVAL: float = 2.0  # seconds between cycles
    MAX_BACKOFF: float = 60.0  # seconds, cap for exponential backoff on 429/errors
    FLOOR_CACHE_TTL: float = 60.0  # seconds to cache API floors (filterStats); also the snapshot cadence

    # --- Live gate, practice ledger and breakers ---
    LIVE_ARM_HOURS: float = 24.0  # LIVE lasts this long once /dry off is confirmed, then back to practice
    PAPER_HORIZON_DAYS: int = 7
    MARK_MAX_AGE_SEC: int = 1800
    MARK_MAX_QUERIES: int = 10
    MARK_INTERVAL_SEC: int = 3600
    GATE_MIN_TRADES: int = 5
    GATE_DAYS: int = 7
    CRASH_PCT: float = 20.0
    CRASH_PAUSE_MIN: int = 60
    LOSS_STREAK_N: int = 3
    LOSS_STREAK_PCT: float = 10.0
    BALANCE_GUARD: bool = True
    BALANCE_TOLERANCE_TON: float = 0.5
    AUTO_PAUSE_ERRORS: int = 5  # consecutive failed live buys -> pause
    STALE_DATA_SEC: int = 300  # warn once when no cycle succeeded for this long
    CONFIRM_ABOVE_TON: float = 0.0  # buys costing more always ask first; 0 = 0.5 x MAX_PRICE_TON
    HOT_WALLET_WARN_X: float = 3.0
    DIGEST_HOUR: int = -1  # local hour for the daily digest, -1 = off
    TZ_OFFSET_HOURS: int = 0  # your offset from UTC for /why times, the digest hour and "Ignore today"

    # --- Fees (net math). TODO(verify): Tonnel fee schedule; defaults are assumptions ---
    # tonnelmp docs: listed prices are "without the 10% fee", so the buyer pays price x 1.10.
    BUY_FEE_PCT: float | None = None  # None -> FEE_PCT if set, else 10
    SELL_FEE_PCT: float = 0.0  # TODO(verify)
    WITHDRAW_FEE_TON: float = 0.0
    FEES_VERIFIED: bool = False

    # --- Robust floor and trap filter ---
    CLUSTER_K: int = 2
    CLUSTER_BAND_PCT: float = 10.0
    MIN_COMPARABLES_COLLECTION: int = 5
    MIN_COMPARABLES_ATTR: int = 3
    FLOOR_JUMP_PCT: float = 25.0
    TOO_GOOD_PCT: float = 25.0
    OBS_TTL_DAYS: int = 14
    SNAPSHOT_TTL_DAYS: int = 14
    DECISIONS_TTL_DAYS: int = 7

    # --- Deal cards ---
    CONFIRM_TTL_SEC: int = 90
    NEAR_MISS_PCT: float = 5.0
    NEAR_MISS_MAX_PER_HOUR: int = 3
    PREVIEW_HOURS: int = 24
    DISPLAY_CURRENCY: str = "usd"  # usd | none

    # --- Second opinion: Telegram valuation and other markets ---
    PEERS_TIMEOUT_SEC: float = 3.0
    TG_VALUE_ENABLED: bool = True
    TG_VALUE_TTL: int = 600
    TG_VALUE_MAX_PER_MIN: int = 10
    TG_VALUE_GUARD: bool = True
    TON_RATE_URL: str = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
    TON_RATE_TTL: int = 600
    PEER_FLOOR_FILES: str = ""  # comma-separated floors_<market>.json paths written by other bots you run
    PEER_MAX_AGE_SEC: int = 600
    PEER_PORTALS_PUBLIC: bool = False
    PORTALS_PUBLIC_URL: str = "https://portal-market.com/api/collections?limit=500"
    PORTALS_PUBLIC_TTL: int = 300
    # Telegram's own resale market (payments.getResaleStarGifts on the Telethon session), opt-in
    TG_MARKET: bool = False
    TG_MARKET_LIMIT: int = 50
    TG_MARKET_MIN_INTERVAL_SEC: float = 5.0
    STARS_USD_RATE: float = 0.013  # USD per Telegram Star, to convert Stars resale prices
    CHEAPER_ELSEWHERE_PCT: float = 3.0

    # --- Shadow rules, events, digest, optional LLM ---
    SHADOW_NEW_RULES: bool = True
    SHADOW_MIN_HOURS: float = 24.0
    SHADOW_MIN_MATCHES: int = 5
    SHADOW_MAX_HOURS: float = 168.0
    SNIPED_WINDOW_SEC: int = 120
    EVENTS_FILE: str = "data/events.jsonl"
    EVENTS_MAX_MB: float = 20.0
    PEER_EVENT_FILES: str = ""
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"

    # --- Storage / misc ---
    DB_PATH: str = "data/tonnel_autobuy.sqlite3"
    DATA_DIR: str = "data"
    RULES_FILE: str = "rules.json"
    LOG_LEVEL: str = "INFO"

    @model_validator(mode="after")
    def _fee_alias(self) -> "Settings":
        """BUY_FEE_PCT wins; the deprecated FEE_PCT is used only when BUY_FEE_PCT is not set."""
        if self.BUY_FEE_PCT is None:
            self.BUY_FEE_PCT = self.FEE_PCT if self.FEE_PCT is not None else 10.0
        self.FEE_PCT = self.BUY_FEE_PCT
        return self

    @property
    def admin_ids(self) -> list[int]:
        out: list[int] = []
        for part in self.ADMIN_IDS.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError as exc:
                raise ConfigError(f"ADMIN_IDS: '{part}' is not an integer user id") from exc
        return out

    @property
    def fee_mult(self) -> float:
        return 1.0 + float(self.BUY_FEE_PCT or 0) / 100.0

    @property
    def floors_ttl(self) -> float:
        """FLOORS_TTL from the brief: floors refresh and snapshot cadence."""
        return self.FLOOR_CACHE_TTL

    @property
    def confirm_above_ton(self) -> float:
        return self.CONFIRM_ABOVE_TON if self.CONFIRM_ABOVE_TON > 0 else 0.5 * self.MAX_PRICE_TON

    @staticmethod
    def _paths(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def peer_floor_files(self) -> list[str]:
        return self._paths(self.PEER_FLOOR_FILES)

    @property
    def peer_event_files(self) -> list[str]:
        return self._paths(self.PEER_EVENT_FILES)

    @property
    def session_file(self) -> Path:
        return Path(self.TG_SESSION + ".session")

    @property
    def has_tg_credentials(self) -> bool:
        return bool(self.TG_API_ID and self.TG_API_HASH)

    @property
    def has_auth_source(self) -> bool:
        return bool(self.TONNEL_INIT_DATA) or self.has_tg_credentials

    def ensure_data_dir(self) -> None:
        Path(self.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(self.TG_SESSION).parent.mkdir(parents=True, exist_ok=True)
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        keys = ", ".join(str(e["loc"][0]) for e in exc.errors() if e.get("loc"))
        raise ConfigError(f"Invalid value in .env for: {keys}") from exc


def mask(secret: str | None, keep: int = 8) -> str:
    """Truncate a secret for logs."""
    if not secret:
        return "<empty>"
    if len(secret) <= keep:
        return "***"
    return f"{secret[:keep]}...({len(secret)} chars)"
