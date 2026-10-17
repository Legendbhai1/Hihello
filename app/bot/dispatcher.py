"""Bot wiring: dispatcher, middlewares and router registration."""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.middlewares import DbSessionMiddleware
from app.bot.routers import admin, orders, payments, shop
from app.config import Settings
from app.providers.registry import ProviderRegistry


def create_bot(settings: Settings) -> Bot:
    return Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    registry: ProviderRegistry,
) -> Dispatcher:
    dispatcher = Dispatcher(storage=MemoryStorage())

    session_middleware = DbSessionMiddleware(session_factory)
    dispatcher.update.outer_middleware(session_middleware)
    dispatcher.message.middleware(session_middleware)
    dispatcher.callback_query.middleware(session_middleware)
    dispatcher.pre_checkout_query.middleware(session_middleware)

    # Inject the config and provider registry into every handler.
    dispatcher.workflow_data.update({"settings": settings, "providers": registry})

    # Admin router first so its commands are matched before generic handlers.
    dispatcher.include_router(admin.build_router())
    dispatcher.include_router(shop.build_router())
    dispatcher.include_router(payments.build_router())
    dispatcher.include_router(orders.build_router())
    return dispatcher
