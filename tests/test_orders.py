"""Order lifecycle: idempotency, replay safety and amount verification.

These tests exercise the real database path (SQLite in-memory), not mocks.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import utcnow
from app.db.models import Order, OrderStatus, Product, StockItem
from app.providers.base import PaymentState, PaymentUpdate
from app.services import catalog
from app.services.orders import (
    apply_payment_update,
    create_order,
    deliver_order,
    expire_stale_orders,
    record_event,
    retry_delivery,
)

USER_ID = 555000111


def paid_update(order: Order, *, amount_minor: int | None = None, currency: str | None = None,
                event_id: str = "evt-1") -> PaymentUpdate:
    return PaymentUpdate(
        provider=order.provider,
        event_id=event_id,
        state=PaymentState.PAID,
        order_id=order.id,
        provider_invoice_id=order.provider_invoice_id,
        paid_amount_minor=order.amount_minor if amount_minor is None else amount_minor,
        currency=currency or order.currency,
        event_type="test",
    )


async def _make_order(
    session: AsyncSession,
    product: Product,
    *,
    currency: str = "USD",
    amount_minor: int = 499,
    provider: str = "oxapay",
) -> Order:
    order = await create_order(
        session,
        user_id=USER_ID,
        product=product,
        provider=provider,
        amount_minor=amount_minor,
        currency=currency,
        expiry_minutes=30,
    )
    order.provider_invoice_id = "inv-1"
    order.status = OrderStatus.PENDING.value
    await session.flush()
    return order


async def test_paid_order_with_stock_is_delivered(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1", "KEY-2"])
    order = await _make_order(session, product)

    order, outcome = await apply_payment_update(session, paid_update(order))
    assert outcome == "delivered"
    assert order.status == OrderStatus.DELIVERED.value
    assert order.delivery_payload == "KEY-1"
    assert order.paid_at is not None
    assert order.delivered_at is not None


async def test_replaying_the_same_event_does_not_deliver_twice(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1", "KEY-2"])
    order = await _make_order(session, product)
    update = paid_update(order)

    assert await record_event(session, update, raw_body="{}", signature_valid=True) is True
    await apply_payment_update(session, update)

    # The same notification arrives again (providers retry).
    assert await record_event(session, update, raw_body="{}", signature_valid=True) is False
    assert order.status == OrderStatus.DELIVERED.value

    remaining = (
        await session.execute(select(StockItem).where(StockItem.delivered_at.is_(None)))
    ).scalars().all()
    assert len(remaining) == 1  # exactly one key consumed


async def test_duplicate_event_does_not_roll_back_earlier_work(
    session: AsyncSession, product: Product
) -> None:
    order = await _make_order(session, product)
    update = paid_update(order, event_id="dup")

    assert await record_event(session, update, raw_body="{}", signature_valid=True) is True
    assert await record_event(session, update, raw_body="{}", signature_valid=True) is False

    # The order created before the duplicate insert must still be visible.
    assert await session.get(Order, order.id) is not None


async def test_concurrent_duplicate_payments_deliver_once(
    session_factory: async_sessionmaker[AsyncSession], product: Product
) -> None:
    async with session_factory() as setup:
        await catalog.add_stock(setup, product.id, ["KEY-1", "KEY-2", "KEY-3"])
        order = await _make_order(setup, product)
        order_id = order.id
        await setup.commit()

    async def pay(event_id: str) -> str:
        async with session_factory() as s:
            local_order = await s.get(Order, order_id)
            assert local_order is not None
            _, outcome = await apply_payment_update(s, paid_update(local_order, event_id=event_id))
            await s.commit()
            return outcome

    outcomes = await asyncio.gather(pay("a"), pay("b"), pay("c"))

    async with session_factory() as check:
        final = await check.get(Order, order_id)
        assert final is not None
        assert final.status == OrderStatus.DELIVERED.value
        delivered = (
            await check.execute(select(StockItem).where(StockItem.delivered_at.is_not(None)))
        ).scalars().all()
        assert len(delivered) == 1
    assert outcomes.count("delivered") == 1


async def test_short_payment_is_not_delivered(session: AsyncSession, product: Product) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1"])
    order = await _make_order(session, product)

    order, outcome = await apply_payment_update(session, paid_update(order, amount_minor=1))
    assert outcome == "short_paid"
    assert order.status == OrderStatus.PENDING.value
    assert order.delivery_payload is None

    stock = (
        await session.execute(select(StockItem).where(StockItem.delivered_at.is_(None)))
    ).scalars().all()
    assert len(stock) == 1


async def test_currency_mismatch_is_rejected(session: AsyncSession, product: Product) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1"])
    order = await _make_order(session, product, currency="USD", amount_minor=499)

    # Provider reports 499 BTC-minor units instead of 499 USD cents.
    order, outcome = await apply_payment_update(
        session, paid_update(order, amount_minor=499, currency="BTC")
    )
    assert outcome == "short_paid"
    assert order.status == OrderStatus.PENDING.value


async def test_overpayment_is_accepted(session: AsyncSession, product: Product) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1"])
    order = await _make_order(session, product)
    order, outcome = await apply_payment_update(session, paid_update(order, amount_minor=1000))
    assert outcome == "delivered"


async def test_stars_order_pays_exact_integer_amount(session: AsyncSession, product: Product) -> None:
    await catalog.add_stock(session, product.id, ["STARS-KEY"])
    order = await _make_order(
        session, product, currency="XTR", amount_minor=50, provider="stars"
    )
    update = paid_update(order, amount_minor=50, currency="XTR")
    update.telegram_payment_charge_id = "tg-charge-1"

    order, outcome = await apply_payment_update(session, update)
    assert outcome == "delivered"
    assert order.telegram_payment_charge_id == "tg-charge-1"


async def test_no_stock_leaves_order_paid_and_retryable(
    session: AsyncSession, product: Product
) -> None:
    order = await _make_order(session, product)
    order, outcome = await apply_payment_update(session, paid_update(order))
    assert outcome == "no_stock"
    assert order.status == OrderStatus.PAID.value

    await catalog.add_stock(session, product.id, ["LATE-KEY"])
    assert await retry_delivery(session, order) is True
    assert order.status == OrderStatus.DELIVERED.value
    assert order.delivery_payload == "LATE-KEY"


async def test_expired_update_closes_open_order(session: AsyncSession, product: Product) -> None:
    order = await _make_order(session, product)
    update = PaymentUpdate(
        provider=order.provider,
        event_id="exp-1",
        state=PaymentState.EXPIRED,
        order_id=order.id,
        event_type="invoice_expired",
    )
    order, outcome = await apply_payment_update(session, update)
    assert outcome == "expired"
    assert order.status == OrderStatus.EXPIRED.value


async def test_late_payment_after_expiry_is_not_delivered(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1"])
    order = await _make_order(session, product)
    await apply_payment_update(
        session,
        PaymentUpdate(
            provider=order.provider,
            event_id="exp-1",
            state=PaymentState.EXPIRED,
            order_id=order.id,
        ),
    )
    order, outcome = await apply_payment_update(session, paid_update(order, event_id="late"))
    assert outcome == "already_final"
    assert order.status == OrderStatus.EXPIRED.value


async def test_expire_stale_orders(session: AsyncSession, product: Product) -> None:
    order = await _make_order(session, product)
    order.expires_at = utcnow().replace(year=utcnow().year - 1)
    await session.flush()

    assert await expire_stale_orders(session) == 1
    await session.refresh(order)
    assert order.status == OrderStatus.EXPIRED.value


async def test_delivery_is_idempotent(session: AsyncSession, product: Product) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1", "KEY-2"])
    order = await _make_order(session, product)
    await apply_payment_update(session, paid_update(order))

    first_payload = order.delivery_payload
    await deliver_order(session, order)  # second call must be a no-op
    assert order.delivery_payload == first_payload

    remaining = (
        await session.execute(select(StockItem).where(StockItem.delivered_at.is_(None)))
    ).scalars().all()
    assert len(remaining) == 1


async def test_unknown_order_is_reported(session: AsyncSession, product: Product) -> None:
    update = PaymentUpdate(
        provider="oxapay",
        event_id="ghost",
        state=PaymentState.PAID,
        order_id="does-not-exist",
        paid_amount_minor=100,
        currency="USD",
    )
    order, outcome = await apply_payment_update(session, update)
    assert order is None
    assert outcome == "unknown_order"


async def test_resolution_by_invoice_id_when_order_id_is_absent(
    session: AsyncSession, product: Product
) -> None:
    await catalog.add_stock(session, product.id, ["KEY-1"])
    order = await _make_order(session, product)
    update = PaymentUpdate(
        provider=order.provider,
        event_id="by-invoice",
        state=PaymentState.PAID,
        order_id=None,
        provider_invoice_id="inv-1",
        paid_amount_minor=order.amount_minor,
        currency=order.currency,
    )
    resolved, outcome = await apply_payment_update(session, update)
    assert resolved is not None
    assert resolved.id == order.id
    assert outcome == "delivered"
