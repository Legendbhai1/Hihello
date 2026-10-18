"""Hosting-platform configuration: Render-style database URLs and PORT.

Render hands out ``postgres://`` connection strings and injects ``PORT``. Both
need translating before the async engine can use them, and getting either wrong
fails in ways that only show up in production.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _settings(**overrides) -> Settings:
    values = {
        "BOT_TOKEN": "123456:TEST-TOKEN",
        "ADMIN_IDS": "42",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PUBLIC_BASE_URL": "https://shop.example.com",
        "STARS_ENABLED": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "given, expected",
    [
        # Render/Heroku style: bare scheme, sync driver implied.
        ("postgres://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        ("postgresql://u:p@h:5432/db", "postgresql+asyncpg://u:p@h:5432/db"),
        # Already async: must be left alone, not double-rewritten.
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        # SQLite is untouched.
        ("sqlite+aiosqlite:///./data/shop.db", "sqlite+aiosqlite:///./data/shop.db"),
    ],
)
def test_database_url_is_normalized_for_async_driver(given: str, expected: str) -> None:
    assert _settings(DATABASE_URL=given).database_url == expected


def test_postgres_url_survives_query_parameters() -> None:
    given = "postgres://u:p@h/db?sslmode=require"
    url = _settings(DATABASE_URL=given).database_url
    assert url == "postgresql+asyncpg://u:p@h/db?sslmode=require"


def test_is_postgres_and_ephemeral_disk_flags() -> None:
    pg = _settings(DATABASE_URL="postgres://u:p@h/db")
    assert pg.is_postgres is True
    assert pg.uses_ephemeral_disk is False

    lite = _settings(DATABASE_URL="sqlite+aiosqlite:///./data/shop.db")
    assert lite.is_postgres is False
    assert lite.uses_ephemeral_disk is True


def test_platform_port_overrides_http_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "10000")
    s = _settings(HTTP_PORT=8080)
    assert s.platform_port == 10000
    assert s.bind_port == 10000


def test_bind_port_falls_back_to_http_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORT", raising=False)
    s = _settings(HTTP_PORT=8080)
    assert s.platform_port is None
    assert s.bind_port == 8080
