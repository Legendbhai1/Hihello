"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    bot_token: str = Field(alias="BOT_TOKEN")
    admin_ids: list[int] = Field(default_factory=list, alias="ADMIN_IDS")
    bot_mode: Literal["polling", "webhook"] = Field("polling", alias="BOT_MODE")

    # Public HTTP server
    public_base_url: str = Field("http://localhost:8080", alias="PUBLIC_BASE_URL")
    webhook_secret: str = Field("", alias="WEBHOOK_SECRET")
    http_host: str = Field("0.0.0.0", alias="HTTP_HOST")
    http_port: int = Field(8080, alias="HTTP_PORT")
    # Injected by hosting platforms (Render, Heroku, Fly). Takes precedence over
    # HTTP_PORT so the health check and provider webhooks stay reachable.
    platform_port: int | None = Field(None, alias="PORT")

    # Storage
    database_url: str = Field("sqlite+aiosqlite:///./data/shop.db", alias="DATABASE_URL")

    # Telegram Stars
    stars_enabled: bool = Field(True, alias="STARS_ENABLED")

    # Crypto Pay (@CryptoBot)
    cryptopay_enabled: bool = Field(False, alias="CRYPTOPAY_ENABLED")
    cryptopay_token: str = Field("", alias="CRYPTOPAY_TOKEN")
    cryptopay_base_url: str = Field("https://pay.crypt.bot", alias="CRYPTOPAY_BASE_URL")
    cryptopay_webhook_secret: str = Field("", alias="CRYPTOPAY_WEBHOOK_SECRET")

    # OxaPay
    oxapay_enabled: bool = Field(False, alias="OXAPAY_ENABLED")
    oxapay_api_key: str = Field("", alias="OXAPAY_API_KEY")
    oxapay_base_url: str = Field("https://api.oxapay.com", alias="OXAPAY_BASE_URL")
    oxapay_sandbox: bool = Field(False, alias="OXAPAY_SANDBOX")

    # Reconciliation / order lifecycle
    reconcile_interval_seconds: int = Field(60, alias="RECONCILE_INTERVAL_SECONDS")
    reconcile_min_age_seconds: int = Field(120, alias="RECONCILE_MIN_AGE_SECONDS")
    order_expiry_minutes: int = Field(120, alias="ORDER_EXPIRY_MINUTES")

    log_level: str = Field("INFO", alias="LOG_LEVEL")

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        # Accept "1,2,3", "1; 2", a single "42", or a JSON list.
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return value  # let pydantic parse the JSON list
            return [int(p) for p in text.replace(";", ",").split(",") if p.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        """Make a hosted Postgres URL usable by SQLAlchemy's async engine.

        Render/Heroku hand out ``postgres://`` and ``postgresql://`` URLs. Both
        select psycopg2, which is synchronous, so an async engine would fail at
        connect time. Point them at asyncpg instead.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix) :]
        return value

    @property
    def bind_port(self) -> int:
        """The port the callback server should listen on.

        Hosting platforms inject ``$PORT`` and route traffic only to it, so it
        wins over the local default.
        """
        return self.platform_port or self.http_port

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def uses_ephemeral_disk(self) -> bool:
        """True when the database lives on the container's own filesystem.

        Hosts like Render discard that filesystem on every deploy and restart,
        which would silently wipe orders and stock. SQLite is only safe here
        for local development.
        """
        return self.database_url.startswith("sqlite")

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/telegram/webhook/{self.webhook_secret}"

    @property
    def cryptopay_webhook_url(self) -> str:
        base = f"{self.public_base_url.rstrip('/')}/webhooks/cryptopay"
        if self.cryptopay_webhook_secret:
            return f"{base}/{self.cryptopay_webhook_secret}"
        return base

    @property
    def oxapay_callback_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/webhooks/oxapay"

    def enabled_providers(self) -> list[str]:
        providers: list[str] = []
        if self.stars_enabled:
            providers.append("stars")
        if self.cryptopay_enabled and self.cryptopay_token:
            providers.append("cryptopay")
        if self.oxapay_enabled and self.oxapay_api_key:
            providers.append("oxapay")
        return providers

    def validate_runtime(self) -> None:
        """Fail fast on configurations that cannot work at runtime."""
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN is empty")
        if not self.enabled_providers():
            problems.append("no payment provider enabled (Stars, Crypto Pay or OxaPay)")
        if self.bot_mode == "webhook":
            if not self.public_base_url.startswith("https://"):
                problems.append("BOT_MODE=webhook requires an https PUBLIC_BASE_URL")
            if not self.webhook_secret:
                problems.append("BOT_MODE=webhook requires WEBHOOK_SECRET")
        if self.cryptopay_enabled and not self.cryptopay_token:
            problems.append("CRYPTOPAY_ENABLED=true but CRYPTOPAY_TOKEN is empty")
        if self.oxapay_enabled and not self.oxapay_api_key:
            problems.append("OXAPAY_ENABLED=true but OXAPAY_API_KEY is empty")
        if problems:
            raise RuntimeError("Invalid configuration: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
