"""Product catalog access: categories, products and stock."""

from __future__ import annotations

import re
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Order, OrderStatus, Product, StockItem


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "item"


# --------------------------------------------------------------- categories


async def list_categories(
    session: AsyncSession, *, only_active: bool = True
) -> list[Category]:
    stmt = select(Category).order_by(Category.position, Category.id)
    if only_active:
        stmt = stmt.where(Category.is_active.is_(True))
    return list((await session.execute(stmt)).scalars())


async def get_category(session: AsyncSession, category_id: int) -> Category | None:
    return await session.get(Category, category_id)


async def upsert_category(
    session: AsyncSession,
    *,
    name: str,
    emoji: str = "",
    slug: str | None = None,
    position: int = 0,
    is_active: bool = True,
) -> Category:
    slug = slug or slugify(name)
    existing = (
        await session.execute(select(Category).where(Category.slug == slug))
    ).scalar_one_or_none()
    if existing is None:
        existing = Category(slug=slug)
        session.add(existing)
    existing.name = name
    existing.emoji = emoji
    existing.position = position
    existing.is_active = is_active
    await session.flush()
    return existing


async def rename_category(session: AsyncSession, category: Category, *, name: str) -> Category:
    category.name = name
    await session.flush()
    return category


async def delete_category(session: AsyncSession, category: Category) -> None:
    """Remove a category; its products fall back to uncategorised."""
    products = (
        await session.execute(select(Product).where(Product.category_id == category.id))
    ).scalars()
    for product in products:
        product.category_id = None
    await session.delete(category)
    await session.flush()


# ----------------------------------------------------------------- products


async def list_products(
    session: AsyncSession, *, only_active: bool = True, category_id: int | None = None
) -> list[Product]:
    stmt = select(Product).order_by(Product.position, Product.id)
    if only_active:
        stmt = stmt.where(Product.is_active.is_(True))
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
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
    features: str = "",
    guarantee: str = "",
    price_stars: int = 0,
    price_usd_cents: int = 0,
    category_id: int | None = None,
    position: int = 0,
    max_per_order: int = 10,
    is_active: bool = True,
) -> Product:
    product = await get_product_by_sku(session, sku)
    if product is None:
        product = Product(sku=sku)
        session.add(product)
    product.title = title
    product.description = description
    product.features = features
    product.guarantee = guarantee
    product.price_stars = price_stars
    product.price_usd_cents = price_usd_cents
    product.category_id = category_id
    product.position = position
    product.max_per_order = max(1, max_per_order)
    product.is_active = is_active
    await session.flush()
    return product


async def update_product(session: AsyncSession, product: Product, **fields: object) -> Product:
    """Patch selected columns. Unknown keys are ignored."""
    allowed = {
        "title",
        "description",
        "features",
        "guarantee",
        "price_stars",
        "price_usd_cents",
        "category_id",
        "position",
        "max_per_order",
        "is_active",
    }
    for key, value in fields.items():
        if key in allowed:
            setattr(product, key, value)
    await session.flush()
    return product


class ProductHasOrders(Exception):
    """Raised when a product cannot be deleted because orders reference it."""


async def delete_product(session: AsyncSession, product: Product) -> None:
    """Delete a product and its stock.

    Order history is immutable, and ``orders.product_id`` is ``RESTRICT``, so a
    product that was ever ordered cannot be removed. Callers should hide it
    instead. Stock is deleted explicitly: relying on the ORM cascade would
    trigger a lazy load, which is not allowed on an async session.
    """
    ordered = await session.scalar(
        select(func.count(Order.id)).where(Order.product_id == product.id)
    )
    if ordered:
        raise ProductHasOrders(
            f"{product.title} has {ordered} order(s); hide it instead of deleting."
        )
    await session.execute(
        delete(StockItem).where(StockItem.product_id == product.id)
    )
    await session.execute(delete(Product).where(Product.id == product.id))
    await session.flush()


# -------------------------------------------------------------------- stock


async def stock_counts(session: AsyncSession) -> dict[int, int]:
    """Available (undelivered, unreserved) units per product id."""
    stmt = (
        select(StockItem.product_id, func.count(StockItem.id))
        .where(StockItem.delivered_at.is_(None), StockItem.order_id.is_(None))
        .group_by(StockItem.product_id)
    )
    return {pid: count for pid, count in (await session.execute(stmt)).all()}


async def stock_count(session: AsyncSession, product_id: int) -> int:
    stmt = select(func.count(StockItem.id)).where(
        StockItem.product_id == product_id,
        StockItem.delivered_at.is_(None),
        StockItem.order_id.is_(None),
    )
    return int((await session.execute(stmt)).scalar_one())


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


async def list_stock(
    session: AsyncSession, product_id: int, *, limit: int = 50
) -> list[StockItem]:
    stmt = (
        select(StockItem)
        .where(StockItem.product_id == product_id, StockItem.delivered_at.is_(None))
        .order_by(StockItem.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def clear_available_stock(session: AsyncSession, product_id: int) -> int:
    """Delete every unsold unit for a product. Delivered units are kept."""
    items = (
        await session.execute(
            select(StockItem).where(
                StockItem.product_id == product_id, StockItem.delivered_at.is_(None)
            )
        )
    ).scalars()
    removed = 0
    for item in items:
        await session.delete(item)
        removed += 1
    await session.flush()
    return removed


async def sales_stats(session: AsyncSession) -> dict[str, int]:
    stmt = select(Order.status, func.count(Order.id)).group_by(Order.status)
    rows = (await session.execute(stmt)).all()
    stats = {status: count for status, count in rows}
    stats.setdefault(OrderStatus.DELIVERED.value, 0)
    return stats


async def revenue_minor(session: AsyncSession) -> dict[str, int]:
    """Delivered revenue per currency, in minor units."""
    stmt = (
        select(Order.currency, func.sum(Order.amount_minor))
        .where(Order.status == OrderStatus.DELIVERED.value)
        .group_by(Order.currency)
    )
    return {currency: int(total or 0) for currency, total in (await session.execute(stmt)).all()}


def price_usd(product: Product) -> Decimal:
    return (Decimal(product.price_usd_cents) / 100).quantize(Decimal("0.01"))
