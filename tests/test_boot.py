"""Booting the real application wiring.

``build_application`` must produce a working object graph, and the assembled
aiohttp app must serve the callback routes, without any network access.
"""

from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from app.config import Settings
from app.main import build_application


def _settings(**overrides) -> Settings:
    values = {
        "BOT_TOKEN": "123456:TEST-TOKEN",
        "ADMIN_IDS": "42",
        "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
        "PUBLIC_BASE_URL": "https://shop.example.com",
        "STARS_ENABLED": True,
        "CRYPTOPAY_ENABLED": True,
        "CRYPTOPAY_TOKEN": "tok",
        "OXAPAY_ENABLED": True,
        "OXAPAY_API_KEY": "key",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


async def test_build_application_wires_everything() -> None:
    ctx = await build_application(_settings())
    try:
        assert ctx.registry.enabled() == ["stars", "cryptopay", "oxapay"]
        assert ctx.dispatcher.resolve_used_update_types()  # handlers registered
        assert ctx.session_factory is not None

        client = TestClient(TestServer(ctx.web_app))
        await client.start_server()
        try:
            resp = await client.get("/health")
            assert resp.status == 200
            assert (await resp.json())["status"] == "ok"

            # Callback routes exist and reject unsigned traffic.
            resp = await client.post("/webhooks/oxapay", data=b"{}", headers={})
            assert resp.status == 400
            resp = await client.post("/webhooks/cryptopay", data=b"{}", headers={})
            assert resp.status == 400
        finally:
            await client.close()
    finally:
        await ctx.aclose()


async def test_webhook_mode_exposes_the_telegram_route() -> None:
    ctx = await build_application(
        _settings(
            BOT_MODE="webhook",
            WEBHOOK_SECRET="s3cret",
            PUBLIC_BASE_URL="https://shop.example.com",
        )
    )
    try:
        client = TestClient(TestServer(ctx.web_app))
        await client.start_server()
        try:
            # Wrong secret path must be forbidden.
            resp = await client.post("/telegram/webhook/wrong", json={"update_id": 1})
            assert resp.status == 403
        finally:
            await client.close()
    finally:
        await ctx.aclose()


async def test_polling_mode_has_no_telegram_route() -> None:
    ctx = await build_application(_settings(BOT_MODE="polling"))
    try:
        client = TestClient(TestServer(ctx.web_app))
        await client.start_server()
        try:
            resp = await client.post("/telegram/webhook/anything", json={"update_id": 1})
            assert resp.status == 404
        finally:
            await client.close()
    finally:
        await ctx.aclose()


async def test_database_schema_is_created_on_boot() -> None:
    ctx = await build_application(_settings())
    try:
        from sqlalchemy import inspect

        async with ctx.engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert {"users", "products", "orders", "stock_items", "payment_events"} <= set(tables)
    finally:
        await ctx.aclose()
