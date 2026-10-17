"""Bot handler wiring.

A recording session stands in for the Telegram transport so the real dispatcher,
middlewares and handlers run end to end: the tests assert on the API calls the bot
would have made.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerPreCheckoutQuery,
    GetMe,
    SendInvoice,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.bot.dispatcher import create_dispatcher
from app.config import Settings
from app.db.models import Order, OrderStatus
from app.providers.registry import build_registry
from app.services import catalog

USER_ID = 909000111


class RecordingSession(BaseSession):
    """Captures outgoing API calls and returns plausible responses."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def close(self) -> None:  # pragma: no cover - nothing to release
        return None

    async def stream_content(  # pragma: no cover - unused by these tests
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ):
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):
        self.calls.append(method)

        if isinstance(method, GetMe):
            return TgUser(id=1, is_bot=True, first_name="ShopBot", username="shop_bot")

        if isinstance(method, SendInvoice):
            return Message(
                message_id=1000,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
            )

        if isinstance(method, AnswerPreCheckoutQuery):
            return True

        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )

        return True

    def sent_texts(self) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, SendMessage)]


def _user() -> TgUser:
    return TgUser(id=USER_ID, is_bot=False, first_name="Buyer", username="buyer")


def _message(text: str) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=USER_ID, type="private"),
            from_user=_user(),
            text=text,
        ),
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        BOT_TOKEN="123456:TEST-TOKEN",
        ADMIN_IDS="42",
        STARS_ENABLED=True,
        CRYPTOPAY_ENABLED=True,
        CRYPTOPAY_TOKEN="tok",
        OXAPAY_ENABLED=True,
        OXAPAY_API_KEY="key",
    )


async def _dispatcher(settings: Settings, session_factory, session: RecordingSession):
    bot = Bot(token="123456:TEST-TOKEN", session=session)
    registry = build_registry(settings, bot)
    dispatcher = create_dispatcher(
        settings=settings, session_factory=session_factory, registry=registry
    )
    return bot, dispatcher


async def test_start_command_sends_welcome_with_menu(
    settings, session_factory
) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        await dispatcher.feed_update(bot, _message("/start"))
    finally:
        await bot.session.close()

    texts = session.sent_texts()
    assert len(texts) == 1
    assert "Welcome to the shop" in texts[0]
    markup = next(c.reply_markup for c in session.calls if isinstance(c, SendMessage))
    assert markup is not None


async def test_start_persists_the_user(settings, session_factory) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        await dispatcher.feed_update(bot, _message("/start"))
    finally:
        await bot.session.close()

    from app.db.models import User

    async with session_factory() as db:
        user = await db.get(User, USER_ID)
        assert user is not None
        assert user.username == "buyer"


async def test_stars_checkout_sends_invoice_and_creates_order(
    settings, session_factory
) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="stars-prod", title="Stars Item", price_stars=75, price_usd_cents=200
        )
        await catalog.add_stock(db, product.id, ["STARS-KEY"])
        product_id = product.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        update = Update(
            update_id=2,
            callback_query={
                "id": "cb1",
                "from": _user().model_dump(),
                "chat_instance": "ci",
                "data": f"buy:{product_id}:stars",
                "message": {
                    "message_id": 5,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": USER_ID, "type": "private"},
                },
            },
        )
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    invoices = [c for c in session.calls if isinstance(c, SendInvoice)]
    assert len(invoices) == 1
    assert invoices[0].currency == "XTR"
    assert invoices[0].prices[0].amount == 75
    assert invoices[0].provider_token is None  # Stars needs no provider token

    async with session_factory() as db:
        from sqlalchemy import select

        order = (await db.execute(select(Order))).scalar_one()
        assert order.provider == "stars"
        assert order.currency == "XTR"
        assert order.amount_minor == 75
        assert order.status == OrderStatus.PENDING.value
        assert order.pay_url is None  # Stars invoices are paid in-chat


