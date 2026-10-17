"""Quantity-aware checkout and multi-unit delivery.

These exercise the order service directly against a real database, because the
risky part is the stock reservation, not the Telegram plumbing.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Product
from app.services import catalog
from app.services.checkout import CheckoutError, start_checkout
from app.services.orders import (
    StockUnavailable,
    apply_payment_update,
    create_order,
    deliver_order,
)

USER_ID = 5150


class _Registry:
    """Minimal stand-in for ProviderRegistry: checkout only needs ``get``."""

    def __init__(self, provider: object | None) -> None:
        self._provider = provider

    def get(self, name: str):
        return self._provider

    def enabled(self) -> list[str]:
        return []


async def _paid_order(session: AsyncSession, product: Product, quantity: int) -> Order:
    order = await create_order(
        session,
        user_id=USER_ID,
        product=product,
        provider="stars",
        unit_amount_minor=product.price_stars,
        currency="XTR",
        quantity=quantity,
        expiry_minutes=30,
    )
    order.status = OrderStatus.PAID.value
    await session.flush()
    return order


async def test_delivery_reserves_exactly_the_requested_quantity(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["K1", "K2", "K3", "K4"])
    order = await _paid_order(session, product, quantity=3)

    await deliver_order(session, order)

    assert order.status == OrderStatus.DELIVERED.value
    assert order.delivered_list() == ["K1", "K2", "K3"]
    # The fourth unit stays available for the next buyer.
    assert await catalog.stock_count(session, product.id) == 1


async def test_delivery_is_all_or_nothing_when_stock_is_short(
    session: AsyncSession, product: Product
) -> None:
    """A short order must not take a partial reservation and strand it."""
    await catalog.add_stock(session, product.id, ["K1", "K2"])
    order = await _paid_order(session, product, quantity=3)

    try:
        await deliver_order(session, order)
    except StockUnavailable as exc:
        assert "no stock" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected delivery to fail")

    assert order.status == OrderStatus.PAID.value
    # Nothing was left reserved: both units are still sellable.
    assert await catalog.stock_count(session, product.id) == 2


async def test_retry_delivers_once_stock_is_restocked(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["K1"])
    order = await _paid_order(session, product, quantity=2)
    try:
        await deliver_order(session, order)
    except StockUnavailable:
        pass  # expected: only one unit was in stock

    await catalog.add_stock(session, product.id, ["K2"])
    from app.services.orders import retry_delivery

    assert await retry_delivery(session, order) is True
    assert order.delivered_list() == ["K1", "K2"]


async def test_quantity_below_stock_is_rejected_at_checkout(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["K1"])
    try:
        await start_checkout(
            session,
            settings=_settings(),
            registry=_Registry(_FakeProvider()),
            user_id=USER_ID,
            product=product,
            provider_name="stars",
            quantity=5,
        )
    except CheckoutError as exc:
        assert "stock" in str(exc).lower()
    else:  # pragma: no cover - must raise
        raise AssertionError("expected CheckoutError")


async def test_quantity_above_max_per_order_is_rejected(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, [f"K{i}" for i in range(50)])
    await catalog.update_product(session, product, max_per_order=2)
    try:
        await start_checkout(
            session,
            settings=_settings(),
            registry=_Registry(_FakeProvider()),
            user_id=USER_ID,
            product=product,
            provider_name="stars",
            quantity=3,
        )
    except CheckoutError as exc:
        assert "at most" in str(exc)
    else:  # pragma: no cover - must raise
        raise AssertionError("expected CheckoutError")


async def test_payment_then_delivery_of_multiple_units(
    session: AsyncSession, product: Product
) -> None:
    """End to end through the payment update, not just deliver_order."""
    await catalog.add_stock(session, product.id, ["M1", "M2"])
    order = await create_order(
        session,
        user_id=USER_ID,
        product=product,
        provider="stars",
        unit_amount_minor=product.price_stars,
        currency="XTR",
        quantity=2,
        expiry_minutes=30,
    )
    order.provider_invoice_id = "inv-multi"
    order.status = OrderStatus.PENDING.value
    await session.flush()

    update = _paid_update(order)
    order, outcome = await apply_payment_update(session, update)

    assert outcome == "delivered"
    assert order.delivered_list() == ["M1", "M2"]


def _settings():
    from app.config import Settings

    return Settings(BOT_TOKEN="123456:TEST", ADMIN_IDS="1")  # type: ignore[call-arg]


class _FakeProvider:
    name = "stars"


def _paid_update(order: Order):
    from app.providers.base import PaymentState, PaymentUpdate

    return PaymentUpdate(
        provider=order.provider,
        event_id="evt-multi",
        state=PaymentState.PAID,
        order_id=order.id,
        provider_invoice_id=order.provider_invoice_id,
        paid_amount_minor=order.amount_minor,
        currency=order.currency,
        event_type="paid",
    )
