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
from app.services import delivery

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

        if product is None:
            await self._safe_send(
                order.user_id,
                f"✅ Order #{order.id[:8]} is paid. Please contact support to receive it.",
            )
            return

        if outcome == "delivered":
            await self._send_file(order, product)
            return

        if outcome == "no_stock":
            await self._safe_send(
                order.user_id,
                "✅ Payment received. We are restocking this item and will send it here "
                f"shortly.\nOrder #{order.id[:8]}",
            )
            await self._alert_admins(
                f"⚠️ Order #{order.id[:8]} is paid but has no stock for "
                f"'{product.title}' ({order.quantity} needed).\n"
                f"Add stock in /panel, then use /retry {order.id}"
            )

    async def _send_file(self, order: Order, product: Product) -> None:
        try:
            await self._bot.send_document(
                order.user_id,
                delivery.build_delivery_file(order, product),
                caption=delivery.delivery_caption(order, product),
            )
        except Exception:
            log.exception("could not send delivery file for order %s", order.id)
            await self._safe_send(
                order.user_id,
                texts.delivery_message(order, product_title=product.title),
            )

    async def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            await self._bot.send_message(chat_id, text)
        except Exception:
            log.exception("could not send message to chat %s", chat_id)

    async def _alert_admins(self, text: str) -> None:
        for admin_id in self._admin_ids:
            await self._safe_send(admin_id, text)
