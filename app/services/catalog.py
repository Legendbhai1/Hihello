"""Product catalog access."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus, Product, StockItem


async def list_products(session: AsyncSession, *, only_active: bool = True) -> list[Product]:
    stmt = select(Product).order_by(Product.id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def get_product(session: AsyncSession, product_id: int) -> Product | None:
    return await session.get(Product, product_id)


async def get_product_by_sku(session: AsyncSession, sku: str) -> Product | None:
    return (
        await session.execute(select(Product).where(Product.sku == sku))
    ).scalar_one_or_none()


async def upsert_product(
    session: AsyncSession,
    *,
    sku: str,
    title: str,
    description: str = "",
    price_stars: int = 0,
    price_usd_cents: int = 0,
    is_active: bool = True,
) -> Product:
    product = await get_product_by_sku(session, sku)
    if product is None:
        product = Product(sku=sku)
        session.add(product)
    product.title = title
    product.description = description
    product.price_stars = price_stars
    product.price_usd_cents = price_usd_cents
    product.is_active = is_active
    await session.flush()
    return product


async def stock_counts(session: AsyncSession) -> dict[int, int]:
    """Available (undelivered, unreserved) units per product id."""
    stmt = (
        select(StockItem.product_id, func.count(StockItem.id))
        .where(StockItem.delivered_at.is_(None), StockItem.order_id.is_(None))
        .group_by(StockItem.product_id)
    )
    return {pid: count for pid, count in (await session.execute(stmt)).all()}


async def add_stock(session: AsyncSession, product_id: int, payloads: list[str]) -> int:
    added = 0
    for payload in payloads:
        text = payload.strip()
        if not text:
            continue
        session.add(StockItem(product_id=product_id, payload=text))
        added += 1
    await session.flush()
    return added


async def sales_stats(session: AsyncSession) -> dict[str, int]:
    stmt = (
        select(Order.status, func.count(Order.id))
        .group_by(Order.status)
    )
    rows = (await session.execute(stmt)).all()
    stats = {status: count for status, count in rows}
    stats.setdefault(OrderStatus.DELIVERED.value, 0)
    return stats


def price_usd(product: Product) -> Decimal:
    return (Decimal(product.price_usd_cents) / 100).quantize(Decimal("0.01"))
