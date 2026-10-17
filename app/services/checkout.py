"""Checkout orchestration: create an order and a provider invoice."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Order, Product
from app.providers.base import Invoice, ProviderError
from app.providers.registry import ProviderRegistry
from app.services import catalog
from app.services.orders import attach_invoice, create_order

log = logging.getLogger(__name__)

# Currency each provider bills in.
PROVIDER_CURRENCY = {
    "stars": "XTR",
    "cryptopay": "USD",
    "oxapay": "USD",
}


class CheckoutError(Exception):
    pass


def price_for(product: Product, provider: str) -> tuple[int, str]:
    """Return ``(amount_minor, currency)`` for a product on a given provider."""
    currency = PROVIDER_CURRENCY[provider]
    if provider == "stars":
        if product.price_stars <= 0:
            raise CheckoutError("This product has no Stars price configured.")
        return product.price_stars, currency
    if product.price_usd_cents <= 0:
        raise CheckoutError("This product has no USD price configured.")
    return product.price_usd_cents, currency


async def start_checkout(
    session: AsyncSession,
    *,
    settings: Settings,
    registry: ProviderRegistry,
    user_id: int,
    product: Product,
    provider_name: str,
    quantity: int = 1,
) -> Order:
    """Create the order row, then the provider invoice.

    A provider failure leaves the order in ``created`` state so it expires
    harmlessly instead of being retried as a payment.
    """
    provider = registry.get(provider_name)
    if provider is None:
        raise CheckoutError(f"Payment method '{provider_name}' is not available.")

    quantity = int(quantity)
    if quantity < 1:
        raise CheckoutError("Quantity must be at least 1.")
    if quantity > max(1, product.max_per_order):
        raise CheckoutError(
            f"You can order at most {product.max_per_order} of this item at a time."
        )

    in_stock = await catalog.stock_count(session, product.id)
    if in_stock < quantity:
        raise CheckoutError(
            f"Only {in_stock} left in stock, so {quantity} cannot be ordered right now."
        )

    unit_amount_minor, currency = price_for(product, provider_name)
    order = await create_order(
        session,
        user_id=user_id,
        product=product,
        provider=provider_name,
        unit_amount_minor=unit_amount_minor,
        currency=currency,
        quantity=quantity,
        expiry_minutes=settings.order_expiry_minutes,
    )

    try:
        invoice: Invoice = await provider.create_invoice(order, product)
    except ProviderError as exc:
        log.error("invoice creation failed for order %s: %s", order.id, exc)
        raise CheckoutError("Could not reach the payment provider. Please try again.") from exc

    expires_at = order.expires_at
    if invoice.expires_at_unix:
        expires_at = datetime.fromtimestamp(invoice.expires_at_unix, tz=UTC)
    await attach_invoice(
        session, order, invoice_id=invoice.provider_invoice_id, pay_url=invoice.pay_url
    )
    if expires_at is not None:
        order.expires_at = expires_at
        await session.flush()
    return order


async def product_availability(session: AsyncSession, product: Product) -> int:
    counts = await catalog.stock_counts(session)
    return counts.get(product.id, 0)
