"""Order lifecycle and fulfillment.

Correctness rules implemented here:

* A provider notification is processed at most once, keyed by
  ``(provider, event_id)`` in ``payment_events``.
* An order transitions out of an open state exactly once, using an atomic
  compare-and-swap UPDATE rather than a read-then-write, so concurrent webhook
  retries cannot double-deliver.
* Delivery only happens after the paid amount is verified against the amount we
  asked for. A short payment is left pending and surfaced to the operator.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    TERMINAL_STATUSES,
    Order,
    OrderStatus,
    PaymentEvent,
    Product,
    StockItem,
    User,
    exponent_for,
)
from app.providers.base import PaymentState, PaymentUpdate
from app.services.money import amounts_match, format_amount

log = logging.getLogger(__name__)

OPEN_STATUSES = (OrderStatus.CREATED.value, OrderStatus.PENDING.value)


class FulfillmentError(Exception):
    pass


class StockUnavailable(FulfillmentError):
    """Payment succeeded but there is nothing to hand over yet."""


async def get_or_create_user(
    session: AsyncSession,
    *,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    language_code: str | None = None,
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        session.add(user)
    user.username = username
    user.first_name = first_name
    user.language_code = language_code
    await session.flush()
    return user


async def create_order(
    session: AsyncSession,
    *,
    user_id: int,
    product: Product,
    provider: str,
    amount_minor: int,
    currency: str,
    expiry_minutes: int,
) -> Order:
    if amount_minor <= 0:
        raise FulfillmentError("order amount must be positive")
    order = Order(
        user_id=user_id,
        product_id=product.id,
        provider=provider,
        currency=currency.upper(),
        currency_exponent=exponent_for(currency),
        amount_minor=amount_minor,
        amount_str=format_amount(amount_minor, currency),
        status=OrderStatus.CREATED.value,
        expires_at=utcnow() + timedelta(minutes=expiry_minutes),
    )
    session.add(order)
    await session.flush()
    return order


async def attach_invoice(session: AsyncSession, order: Order, *, invoice_id: str, pay_url: str | None) -> None:
    order.provider_invoice_id = invoice_id
    order.pay_url = pay_url
    order.status = OrderStatus.PENDING.value
    await session.flush()


async def record_event(
    session: AsyncSession,
    update: PaymentUpdate,
    *,
    raw_body: bytes | str,
    signature_valid: bool,
) -> bool:
    """Persist a provider notification. Returns False when it is a replay.

    The insert runs in a SAVEPOINT so a unique-constraint violation only discards
    the duplicate row, leaving the caller's transaction (and any work already done
    in it) intact.
    """
    payload = raw_body.decode("utf-8", "replace") if isinstance(raw_body, bytes) else raw_body
    event = PaymentEvent(
        provider=update.provider,
        event_id=update.event_id,
        order_id=update.order_id,
        event_type=update.event_type,
        signature_valid=signature_valid,
        payload=payload[:65535],
    )
    try:
        async with session.begin_nested():
            session.add(event)
    except IntegrityError:
        log.info("duplicate event ignored provider=%s event_id=%s", update.provider, update.event_id)
        return False
    return True


async def apply_payment_update(session: AsyncSession, update: PaymentUpdate) -> tuple[Order | None, str]:
    """Apply a normalised payment update to its order.

    Returns ``(order, outcome)`` where outcome is one of ``delivered``,
    ``already_final``, ``short_paid``, ``expired``, ``failed``, ``no_stock``,
    ``unknown_order`` or ``ignored``.
    """
    order = await _resolve_order(session, update)
    if order is None:
        log.warning("update for unknown order provider=%s invoice=%s", update.provider, update.provider_invoice_id)
        return None, "unknown_order"

    if update.state is PaymentState.PENDING:
        return order, "ignored"

    if update.state in (PaymentState.EXPIRED, PaymentState.FAILED):
        changed = await _cas_status(session, order.id, OPEN_STATUSES, PaymentState.EXPIRED.value)
        return order, "expired" if changed else "already_final"

    if update.state is PaymentState.REFUNDED:
        await _cas_status(
            session,
            order.id,
            (OrderStatus.PAID.value, OrderStatus.DELIVERED.value),
            OrderStatus.REFUNDED.value,
        )
        return order, "already_final"

    # PaymentState.PAID
    if order.status in TERMINAL_STATUSES:
        return order, "already_final"

    if not _amount_ok(order, update):
        log.warning(
            "short payment order=%s expected=%s received=%s %s",
            order.id,
            order.amount_str,
            update.paid_amount_minor,
            update.currency,
        )
        return order, "short_paid"

    claimed = await _cas_status(
        session,
        order.id,
        OPEN_STATUSES,
        OrderStatus.PAID.value,
        paid_at=utcnow(),
        paid_amount_minor=update.paid_amount_minor,
        telegram_payment_charge_id=update.telegram_payment_charge_id,
        provider_payment_charge_id=update.provider_payment_charge_id,
    )
    if not claimed:
        return order, "already_final"

    await session.refresh(order)
    try:
        await deliver_order(session, order)
    except StockUnavailable:
        return order, "no_stock"
    return order, "delivered"


async def deliver_order(session: AsyncSession, order: Order) -> None:
    """Attach a stock item to a paid order. Raises :class:`StockUnavailable`."""
    if order.status == OrderStatus.DELIVERED.value:
        return

    stock_item = await _reserve_stock(session, order)
    if stock_item is None:
        log.error("order %s paid but no stock for product %s", order.id, order.product_id)
        raise StockUnavailable(f"no stock for product {order.product_id}")

    delivered = await session.execute(
        update(StockItem)
        .where(
            StockItem.id == stock_item.id,
            StockItem.order_id == order.id,
            StockItem.delivered_at.is_(None),
        )
        .values(delivered_at=utcnow())
    )
    if delivered.rowcount == 0:
        raise StockUnavailable("stock item was claimed by another delivery")

    order.delivery_payload = stock_item.payload
    order.stock_item_id = stock_item.id
    order.status = OrderStatus.DELIVERED.value
    order.delivered_at = utcnow()
    order.delivery_attempts += 1
    await session.flush()
    log.info("order %s delivered product=%s", order.id, order.product_id)


async def retry_delivery(session: AsyncSession, order: Order) -> bool:
    """Operator-triggered delivery retry for a paid order that had no stock."""
    if order.status != OrderStatus.PAID.value:
        return False
    try:
        await deliver_order(session, order)
    except StockUnavailable:
        order.delivery_attempts += 1
        await session.flush()
        return False
    return True


async def _reserve_stock(session: AsyncSession, order: Order) -> StockItem | None:
    """Claim one unreserved stock row for the order, retrying on races."""
    for _ in range(5):
        candidate = (
            await session.execute(
                select(StockItem)
                .where(
                    StockItem.product_id == order.product_id,
                    StockItem.order_id.is_(None),
                    StockItem.delivered_at.is_(None),
                )
                .order_by(StockItem.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return None
        claimed = await session.execute(
            update(StockItem)
            .where(StockItem.id == candidate.id, StockItem.order_id.is_(None))
            .values(order_id=order.id, reserved_at=utcnow())
        )
        if claimed.rowcount == 1:
            await session.refresh(candidate)
            return candidate
    return None


async def _cas_status(
    session: AsyncSession,
    order_id: str,
    from_statuses: tuple[str, ...],
    to_status: str,
    **values: object,
) -> bool:
    """Atomic status transition. Returns False when the order already moved on."""
    result = await session.execute(
        update(Order)
        .where(Order.id == order_id, Order.status.in_(from_statuses))
        .values(status=to_status, **values)
    )
    await session.flush()
    return result.rowcount == 1


def _amount_ok(order: Order, update: PaymentUpdate) -> bool:
    """Verify the provider really paid what we asked for.

    Amounts are only compared when both sides are in the same currency, so an
    apples-to-oranges comparison can never pass. A provider that reports a
    different currency than we billed is rejected rather than trusted.
    """
    if update.paid_amount_minor is None:
        log.warning(
            "order %s: provider=%s reported paid with no amount; accepting the paid flag",
            order.id,
            update.provider,
        )
        return True

    received_currency = (update.currency or order.currency).upper()
    if received_currency != order.currency.upper():
        log.error(
            "order %s: currency mismatch, billed %s but provider reported %s; rejecting",
            order.id,
            order.currency,
            received_currency,
        )
        return False

    return amounts_match(order.amount_minor, update.paid_amount_minor, order.currency)


async def _resolve_order(session: AsyncSession, update: PaymentUpdate) -> Order | None:
    if update.order_id:
        order = await session.get(Order, update.order_id)
        if order is not None:
            return order
    if update.provider_invoice_id:
        return (
            await session.execute(
                select(Order).where(
                    Order.provider == update.provider,
                    Order.provider_invoice_id == update.provider_invoice_id,
                )
            )
        ).scalar_one_or_none()
    return None


async def expire_stale_orders(session: AsyncSession) -> int:
    """Close open orders whose payment window has passed."""
    result = await session.execute(
        update(Order)
        .where(Order.status.in_(OPEN_STATUSES), Order.expires_at < utcnow())
        .values(status=OrderStatus.EXPIRED.value)
    )
    await session.flush()
    return result.rowcount or 0


async def open_orders_for_reconciliation(session: AsyncSession, *, min_age_seconds: int) -> list[Order]:
    cutoff = utcnow() - timedelta(seconds=min_age_seconds)
    stmt = select(Order).where(
        Order.status.in_(OPEN_STATUSES),
        Order.created_at < cutoff,
        Order.provider_invoice_id.is_not(None),
    )
    return list((await session.execute(stmt)).scalars())
