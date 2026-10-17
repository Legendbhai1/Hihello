"""Shared test fixtures."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import Product
from app.services import catalog


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def product(session: AsyncSession) -> Product:
    return await catalog.upsert_product(
        session,
        sku="test-sku",
        title="Test Product",
        description="A thing",
        price_stars=50,
        price_usd_cents=499,
    )


@pytest.fixture(scope="session")
def event_loop():  # pragma: no cover - pytest-asyncio compatibility
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
