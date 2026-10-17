"""Telegram Stars provider.

Stars payments do not use an HTTP gateway: the bot sends an XTR invoice with
``sendInvoice`` (no ``provider_token``), Telegram sends ``pre_checkout_query`` and
then a ``successful_payment`` update. Refunds go through ``refundStarPayment``
using the stored ``telegram_payment_charge_id``.

Docs: https://core.telegram.org/bots/api#payments
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import LabeledPrice

from app.db.models import Order, Product
from app.providers.base import Invoice, PaymentState, PaymentUpdate, ProviderError

log = logging.getLogger(__name__)

STARS_CURRENCY = "XTR"


class StarsProvider:
    name = "stars"

    def __init__(self, *, bot: Bot) -> None:
        self._bot = bot

    async def create_invoice(self, order: Order, product: Product) -> Invoice:
        if order.currency != STARS_CURRENCY:
            raise ProviderError(self.name, f"unsupported currency {order.currency}")
        if order.amount_minor <= 0:
            raise ProviderError(self.name, "Stars price must be a positive integer")

        quantity = max(1, order.quantity)
        label = product.title if quantity == 1 else f"{product.title} x{quantity}"
        try:
            message = await self._bot.send_invoice(
                chat_id=order.user_id,
                title=label[:32],
                description=(product.description or product.title)[:255],
                payload=order.id,
                currency=STARS_CURRENCY,
                prices=[LabeledPrice(label=label[:32], amount=order.amount_minor)],
            )
        except Exception as exc:  # aiogram raises TelegramAPIError subclasses
            raise ProviderError(self.name, f"sendInvoice failed: {exc}") from exc

        return Invoice(
            provider_invoice_id=f"{order.user_id}:{message.message_id}",
            pay_url=None,
        )

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> PaymentUpdate | None:
        # Stars updates arrive through the bot's own update stream, not HTTP callbacks.
        return None

    async def check_status(self, order: Order) -> PaymentUpdate | None:
        # Telegram offers no invoice status polling; delivery is driven by the
        # successful_payment update, and refunds by refundStarPayment.
        return None

    @staticmethod
    def update_from_successful_payment(payment: object) -> PaymentUpdate:
        """Build a :class:`PaymentUpdate` from ``Message.successful_payment``."""
        charge_id = getattr(payment, "telegram_payment_charge_id", None)
        order_id = getattr(payment, "invoice_payload", None)
        currency = getattr(payment, "currency", STARS_CURRENCY)
        total = getattr(payment, "total_amount", None)
        return PaymentUpdate(
            provider=StarsProvider.name,
            event_id=str(charge_id or order_id),
            state=PaymentState.PAID,
            order_id=str(order_id) if order_id else None,
            provider_invoice_id=None,
            paid_amount_minor=int(total) if total is not None else None,
            currency=str(currency),
            telegram_payment_charge_id=str(charge_id) if charge_id else None,
            event_type="successful_payment",
        )

    async def refund(self, order: Order) -> None:
        if not order.telegram_payment_charge_id:
            raise ProviderError(self.name, "order has no telegram_payment_charge_id")
        try:
            await self._bot.refund_star_payment(
                user_id=order.user_id,
                telegram_payment_charge_id=order.telegram_payment_charge_id,
            )
        except Exception as exc:
            raise ProviderError(self.name, f"refundStarPayment failed: {exc}") from exc
