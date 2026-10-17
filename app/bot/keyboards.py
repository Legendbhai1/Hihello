"""Inline keyboards for the shop flow."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.models import Order, Product

PROVIDER_LABELS = {
    "stars": "⭐ Telegram Stars",
    "cryptopay": "💎 Crypto (@CryptoBot)",
    "oxapay": "🪙 OxaPay",
}


def main_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛍 Products", callback_data="shop:list")
    builder.button(text="🧾 My orders", callback_data="orders:list")
    builder.adjust(1)
    return builder.as_markup()


def product_list(products: list[Product], available: dict[int, int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        stock = available.get(product.id, 0)
        suffix = "" if stock > 0 else " · out of stock"
        builder.row(
            InlineKeyboardButton(
                text=f"{product.title}{suffix}",
                callback_data=f"product:{product.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="menu"))
    return builder.as_markup()


def product_detail(product: Product, providers: list[str], in_stock: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if in_stock:
        for provider in providers:
            builder.row(
                InlineKeyboardButton(
                    text=f"Buy with {PROVIDER_LABELS.get(provider, provider)}",
                    callback_data=f"buy:{product.id}:{provider}",
                )
            )
    builder.row(InlineKeyboardButton(text="⬅️ Products", callback_data="shop:list"))
    return builder.as_markup()


def pay_now(order: Order) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if order.pay_url:
        builder.row(InlineKeyboardButton(text="💳 Pay now", url=order.pay_url))
    builder.row(InlineKeyboardButton(text="🔄 Check status", callback_data=f"check:{order.id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="menu"))
    return builder.as_markup()


def order_list(orders: list[Order]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.row(
            InlineKeyboardButton(
                text=f"#{order.id[:8]} · {order.status}",
                callback_data=f"order:{order.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="⬅️ Menu", callback_data="menu"))
    return builder.as_markup()
