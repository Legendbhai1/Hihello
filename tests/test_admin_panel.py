"""Admin panel: product, stock and category management through the bot.

The panel is driven by callbacks and FSM states, so these feed real updates
through the real dispatcher and then assert on both the API calls and the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.services import catalog
from tests.test_bot_flows import RecordingSession, _callback, _dispatcher, _message

ADMIN_ID = 42  # matches ADMIN_IDS in the settings fixture
USER_ID = 909000111


def _admin_settings():
    from app.config import Settings

    return Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        ADMIN_IDS=str(ADMIN_ID),
        STARS_ENABLED=True,
    )


def _admin_message(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Boss"),
            text=text,
        ),
    )


def _admin_callback(data: str) -> Update:
    return _callback(data, user_id=ADMIN_ID)


@pytest.fixture
def settings():
    return _admin_settings()


async def test_panel_opens_for_an_admin(session_factory) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_message("/panel"))
    finally:
        await bot.session.close()

    texts = session.sent_texts()
    assert any("Store panel" in t for t in texts)


async def test_panel_is_hidden_from_regular_users(session_factory) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _message("/panel"))
    finally:
        await bot.session.close()

    assert session.sent_texts() == []


async def test_admin_creates_a_product_then_edits_its_description(
    session_factory,
) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        # 1. /panel -> New product, then the one-line spec.
        await dispatcher.feed_update(bot, _admin_message("/panel"))
        await dispatcher.feed_update(bot, _admin_callback("ap:new"))
        await dispatcher.feed_update(
            bot, _admin_message("netflix-1m | Netflix 1 Month | 250 | 4.99")
        )

        async with session_factory() as db:
            product = await catalog.get_product_by_sku(db, "netflix-1m")
        assert product is not None
        assert product.price_stars == 250
        assert product.price_usd_cents == 499

        # 2. Edit the description through the field prompt.
        await dispatcher.feed_update(bot, _admin_callback(f"ap:e:description:{product.id}"))
        await dispatcher.feed_update(bot, _admin_message("Watch anything, instantly."))

        async with session_factory() as db:
            product = await catalog.get_product(db, product.id)
        assert product is not None
        assert product.description == "Watch anything, instantly."
    finally:
        await bot.session.close()


async def test_admin_restocks_a_product(session_factory) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="restock", title="Restock Me", price_stars=10, price_usd_cents=100
        )
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_callback(f"ap:stock:{product_id}"))
        await dispatcher.feed_update(bot, _admin_message("KEY-A\nKEY-B\nKEY-C"))
    finally:
        await bot.session.close()

    async with session_factory() as db:
        assert await catalog.stock_count(db, product_id) == 3
        items = await catalog.list_stock(db, product_id)
    assert [item.payload for item in items] == ["KEY-A", "KEY-B", "KEY-C"]


async def test_admin_creates_a_category_and_assigns_a_product(
    session_factory,
) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="cat-prod", title="Categorised", price_stars=10, price_usd_cents=100
        )
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_callback("ac:new"))
        await dispatcher.feed_update(bot, _admin_message("🎬 Streaming"))

        async with session_factory() as db:
            categories = await catalog.list_categories(db, only_active=False)
        assert len(categories) == 1
        assert categories[0].name == "Streaming"
        assert categories[0].emoji == "🎬"

        await dispatcher.feed_update(
            bot, _admin_callback(f"ap:setcat:{categories[0].id}:{product_id}")
        )
    finally:
        await bot.session.close()

    async with session_factory() as db:
        product = await catalog.get_product(db, product_id)
        assert product is not None
        assert product.category_id == categories[0].id


async def test_admin_hides_a_product_from_the_shop(session_factory) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="hide-me", title="Hide Me", price_stars=10, price_usd_cents=100
        )
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_callback(f"ap:toggle:{product_id}"))
    finally:
        await bot.session.close()

    async with session_factory() as db:
        visible = await catalog.list_products(db, only_active=True)
        product = await catalog.get_product(db, product_id)
    assert all(p.id != product_id for p in visible)
    assert product is not None and product.is_active is False


async def test_deleting_a_product_requires_confirmation(session_factory) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="del-me", title="Delete Me", price_stars=10, price_usd_cents=100
        )
        await catalog.add_stock(db, product.id, ["X"])
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        # First tap only asks; the product must still exist.
        await dispatcher.feed_update(bot, _admin_callback(f"ap:del:{product_id}"))
        async with session_factory() as db:
            assert await catalog.get_product(db, product_id) is not None

        await dispatcher.feed_update(bot, _admin_callback(f"ap:delok:{product_id}"))
    finally:
        await bot.session.close()

    async with session_factory() as db:
        assert await catalog.get_product(db, product_id) is None


async def test_admin_can_clear_stock_without_deleting_the_product(
    session_factory,
) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="clear-stock", title="Clear", price_stars=10, price_usd_cents=100
        )
        await catalog.add_stock(db, product.id, ["S1", "S2"])
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_callback(f"ap:clearok:{product_id}"))
    finally:
        await bot.session.close()

    async with session_factory() as db:
        assert await catalog.stock_count(db, product_id) == 0
        assert await catalog.get_product(db, product_id) is not None


async def test_stats_reports_counts(session_factory) -> None:
    async with session_factory() as db:
        await catalog.upsert_product(
            db, sku="s1", title="One", price_stars=10, price_usd_cents=100
        )
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(_admin_settings(), session_factory, session)
    try:
        await dispatcher.feed_update(bot, _admin_message("/stats"))
    finally:
        await bot.session.close()

    assert any("Store stats" in t for t in session.sent_texts())
