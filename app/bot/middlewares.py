"""Middleware that gives each update its own database session and user row."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from aiogram.types import User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.orders import get_or_create_user


class DbSessionMiddleware(BaseMiddleware):
    """Injects ``session`` into handler data and commits on success.

    Handlers run inside a single transaction per update. A handler exception rolls
    the whole update back, so a partially applied order is never persisted.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            # Handlers that start background work (the stock watcher) need a way
            # to open their own sessions once this one is committed and closed.
            session.info["session_factory"] = self._session_factory
            from_user: TgUser | None = getattr(event, "from_user", None)
            if from_user is not None and not from_user.is_bot:
                data["db_user"] = await get_or_create_user(
                    session,
                    user_id=from_user.id,
                    username=from_user.username,
                    first_name=from_user.first_name,
                    language_code=from_user.language_code,
                )
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


__all__ = ["DbSessionMiddleware"]
