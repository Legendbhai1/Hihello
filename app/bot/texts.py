"""Shared bot text and formatting helpers."""

from __future__ import annotations

from decimal import Decimal

from app.db.models import Order, Product
from app.services.money import format_amount

STATUS_LABELS = {
    "created": "🕓 Created",
    "pending": "⏳ Awaiting payment",
    "paid": "✅ Paid (preparing delivery)",
    "delivered": "🎉 Delivered",
    "expired": "⌛️ Expired",
    "failed": "❌ Failed",
    "refunded": "↩️ Refunded",
}

PROVIDER_LABELS = {
    "stars": "Telegram Stars",
    "cryptopay": "Crypto (@CryptoBot)",
    "oxapay": "OxaPay",
}


def price_line(product: Product, providers: list[str]) -> str:
    parts: list[str] = []
    if "stars" in providers and product.price_stars > 0:
        parts.append(f"⭐ {product.price_stars} Stars")
    if product.price_usd_cents > 0:
        usd = (Decimal(product.price_usd_cents) / 100).quantize(Decimal("0.01"))
        for provider in ("cryptopay", "oxapay"):
            if provider in providers:
                parts.append(f"💵 ${usd}")
                break
    return " / ".join(parts) if parts else "price not configured"


def product_card(product: Product, providers: list[str], stock: int) -> str:
    stock_line = f"📦 In stock: {stock}" if stock > 0 else "📦 Out of stock"
    description = f"\n\n{product.description}" if product.description else ""
    return (
        f"<b>{product.title}</b>{description}\n\n"
        f"💰 {price_line(product, providers)}\n"
        f"{stock_line}"
    )


def order_card(order: Order, *, product_title: str | None = None) -> str:
    lines = [
        f"<b>Order #{order.id[:8]}</b>",
        f"Status: {STATUS_LABELS.get(order.status, order.status)}",
        f"Amount: {format_amount(order.amount_minor, order.currency)} {order.currency}",
        f"Method: {PROVIDER_LABELS.get(order.provider, order.provider)}",
    ]
    if product_title:
        lines.insert(1, f"Item: {product_title}")
    return "\n".join(lines)


def delivery_message(order: Order, *, product_title: str) -> str:
    return (
        f"🎉 <b>Payment received — here is your order</b>\n\n"
        f"<b>{product_title}</b>\n"
        f"<code>{_escape(order.delivery_payload or '')}</code>\n\n"
        f"Order #{order.id[:8]}\n"
        f"Thank you for your purchase!"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
