"""Payment flow: starting a checkout, Stars pre-checkout and paid delivery."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.config import Settings
from app.db.models import Order, OrderStatus
from app.providers.registry import ProviderRegistry
from app.providers.stars import StarsProvider
from app.services import catalog
from app.services.checkout import CheckoutError, start_checkout
from app.services.orders import apply_payment_update, record_event

log = logging.getLogger(__name__)

def build_router() -> Router:
    router = Router(name="payments")

    @router.callback_query(F.data.startswith("buy:"))
    async def on_buy(
        callback: CallbackQuery,
        session: AsyncSession,
        settings: Settings,
        providers: ProviderRegistry,
    ) -> None:
        _, raw_id, provider_name = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None or not product.is_active:
            await callback.answer("This product is no longer available.", show_alert=True)
            return

        available = await catalog.stock_counts(session)
        if available.get(product.id, 0) <= 0:
            await callback.answer("Sorry, this item just sold out.", show_alert=True)
            return

        await callback.answer("Creating your order…")
        try:
            order = await start_checkout(
                session,
                settings=settings,
                registry=providers,
                user_id=callback.from_user.id,
                product=product,
                provider_name=provider_name,
            )
        except CheckoutError as exc:
            await callback.message.answer(f"⚠️ {exc}")
            return

        if provider_name == "stars":
            # sendInvoice already put the invoice in the chat.
            await callback.message.answer(
                texts.order_card(order, product_title=product.title),
                reply_markup=keyboards.pay_now(order),
            )
            return

        await callback.message.answer(
            texts.order_card(order, product_title=product.title)
            + "\n\nComplete the payment using the button below. "
            "Your item is sent automatically once the payment is confirmed.",
            reply_markup=keyboards.pay_now(order),
        )

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
            await message.answer("Payment received, but we could not find your order. Contact support.")
            return

        await _deliver_or_explain(message, session, order, outcome)

    @router.callback_query(F.data.startswith("check:"))
    async def on_check_status(callback: CallbackQuery, session: AsyncSession) -> None:
        order = await session.get(Order, callback.data.split(":", 1)[1])
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return
        product = await catalog.get_product(session, order.product_id)
        title = product.title if product else None

        if order.status == OrderStatus.DELIVERED.value:
            await callback.message.answer(texts.delivery_message(order, product_title=title or ""))
            await callback.answer()
            return

        if order.status == OrderStatus.PAID.value:
            await callback.answer("Payment confirmed — preparing your item.", show_alert=True)
            return

        await callback.answer(
            f"Current status: {texts.STATUS_LABELS.get(order.status, order.status)}", show_alert=True
        )

    async def _deliver_or_explain(
        message: Message, session: AsyncSession, order: Order, outcome: str
    ) -> None:
        product = await catalog.get_product(session, order.product_id)
        title = product.title if product else "your item"

        if outcome == "delivered":
            await message.answer(texts.delivery_message(order, product_title=title))
        elif outcome == "no_stock":
            await message.answer(
                "✅ Payment received. We are restocking this item and will send it here shortly.\n"
                f"Order #{order.id[:8]}"
            )
        elif outcome == "short_paid":
            await message.answer(
                "⚠️ We received less than the order amount, so it is on hold.\n"
                "Please contact support with your order number."
            )
        else:
            await message.answer(
                "Payment noted. Use /orders to check the status.",
                reply_markup=keyboards.pay_now(order),
            )

    return router
