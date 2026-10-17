"""End-to-end wiring: dispatcher construction and the callback HTTP server.

Uses aiohttp's test client against the real app, and a real (fake-token) Bot so
signature handling, order lookup and delivery are all exercised for real.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from aiogram import Bot
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Order, OrderStatus, StockItem
from app.providers.registry import build_registry
from app.services import catalog
from app.services.orders import create_order
from app.web.server import WebhookServer

CRYPTOPAY_TOKEN = "cryptopay-token-for-tests"
OXAPAY_KEY = "oxapay-key-for-tests"
USER_ID = 777000111


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        ADMIN_IDS="42",
        BOT_MODE="polling",
        PUBLIC_BASE_URL="https://shop.example.com",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
        STARS_ENABLED=True,
        CRYPTOPAY_ENABLED=True,
        CRYPTOPAY_TOKEN=CRYPTOPAY_TOKEN,
        OXAPAY_ENABLED=True,
        OXAPAY_API_KEY=OXAPAY_KEY,
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def bot() -> Bot:
    # A real Bot with an unreachable token: HTTP calls would fail, which is what we
    # want for callback tests that must not talk to Telegram.
    return Bot(token="123456:TEST-TOKEN")


@pytest.fixture
def registry(settings: Settings, bot: Bot):
    return build_registry(settings, bot)


async def _seed_order(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: str,
    currency: str,
    amount_minor: int,
    sku: str = "e2e-sku",
) -> tuple[str, int]:
    async with session_factory() as session:
        product = await catalog.upsert_product(
            session,
            sku=sku,
            title="E2E Product",
            price_stars=100,
            price_usd_cents=999,
        )
        await catalog.add_stock(session, product.id, ["E2E-KEY-1"])
        order = await create_order(
            session,
            user_id=USER_ID,
            product=product,
            provider=provider,
            unit_amount_minor=amount_minor,
            currency=currency,
            expiry_minutes=30,
        )
        order.provider_invoice_id = "track-123"
        order.status = OrderStatus.PENDING.value
        await session.flush()
        order_id = order.id
        await session.commit()
        return order_id, product.id


async def _client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry,
    bot: Bot,
) -> TestClient:
    server = WebhookServer(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        bot=bot,
        dispatcher=None,
    )
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client


async def test_health_endpoint(settings, session_factory, registry, bot) -> None:
    client = await _client(settings, session_factory, registry, bot)
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        body = await resp.json()
        assert body["status"] == "ok"
        assert set(body["providers"]) == {"stars", "cryptopay", "oxapay"}
    finally:
        await client.close()


async def test_oxapay_callback_delivers_order(
    settings, session_factory, registry, bot
) -> None:
    order_id, _ = await _seed_order(
        session_factory, provider="oxapay", currency="USD", amount_minor=999
    )
    client = await _client(settings, session_factory, registry, bot)
    try:
        body = json.dumps(
            {
                "track_id": "track-123",
                "status": "Paid",
                "type": "invoice",
                "amount": 10,
                "value": 9.99,
                "currency": "POL",
                "order_id": order_id,
            }
        ).encode()
        signature = hmac.new(OXAPAY_KEY.encode(), body, hashlib.sha512).hexdigest()
        resp = await client.post("/webhooks/oxapay", data=body, headers={"HMAC": signature})
        assert resp.status == 200
    finally:
        await client.close()

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.DELIVERED.value
        assert order.delivery_payload == "E2E-KEY-1"


async def test_oxapay_callback_with_bad_signature_is_rejected(
    settings, session_factory, registry, bot
) -> None:
    order_id, _ = await _seed_order(
        session_factory, provider="oxapay", currency="USD", amount_minor=999, sku="bad-sig"
    )
    client = await _client(settings, session_factory, registry, bot)
    try:
        body = json.dumps(
            {"track_id": "track-123", "status": "Paid", "amount": 10, "value": 9.99, "currency": "POL"}
        ).encode()
        resp = await client.post(
            "/webhooks/oxapay", data=body, headers={"HMAC": "deadbeef"}
        )
        assert resp.status == 400
    finally:
        await client.close()

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.PENDING.value  # untouched


async def test_cryptopay_callback_delivers_order(
    settings, session_factory, registry, bot
) -> None:
    order_id, _ = await _seed_order(
        session_factory, provider="cryptopay", currency="USD", amount_minor=999, sku="cp-sku"
    )
    client = await _client(settings, session_factory, registry, bot)
    try:
        body = json.dumps(
            {
                "update_id": 1,
                "update_type": "invoice_paid",
                "payload": {
                    "invoice_id": 555,
                    "status": "paid",
                    "amount": "9.99",
                    "payload": order_id,
                },
            }
        ).encode()
        secret = hashlib.sha256(CRYPTOPAY_TOKEN.encode()).digest()
        signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
        resp = await client.post(
            "/webhooks/cryptopay",
            data=body,
            headers={"crypto-pay-api-signature": signature},
        )
        assert resp.status == 200
    finally:
        await client.close()

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.DELIVERED.value


async def test_duplicate_callback_delivers_only_once(
    settings, session_factory, registry, bot
) -> None:
    order_id, product_id = await _seed_order(
        session_factory, provider="oxapay", currency="USD", amount_minor=999, sku="dup-sku"
    )
    async with session_factory() as session:
        await catalog.add_stock(session, product_id, ["SECOND-KEY"])
        await session.commit()

    client = await _client(settings, session_factory, registry, bot)
    try:
        body = json.dumps(
            {
                "track_id": "track-123",
                "status": "Paid",
                "amount": 10,
                "value": 9.99,
                "currency": "POL",
                "order_id": order_id,
            }
        ).encode()
        signature = hmac.new(OXAPAY_KEY.encode(), body, hashlib.sha512).hexdigest()
        for _ in range(3):
            resp = await client.post(
                "/webhooks/oxapay", data=body, headers={"HMAC": signature}
            )
            assert resp.status == 200
    finally:
        await client.close()

    async with session_factory() as session:
        delivered = (
            await session.execute(
                StockItem.__table__.select().where(StockItem.delivered_at.is_not(None))
            )
        ).all()
        assert len(delivered) == 1


async def test_underpaid_callback_does_not_deliver(
    settings, session_factory, registry, bot
) -> None:
    order_id, _ = await _seed_order(
        session_factory, provider="oxapay", currency="USD", amount_minor=999, sku="under-sku"
    )
    client = await _client(settings, session_factory, registry, bot)
    try:
        body = json.dumps(
            {
                "track_id": "track-123",
                "status": "Paid",
                "amount": 0.0001,
                "value": 0.01,
                "currency": "BTC",
                "order_id": order_id,
            }
        ).encode()
        signature = hmac.new(OXAPAY_KEY.encode(), body, hashlib.sha512).hexdigest()
        resp = await client.post("/webhooks/oxapay", data=body, headers={"HMAC": signature})
        assert resp.status == 200
    finally:
        await client.close()

    async with session_factory() as session:
        order = await session.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.PENDING.value


async def test_cryptopay_secret_path_is_enforced(
    session_factory, registry, bot
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        PUBLIC_BASE_URL="https://shop.example.com",
        CRYPTOPAY_ENABLED=True,
        CRYPTOPAY_TOKEN=CRYPTOPAY_TOKEN,
        CRYPTOPAY_WEBHOOK_SECRET="s3cret",
    )
    client = await _client(settings, session_factory, registry, bot)
    try:
        body = b"{}"
        resp = await client.post("/webhooks/cryptopay", data=body, headers={})
        assert resp.status == 403
    finally:
        await client.close()


def test_settings_validation_rejects_no_providers() -> None:
    settings = Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        STARS_ENABLED=False,
    )
    with pytest.raises(RuntimeError, match="no payment provider"):
        settings.validate_runtime()


def test_settings_validation_rejects_webhook_without_https() -> None:
    settings = Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        BOT_MODE="webhook",
        PUBLIC_BASE_URL="http://insecure.example.com",
        WEBHOOK_SECRET="x",
    )
    with pytest.raises(RuntimeError, match="https PUBLIC_BASE_URL"):
        settings.validate_runtime()


def test_settings_parses_admin_ids_and_urls() -> None:
    settings = Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        ADMIN_IDS="1, 2;3",
        PUBLIC_BASE_URL="https://shop.example.com/",
        WEBHOOK_SECRET="abc",
        CRYPTOPAY_ENABLED=True,
        CRYPTOPAY_TOKEN="t",
        CRYPTOPAY_WEBHOOK_SECRET="path",
        OXAPAY_ENABLED=True,
        OXAPAY_API_KEY="k",
    )
    assert settings.admin_ids == [1, 2, 3]
    assert settings.telegram_webhook_url == "https://shop.example.com/telegram/webhook/abc"
    assert settings.cryptopay_webhook_url == "https://shop.example.com/webhooks/cryptopay/path"
    assert settings.oxapay_callback_url == "https://shop.example.com/webhooks/oxapay"
    assert settings.enabled_providers() == ["stars", "cryptopay", "oxapay"]
