"""Operator commands.

Access is limited to ids listed in ADMIN_IDS. Product changes are made with a
compact command grammar so no extra UI is needed:

    /addproduct sku | Title | Stars price | USD price
    /setstock sku
    <one payload per line>
    /products
    /stats
    /retry <order_id>
    /refund <order_id>
"""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.config import Settings
from app.db.models import Order, OrderStatus
from app.providers.base import ProviderError
from app.providers.registry import ProviderRegistry
from app.providers.stars import StarsProvider
from app.services import catalog
from app.services.orders import retry_delivery

log = logging.getLogger(__name__)

HELP = (
    "<b>🛠 Admin commands</b>\n\n"
    "<code>/addproduct sku | Title | stars_price | usd_price</code>\n"
    "Create or update a product (prices are integers).\n\n"
    "<code>/setstock sku</code>\n"
    "Reply to a message, or send payloads one per line after the command, "
    "to add stock items.\n\n"
    "<code>/products</code> — list products with stock and status\n"
    "<code>/stats</code> — order counts by status\n"
    "<code>/retry &lt;order_id&gt;</code> — retry delivery for a paid order\n"
    "<code>/refund &lt;order_id&gt;</code> — refund a Stars order\n"
)

def _is_admin(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids

def build_router() -> Router:
    router = Router(name="admin")

    @router.message(Command("admin"))
    async def on_admin(message: Message, settings: Settings) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        await message.answer(HELP)

    @router.message(Command("addproduct"))
    async def on_add_product(
        message: Message, command: CommandObject, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        raw = (command.args or "").strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) < 4:
            await message.answer("Usage: /addproduct sku | Title | stars_price | usd_price")
            return
        sku, title, stars_raw, usd_raw = parts[:4]
        try:
            price_stars = int(stars_raw)
            price_usd_cents = round(float(usd_raw) * 100)
        except ValueError:
            await message.answer("Prices must be numbers. USD is entered as dollars, e.g. 4.99")
            return

        product = await catalog.upsert_product(
            session,
            sku=sku,
            title=title,
            price_stars=price_stars,
            price_usd_cents=price_usd_cents,
        )
        await message.answer(
            f"✅ Saved <b>{product.title}</b> (sku <code>{product.sku}</code>)\n"
            f"⭐ {price_stars} Stars · 💵 ${price_usd_cents / 100:.2f}"
        )

    @router.message(Command("setstock"))
    async def on_set_stock(
        message: Message, command: CommandObject, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        args = (command.args or "").strip()
        if not args:
            await message.answer("Usage: /setstock sku (then payloads, one per line)")
            return

        lines = args.splitlines()
        sku = lines[0].strip()
        product = await catalog.get_product_by_sku(session, sku)
        if product is None:
            await message.answer(f"No product with sku <code>{sku}</code>")
            return

        payloads = [line.strip() for line in lines[1:] if line.strip()]
        if not payloads and message.reply_to_message is not None:
            payloads = [
                line.strip()
                for line in (message.reply_to_message.text or "").splitlines()
                if line.strip()
            ]
        if not payloads:
            await message.answer("No stock payloads provided.")
            return

        added = await catalog.add_stock(session, product.id, payloads)
        counts = await catalog.stock_counts(session)
        await message.answer(
            f"✅ Added {added} unit(s) to <b>{product.title}</b>. "
            f"Available now: {counts.get(product.id, 0)}"
        )

    @router.message(Command("products"))
    async def on_products(message: Message, session: AsyncSession, settings: Settings) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        products = await catalog.list_products(session, only_active=False)
        if not products:
            await message.answer("No products yet. Use /addproduct.")
            return
        counts = await catalog.stock_counts(session)
        lines = ["<b>📦 Products</b>"]
        for product in products:
            state = "active" if product.is_active else "inactive"
            lines.append(
                f"• <code>{product.sku}</code> — {product.title} — "
                f"⭐{product.price_stars} / ${product.price_usd_cents / 100:.2f} — "
                f"stock {counts.get(product.id, 0)} — {state}"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("stats"))
    async def on_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        stats = await catalog.sales_stats(session)
        lines = ["<b>📊 Orders by status</b>"]
        for status in OrderStatus:
            lines.append(f"• {texts.STATUS_LABELS.get(status.value, status.value)}: {stats.get(status.value, 0)}")
        await message.answer("\n".join(lines))

    @router.message(Command("retry"))
    async def on_retry(
        message: Message, command: CommandObject, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        order_id = (command.args or "").strip()
        order = await _find_order(session, order_id)
        if order is None:
            await message.answer("Order not found. Pass a full order id.")
            return
        if order.status != OrderStatus.PAID.value:
            await message.answer(f"Order is in status '{order.status}'; only paid orders can be retried.")
            return
        ok = await retry_delivery(session, order)
        if ok:
            await message.answer(f"✅ Delivered order #{order.id[:8]}.")
            try:
                await message.bot.send_message(
                    order.user_id,
                    texts.delivery_message(order, product_title=f"order #{order.id[:8]}"),
                )
            except Exception:
                log.exception("could not notify user %s about order %s", order.user_id, order.id)
        else:
            await message.answer("Still no stock available for this order.")

    @router.message(Command("refund"))
    async def on_refund(
        message: Message,
        command: CommandObject,
        session: AsyncSession,
        settings: Settings,
        providers: ProviderRegistry,
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        order = await _find_order(session, (command.args or "").strip())
        if order is None:
            await message.answer("Order not found. Pass a full order id.")
            return
        if order.provider != "stars":
            await message.answer(
                "Only Telegram Stars orders can be refunded from the bot. "
                "Refund Crypto Pay / OxaPay payments from the provider dashboard."
            )
            return
        provider = providers.get("stars")
        if not isinstance(provider, StarsProvider):
            await message.answer("Stars provider is not configured.")
            return
        try:
            await provider.refund(order)
        except ProviderError as exc:
            await message.answer(f"⚠️ Refund failed: {exc}")
            return
        order.status = OrderStatus.REFUNDED.value
        await session.flush()
        await message.answer(f"↩️ Refunded Stars for order #{order.id[:8]}.")

    async def _find_order(session: AsyncSession, order_id: str) -> Order | None:
        if not order_id:
            return None
        return await session.get(Order, order_id)

    return router
