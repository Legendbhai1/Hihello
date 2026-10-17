"""Sending delivery notifications from webhook/reconciler context.

The webhook server and the reconciler are not bot handlers, so they call this
helper directly instead of relying on middleware-provided data.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import texts
from app.db.models import Order, Product

log = logging.getLogger(__name__)


class PaymentNotifier:
    def __init__(
        self,
        *,
        bot: Bot,
        session_factory: async_sessionmaker[AsyncSession],
        admin_ids: list[int],
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._admin_ids = admin_ids

    async def __call__(self, order: Order, outcome: str) -> None:
        async with self._session_factory() as session:
            product = await session.get(Product, order.product_id)
            title = product.title if product else f"order #{order.id[:8]}"

        if outcome == "delivered":
            await self._safe_send(
                order.user_id,
                texts.delivery_message(order, product_title=title),
            )
            return

        if outcome == "no_stock":
            await self._safe_send(
                order.user_id,
                "✅ Payment received. We are preparing your item and will send it here shortly.\n"
                f"Order #{order.id[:8]}",
            )
            await self._alert_admins(
                f"⚠️ Order #{order.id[:8]} is paid but has no stock for '{title}'.\n"
                f"Add stock and run /retry {order.id}"
            )

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except Exception:
            log.exception("could not send message to chat %s", chat_id)

    async def _alert_admins(self, text: str) -> None:
        for admin_id in self._admin_ids:
            await self._safe_send(admin_id, text)
