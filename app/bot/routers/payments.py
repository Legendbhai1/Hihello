"""Payment confirmation: Stars pre-checkout, paid delivery and status checks."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.db.models import Order, OrderStatus
from app.providers.stars import StarsProvider
from app.services import catalog, delivery
from app.services.orders import apply_payment_update, record_event

log = logging.getLogger(__name__)


def build_router() -> Router:
    router = Router(name="payments")

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery, session: AsyncSession) -> None:
        """Telegram gives 10 seconds to accept or reject a Stars charge."""
        order = await session.get(Order, query.invoice_payload or "")
        if order is None or not order.is_open():
            await query.answer(ok=False, error_message="This order is no longer valid.")
            return
        if order.amount_minor != query.total_amount or query.currency != order.currency:
            log.warning(
                "pre_checkout mismatch order=%s expected=%s %s got=%s %s",
                order.id,
                order.amount_minor,
                order.currency,
                query.total_amount,
                query.currency,
            )
            await query.answer(ok=False, error_message="Price changed, please order again.")
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def on_successful_payment(message: Message, session: AsyncSession) -> None:
        payment = message.successful_payment
        update = StarsProvider.update_from_successful_payment(payment)

        fresh = await record_event(
            session, update, raw_body=payment.model_dump_json(), signature_valid=True
        )
        if not fresh:
            return

        order, outcome = await apply_payment_update(session, update)
        if order is None:
            log.error("Stars payment for unknown order payload=%s", payment.invoice_payload)
            await message.answer(
                "Payment received, but we could not find your order. Please contact support."
            )
            return

        await deliver_or_explain(message, session, order, outcome)

    @router.callback_query(F.data.startswith("k:"))
    async def on_check_status(callback: CallbackQuery, session: AsyncSession) -> None:
        order = await session.get(Order, callback.data.split(":", 1)[1])
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return
        product = await catalog.get_product(session, order.product_id)

        if order.status == OrderStatus.DELIVERED.value and product is not None:
            await callback.message.answer_document(
                delivery.build_delivery_file(order, product),
                caption=delivery.delivery_caption(order, product),
            )
            await callback.answer("Here is your file again.")
            return

        if order.status == OrderStatus.PAID.value:
            await callback.answer("Payment confirmed — preparing your item.", show_alert=True)
            return

        await callback.answer(
            f"Current status: {texts.STATUS_LABELS.get(order.status, order.status)}",
            show_alert=True,
        )

    return router


async def deliver_or_explain(
    message: Message, session: AsyncSession, order: Order, outcome: str
) -> None:
    """Tell the buyer what happened after a successful payment."""
    product = await catalog.get_product(session, order.product_id)
    title = product.title if product else "your item"

    if outcome == "delivered" and product is not None:
        await message.answer_document(
            delivery.build_delivery_file(order, product),
            caption=delivery.delivery_caption(order, product),
        )
        return

    if outcome == "no_stock":
        await message.answer(
            "✅ Payment received. We are restocking this item and will send it here shortly.\n"
            f"Order #{order.id[:8]}"
        )
        return

    if outcome == "short_paid":
        await message.answer(
            "⚠️ We received less than the order amount, so it is on hold.\n"
            "Please contact support with your order number."
        )
        return

    await message.answer(
        f"Payment noted for {title}. Use /orders to check the status.",
        reply_markup=keyboards.main_menu(),
    )
