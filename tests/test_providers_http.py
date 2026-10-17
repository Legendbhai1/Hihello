"""Provider HTTP contracts, exercised against a real local HTTP server.

These verify the request we actually send (headers, payload shape) and how we
parse the provider's response, without mocking anything: the provider talks to a
live aiohttp server on loopback.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Product
from app.providers.base import ProviderError
from app.providers.cryptopay import CryptoPayProvider
from app.providers.oxapay import OxaPayProvider
from app.services.orders import create_order

TOKEN = "cryptopay-token"
KEY = "oxapay-key"


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.responses: dict[str, tuple[int, dict]] = {}


@pytest.fixture
async def cryptopay_api():
    rec = Recorder()

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        rec.requests.append({"path": request.path, "headers": dict(request.headers), "json": body})
        status, payload = rec.responses.get(request.path, (200, {"ok": True, "result": {}}))
        return web.json_response(payload, status=status)

    client = TestClient(TestServer(web.Application()))
    client.server.app.router.add_post("/api/{method}", handler)
    await client.start_server()
    try:
        yield client, rec
    finally:
        await client.close()


@pytest.fixture
async def oxapay_api():
    rec = Recorder()

    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        rec.requests.append({"path": request.path, "headers": dict(request.headers), "json": body})
        status, payload = rec.responses.get(request.path, (200, {"status": 200, "data": {}}))
        return web.json_response(payload, status=status)

    client = TestClient(TestServer(web.Application()))
    client.server.app.router.add_post("/v1/payment/invoice", handler)
    client.server.app.router.add_post("/v1/payment/{track_id}", handler)
    await client.start_server()
    try:
        yield client, rec
    finally:
        await client.close()


async def test_cryptopay_create_invoice_request_and_parse(
    session: AsyncSession, product: Product, cryptopay_api
) -> None:
    client, rec = cryptopay_api
    rec.responses["/api/createInvoice"] = (
        200,
        {
            "ok": True,
            "result": {
                "invoice_id": 9001,
                "bot_invoice_url": "https://t.me/CryptoBot?start=IVxyz",
                "status": "active",
            },
        },
    )
    provider = CryptoPayProvider(token=TOKEN, base_url=str(client.make_url("")).rstrip("/"))
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="cryptopay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )

    invoice = await provider.create_invoice(order, product)

    assert invoice.provider_invoice_id == "9001"
    assert invoice.pay_url == "https://t.me/CryptoBot?start=IVxyz"

    sent = rec.requests[0]
    assert sent["headers"]["Crypto-Pay-API-Token"] == TOKEN
    assert sent["json"]["currency_type"] == "fiat"
    assert sent["json"]["fiat"] == "USD"
    assert sent["json"]["amount"] == "4.99"  # exact decimal string, not a float
    assert sent["json"]["payload"] == order.id
    assert sent["json"]["expires_in"] > 0


async def test_cryptopay_api_error_becomes_provider_error(
    session: AsyncSession, product: Product, cryptopay_api
) -> None:
    client, rec = cryptopay_api
    rec.responses["/api/createInvoice"] = (
        200,
        {"ok": False, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}},
    )
    provider = CryptoPayProvider(token=TOKEN, base_url=str(client.make_url("")).rstrip("/"))
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="cryptopay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )
    with pytest.raises(ProviderError, match="AMOUNT_TOO_SMALL"):
        await provider.create_invoice(order, product)


async def test_cryptopay_check_status_maps_paid_invoice(
    session: AsyncSession, product: Product, cryptopay_api
) -> None:
    client, rec = cryptopay_api
    rec.responses["/api/getInvoices"] = (
        200,
        {
            "ok": True,
            "result": {
                "items": [
                    {
                        "invoice_id": 9001,
                        "status": "paid",
                        "amount": "4.99",
                        "payload": "some-order",
                    }
                ]
            },
        },
    )
    provider = CryptoPayProvider(token=TOKEN, base_url=str(client.make_url("")).rstrip("/"))
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="cryptopay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )
    order.provider_invoice_id = "9001"
    await session.flush()

    update = await provider.check_status(order)
    assert update is not None
    assert update.state.value == "paid"
    assert update.paid_amount_minor == 499
    assert rec.requests[0]["json"]["invoice_ids"] == "9001"


async def test_oxapay_create_invoice_request_and_parse(
    session: AsyncSession, product: Product, oxapay_api
) -> None:
    client, rec = oxapay_api
    rec.responses["/v1/payment/invoice"] = (
        200,
        {
            "status": 200,
            "data": {
                "track_id": "168931368",
                "payment_url": "https://pay.oxapay.com/168931368",
            },
        },
    )
    provider = OxaPayProvider(
        api_key=KEY,
        base_url=str(client.make_url("")).rstrip("/"),
        callback_url="https://shop.example.com/webhooks/oxapay",
    )
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="oxapay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )

    invoice = await provider.create_invoice(order, product)

    assert invoice.provider_invoice_id == "168931368"
    assert invoice.pay_url == "https://pay.oxapay.com/168931368"

    sent = rec.requests[0]
    assert sent["headers"]["merchant_api_key"] == KEY
    assert sent["json"]["amount"] == 4.99
    assert sent["json"]["currency"] == "USD"
    assert sent["json"]["order_id"] == order.id
    assert sent["json"]["callback_url"] == "https://shop.example.com/webhooks/oxapay"
    assert sent["json"]["sandbox"] is False
    assert sent["json"]["lifetime"] > 0


async def test_oxapay_non_200_status_becomes_provider_error(
    session: AsyncSession, product: Product, oxapay_api
) -> None:
    client, rec = oxapay_api
    rec.responses["/v1/payment/invoice"] = (200, {"status": 401, "message": "Invalid API key"})
    provider = OxaPayProvider(api_key=KEY, base_url=str(client.make_url("")).rstrip("/"))
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="oxapay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )
    with pytest.raises(ProviderError, match="Invalid API key"):
        await provider.create_invoice(order, product)


async def test_oxapay_check_status_maps_paid_payment(
    session: AsyncSession, product: Product, oxapay_api
) -> None:
    client, rec = oxapay_api
    rec.responses["/v1/payment/168931368"] = (
        200,
        {"status": 200, "data": {"track_id": "168931368", "status": "Paid", "amount": 4.99}},
    )
    provider = OxaPayProvider(api_key=KEY, base_url=str(client.make_url("")).rstrip("/"))
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="oxapay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )
    order.provider_invoice_id = "168931368"
    await session.flush()

    update = await provider.check_status(order)
    assert update is not None
    assert update.state.value == "paid"
    assert update.paid_amount_minor == 499


async def test_oxapay_transport_failure_is_retryable(
    session: AsyncSession, product: Product
) -> None:
    # Nothing listening on this port.
    provider = OxaPayProvider(api_key=KEY, base_url="http://127.0.0.1:1")
    order = await create_order(
        session,
        user_id=1,
        product=product,
        provider="oxapay",
        unit_amount_minor=499,
        currency="USD",
        expiry_minutes=30,
    )
    with pytest.raises(ProviderError) as excinfo:
        await provider.create_invoice(order, product)
    assert excinfo.value.retryable is True
