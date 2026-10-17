"""Inline keyboards for the shop flow.

Callback data is compact because Telegram caps it at 64 bytes:

* ``m``            main menu
* ``c``            category list
* ``c:<id>``       products in a category
* ``p:<id>``       product card
* ``q:<id>:<n>``   set quantity on a product card
* ``b:<id>:<n>``   buy ``n`` of product ``id`` (choose payment method)
* ``pm:<id>:<n>:<prov>``  pay ``n`` of product ``id`` with ``prov``
* ``k:<order_id>`` check order status
* ``o:<order_id>`` order detail
* ``ol``           order list
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Category, Order, Product

PROVIDER_LABELS = {
    "stars": "⭐ Telegram Stars",
    "cryptopay": "💎 Crypto (@CryptoBot)",
    "oxapay": "🪙 OxaPay",
}


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛍 Browse products", callback_data="c")
    builder.button(text="🧾 My orders", callback_data="ol")
    builder.adjust(1)
    return builder.as_markup()


def category_list(categories: list[Category], *, products_in: dict[int, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        count = products_in.get(category.id, 0)
        label = f"{category.emoji} {category.name}".strip()
        builder.row(
            InlineKeyboardButton(text=f"{label} ({count})", callback_data=f"c:{category.id}")
        )
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="m"))
    return builder.as_markup()


def product_list(
    products: list[Product],
    available: dict[int, int],
    *,
    back_to_categories: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        stock = available.get(product.id, 0)
        dot = "🟢" if stock > 3 else ("🟡" if stock > 0 else "🔴")
        builder.row(
            InlineKeyboardButton(
                text=f"{dot} {product.title}",
                callback_data=f"p:{product.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Categories" if back_to_categories else "⬅️ Menu",
            callback_data="c" if back_to_categories else "m",
        )
    )
    return builder.as_markup()


def product_detail(
    product: Product,
    providers: list[str],
    stock: int,
    quantity: int = 1,
) -> InlineKeyboardMarkup:
    """Quantity stepper plus a Continue button that leads to payment choice."""
    builder = InlineKeyboardBuilder()
    if stock > 0:
        builder.row(
            InlineKeyboardButton(text="➖", callback_data=f"q:{product.id}:{quantity - 1}"),
            InlineKeyboardButton(text=f"Qty: {quantity}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"q:{product.id}:{quantity + 1}"),
        )
        builder.row(
            InlineKeyboardButton(
                text="✅ Continue",
                callback_data=f"b:{product.id}:{quantity}",
            )
        )
    else:
        builder.row(
            InlineKeyboardButton(text="🔔 Notify me", callback_data=f"notify:{product.id}")
        )
    builder.row(InlineKeyboardButton(text="⬅️ Products", callback_data="c"))
    return builder.as_markup()


def payment_methods(
    product: Product, providers: list[str], quantity: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for provider in providers:
        builder.row(
            InlineKeyboardButton(
                text=f"Pay with {PROVIDER_LABELS.get(provider, provider)}",
                callback_data=f"pm:{product.id}:{quantity}:{provider}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Back", callback_data=f"p:{product.id}"))
    return builder.as_markup()


def pay_now(order: Order) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if order.pay_url:
        builder.row(InlineKeyboardButton(text="💳 Pay now", url=order.pay_url))
    builder.row(InlineKeyboardButton(text="🔄 Check status", callback_data=f"k:{order.id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="m"))
    return builder.as_markup()


def order_list(orders: list[Order]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.row(
            InlineKeyboardButton(
                text=f"#{order.id[:8]} · {order.status}",
                callback_data=f"o:{order.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="m"))
    return builder.as_markup()


# ------------------------------------------------------------------ admin


def panel_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📦 Products", callback_data="ap:list"))
    builder.row(InlineKeyboardButton(text="🗂 Categories", callback_data="ac:list"))
    builder.row(InlineKeyboardButton(text="📊 Stats", callback_data="as"))
    builder.row(InlineKeyboardButton(text="❌ Close", callback_data="aclose"))
    return builder.as_markup()


def panel_products(products: list[Product], available: dict[int, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        stock = available.get(product.id, 0)
        state = "" if product.is_active else " (hidden)"
        builder.row(
            InlineKeyboardButton(
                text=f"{product.title} · {stock} in stock{state}",
                callback_data=f"ap:v:{product.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ New product", callback_data="ap:new"))
    builder.row(InlineKeyboardButton(text="⬅️ Panel", callback_data="apanel"))
    return builder.as_markup()


def panel_product(product: Product, stock: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Title", callback_data=f"ap:e:title:{product.id}"),
        InlineKeyboardButton(text="📝 Description", callback_data=f"ap:e:description:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Features", callback_data=f"ap:e:features:{product.id}"),
        InlineKeyboardButton(text="🛡 Guarantee", callback_data=f"ap:e:guarantee:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="⭐ Stars price", callback_data=f"ap:e:price_stars:{product.id}"),
        InlineKeyboardButton(text="💵 USD price", callback_data=f"ap:e:price_usd:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🗂 Category", callback_data=f"ap:e:category:{product.id}"),
        InlineKeyboardButton(text="🔢 Max per order", callback_data=f"ap:e:max:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(
            text="🙈 Hide" if product.is_active else "👁 Show",
            callback_data=f"ap:toggle:{product.id}",
        ),
        InlineKeyboardButton(text="🗑 Delete", callback_data=f"ap:del:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(text=f"➕ Add stock ({stock})", callback_data=f"ap:stock:{product.id}"),
        InlineKeyboardButton(text="👀 View stock", callback_data=f"ap:view:{product.id}"),
    )
    builder.row(
        InlineKeyboardButton(text="🧹 Clear stock", callback_data=f"ap:clear:{product.id}"),
    )
    builder.row(InlineKeyboardButton(text="⬅️ Products", callback_data="ap:list"))
    return builder.as_markup()


def panel_categories(categories: list[Category], counts: dict[int, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"{category.emoji} {category.name} ({counts.get(category.id, 0)})".strip(),
                callback_data=f"ac:v:{category.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ New category", callback_data="ac:new"))
    builder.row(InlineKeyboardButton(text="⬅️ Panel", callback_data="apanel"))
    return builder.as_markup()


def panel_category(category: Category) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Rename", callback_data=f"ac:e:name:{category.id}"),
        InlineKeyboardButton(text="🙂 Emoji", callback_data=f"ac:e:emoji:{category.id}"),
    )
    builder.row(InlineKeyboardButton(text="🗑 Delete", callback_data=f"ac:del:{category.id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Categories", callback_data="ac:list"))
    return builder.as_markup()


def cancel_edit(product_id: int | None = None, category_id: int | None = None) -> InlineKeyboardMarkup:
    if product_id is not None:
        target = f"ap:v:{product_id}"
    elif category_id is not None:
        target = f"ac:v:{category_id}"
    else:
        target = "apanel"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=target))
    return builder.as_markup()


def pick_category(categories: list[Category], product_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="— none —", callback_data=f"ap:setcat:0:{product_id}"))
    for category in categories:
        builder.row(
            InlineKeyboardButton(
                text=f"{category.emoji} {category.name}".strip(),
                callback_data=f"ap:setcat:{category.id}:{product_id}",
            )
        )
    builder.row(InlineKeyboardButton(text="✖️ Cancel", callback_data=f"ap:v:{product_id}"))
    return builder.as_markup()


def confirm(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Yes", callback_data=yes_data),
        InlineKeyboardButton(text="✖️ Cancel", callback_data=no_data),
    )
    return builder.as_markup()
