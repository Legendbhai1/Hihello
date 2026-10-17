"""Turning a delivered order into a file the buyer can keep.

Digital goods are handed over as a ``.txt`` attachment so the buyer has the keys
in a durable place rather than only in chat history.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import BufferedInputFile

from app.db.models import Order, Product

MAX_CAPTION = 1024


def build_delivery_file(order: Order, product: Product) -> BufferedInputFile:
    """Render the order's delivered units as a UTF-8 text file."""
    items = order.delivered_list()
    lines = [
        f"{product.title}",
        "=" * len(product.title),
        "",
        f"Order ID : {order.id}",
        f"Quantity : {len(items)}",
        f"Amount   : {order.amount_str} {order.currency}",
        f"Method   : {order.provider}",
        f"Date     : {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC",
        "",
        "Items",
        "-----",
    ]
    lines.extend(items)
    lines.append("")
    if product.guarantee:
        lines.extend(["Guarantee", "---------", product.guarantee, ""])
    lines.append("Keep this file safe. Do not share these items with anyone.")

    content = "\n".join(lines).encode("utf-8")
    return BufferedInputFile(content, filename=f"order-{order.id[:8]}.txt")


def delivery_caption(order: Order, product: Product) -> str:
    return (
        f"🎉 <b>Payment received</b>\n\n"
        f"<b>{product.title}</b> × {len(order.delivered_list())}\n"
        f"Order #{order.id[:8]}\n\n"
        f"Your items are in the attached file."
    )[:MAX_CAPTION]