async def test_pre_checkout_accepts_matching_amount(settings, session_factory) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="pc-prod", title="PC Item", price_stars=75, price_usd_cents=200
        )
        await catalog.add_stock(db, product.id, ["K"])
        from app.services.orders import create_order

        order = await create_order(
            db,
            user_id=USER_ID,
            product=product,
            provider="stars",
            amount_minor=75,
            currency="XTR",
            expiry_minutes=30,
        )
        order_id = order.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        update = Update(
            update_id=3,
            pre_checkout_query={
                "id": "pcq1",
                "from": _user().model_dump(),
                "currency": "XTR",
                "total_amount": 75,
                "invoice_payload": order_id,
            },
        )
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    answers = [c for c in session.calls if isinstance(c, AnswerPreCheckoutQuery)]
    assert len(answers) == 1
    assert answers[0].ok is True


async def test_pre_checkout_rejects_wrong_amount(settings, session_factory) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="pc2", title="PC2", price_stars=75, price_usd_cents=200
        )
        from app.services.orders import create_order

        order = await create_order(
            db,
            user_id=USER_ID,
            product=product,
            provider="stars",
            amount_minor=75,
            currency="XTR",
            expiry_minutes=30,
        )
        order_id = order.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        update = Update(
            update_id=4,
            pre_checkout_query={
                "id": "pcq2",
                "from": _user().model_dump(),
                "currency": "XTR",
                "total_amount": 1,  # tampered
                "invoice_payload": order_id,
            },
        )
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    answers = [c for c in session.calls if isinstance(c, AnswerPreCheckoutQuery)]
    assert len(answers) == 1
    assert answers[0].ok is False


async def test_successful_payment_delivers_the_product(
    settings, session_factory
) -> None:
    async with session_factory() as db:
        product = await catalog.upsert_product(
            db, sku="sp", title="SP Item", price_stars=75, price_usd_cents=200
        )
        await catalog.add_stock(db, product.id, ["DELIVERED-KEY"])
        from app.services.orders import create_order

        order = await create_order(
            db,
            user_id=USER_ID,
            product=product,
            provider="stars",
            amount_minor=75,
            currency="XTR",
            expiry_minutes=30,
        )
        order_id = order.id
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        update = Update(
            update_id=5,
            message={
                "message_id": 9,
                "date": int(datetime.now(UTC).timestamp()),
                "chat": {"id": USER_ID, "type": "private"},
                "from": _user().model_dump(),
                "successful_payment": {
                    "currency": "XTR",
                    "total_amount": 75,
                    "invoice_payload": order_id,
                    "telegram_payment_charge_id": "tg-charge-99",
                    "provider_payment_charge_id": "",
                },
            },
        )
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    assert any("DELIVERED-KEY" in t for t in session.sent_texts())

    async with session_factory() as db:
        order = await db.get(Order, order_id)
        assert order is not None
        assert order.status == OrderStatus.DELIVERED.value
        assert order.delivery_payload == "DELIVERED-KEY"
        assert order.telegram_payment_charge_id == "tg-charge-99"


async def test_admin_command_is_ignored_for_non_admins(
    settings, session_factory
) -> None:
    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        await dispatcher.feed_update(bot, _message("/admin"))
    finally:
        await bot.session.close()

    assert session.sent_texts() == []


async def test_shop_list_shows_products(settings, session_factory) -> None:
    async with session_factory() as db:
        await catalog.upsert_product(
            db, sku="listed", title="Listed Item", price_stars=10, price_usd_cents=100
        )
        await db.commit()

    session = RecordingSession()
    bot, dispatcher = await _dispatcher(settings, session_factory, session)
    try:
        update = Update(
            update_id=6,
            callback_query={
                "id": "cb2",
                "from": _user().model_dump(),
                "chat_instance": "ci",
                "data": "shop:list",
                "message": {
                    "message_id": 7,
                    "date": int(datetime.now(UTC).timestamp()),
                    "chat": {"id": USER_ID, "type": "private"},
                    "text": "old",
                },
            },
        )
        await dispatcher.feed_update(bot, update)
    finally:
        await bot.session.close()

    from aiogram.methods import EditMessageText

    edits = [c for c in session.calls if isinstance(c, EditMessageText)]
    assert len(edits) == 1
    assert "Products" in edits[0].text
    labels = [b.text for row in edits[0].reply_markup.inline_keyboard for b in row]
    assert any("Listed Item" in label for label in labels)
