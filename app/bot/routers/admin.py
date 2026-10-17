"""Admin control panel: everything an operator needs to run the store.

``/panel`` opens an inline menu covering products (create, edit every field,
show/hide, delete), stock (restock, inspect, clear), categories and stats.
Text entry is handled through the :class:`AdminFlow` states so the panel stays
button-driven.

Only ids listed in ``ADMIN_IDS`` may use any of this.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.states import AdminFlow
from app.config import Settings
from app.db.models import Order, OrderStatus, Product
from app.services import catalog

log = logging.getLogger(__name__)

# Which product fields are text vs numeric vs a picker.
TEXT_FIELDS = {"title", "description", "features", "guarantee"}
INT_FIELDS = {"price_stars", "max"}
FIELD_LABELS = {
    "title": "title",
    "description": "description",
    "features": "features",
    "guarantee": "guarantee",
    "price_stars": "Stars price",
    "price_usd": "USD price",
    "max": "max per order",
}

HELP = (
    "<b>🛠 Admin</b>\n\n"
    "/panel — open the store control panel\n"
    "/stats — quick order counts\n"
    "/id — show your Telegram user id\n"
)

PANEL_TEXT = (
    "<b>🏪 Store panel</b>\n\n"
    "Manage products, stock, categories and see how the store is doing."
)


def _is_admin(settings: Settings, user_id: int | None) -> bool:
    return user_id is not None and user_id in settings.admin_ids


def build_router() -> Router:
    router = Router(name="admin")

    # ------------------------------------------------------------- entry

    @router.message(Command("id"))
    async def on_id(message: Message) -> None:
        await message.answer(f"Your Telegram user id: <code>{message.from_user.id}</code>")

    @router.message(Command("admin"))
    async def on_admin(message: Message, settings: Settings) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        await message.answer(HELP)

    @router.message(Command("panel"))
    async def on_panel(message: Message, settings: Settings, state: FSMContext) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        await state.clear()
        await message.answer(PANEL_TEXT, reply_markup=keyboards.panel_menu())

    @router.callback_query(F.data == "apanel")
    async def on_panel_cb(
        callback: CallbackQuery, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text(PANEL_TEXT, reply_markup=keyboards.panel_menu())
        await callback.answer()

    @router.callback_query(F.data == "aclose")
    async def on_close(callback: CallbackQuery, settings: Settings, state: FSMContext) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await state.clear()
        await callback.message.edit_text("Panel closed. Send /panel to reopen.")
        await callback.answer()

    # ---------------------------------------------------------- products

    async def _show_products(callback: CallbackQuery, session: AsyncSession) -> None:
        products = await catalog.list_products(session, only_active=False)
        if not products:
            await callback.message.edit_text(
                "No products yet. Create your first one.",
                reply_markup=keyboards.panel_products([], {}),
            )
            return
        available = await catalog.stock_counts(session)
        await callback.message.edit_text(
            "<b>📦 Products</b>\n\nTap a product to edit it:",
            reply_markup=keyboards.panel_products(products, available),
        )

    @router.callback_query(F.data == "ap:list")
    async def on_product_list(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await _show_products(callback, session)
        await callback.answer()

    async def _render_product(
        callback: CallbackQuery, session: AsyncSession, product: Product
    ) -> None:
        stock = await catalog.stock_count(session, product.id)
        category_name = None
        if product.category_id is not None:
            category = await catalog.get_category(session, product.category_id)
            category_name = f"{category.emoji} {category.name}".strip() if category else None
        features = product.feature_list()
        lines = [
            f"<b>{texts.escape(product.title)}</b>",
            f"<code>{product.sku}</code>",
            f"Category: {texts.escape(category_name) if category_name else '—'}",
            f"Status: {'active' if product.is_active else 'hidden'}",
            "",
            f"Description: {texts.escape(product.description) or '—'}",
            f"Features: {len(features)} line(s)",
            f"Guarantee: {texts.escape(product.guarantee) or '—'}",
            "",
            f"⭐ {product.price_stars} Stars · 💵 {texts.usd(product.price_usd_cents)}",
            f"Max per order: {product.max_per_order}",
            f"Stock: {stock}",
        ]
        await callback.message.edit_text(
            "\n".join(lines), reply_markup=keyboards.panel_product(product, stock)
        )

    @router.callback_query(F.data.startswith("ap:v:"))
    async def on_product_view(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        await _render_product(callback, session, product)
        await callback.answer()

    @router.callback_query(F.data == "ap:new")
    async def on_new_product(
        callback: CallbackQuery, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await state.set_state(AdminFlow.new_product)
        await callback.message.answer(
            "Send the new product as one line:\n\n"
            "<code>sku | Title | stars price | usd price</code>\n\n"
            "Example: <code>netflix-1m | Netflix 1 Month | 250 | 4.99</code>\n\n"
            "You can edit description, features, guarantee and category afterwards.",
            reply_markup=keyboards.cancel_edit(),
        )
        await callback.answer()

    @router.message(AdminFlow.new_product)
    async def on_new_product_text(
        message: Message, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        parts = [part.strip() for part in (message.text or "").split("|")]
        if len(parts) < 4:
            await message.answer(
                "That needs four parts separated by <code>|</code>:\n"
                "<code>sku | Title | stars | usd</code>"
            )
            return
        sku, title, stars_raw, usd_raw = parts[:4]
        try:
            price_stars = int(stars_raw)
            price_usd_cents = int((Decimal(usd_raw) * 100).to_integral_value())
        except (ValueError, InvalidOperation):
            await message.answer("Stars must be a whole number and USD a number like 4.99.")
            return
        if await catalog.get_product_by_sku(session, sku) is not None:
            await message.answer(f"A product with sku <code>{sku}</code> already exists.")
            return

        product = await catalog.upsert_product(
            session,
            sku=sku,
            title=title,
            price_stars=price_stars,
            price_usd_cents=price_usd_cents,
        )
        await session.commit()
        await state.clear()
        await message.answer(
            f"✅ Created <b>{texts.escape(product.title)}</b> (<code>{product.sku}</code>).\n"
            "Open it in /panel to add description, features, guarantee and stock."
        )

    @router.callback_query(F.data.startswith("ap:e:"))
    async def on_edit_field(
        callback: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        _, _, field, raw_id = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return

        if field == "category":
            categories = await catalog.list_categories(session, only_active=False)
            if not categories:
                await callback.answer("Create a category first.", show_alert=True)
                return
            await callback.message.edit_text(
                "Pick a category:",
                reply_markup=keyboards.pick_category(categories, product.id),
            )
            await callback.answer()
            return

        prompts = {
            "title": "Send the new title.",
            "description": "Send the new description.",
            "features": "Send the features, one per line.",
            "guarantee": "Send the guarantee text.",
            "price_stars": "Send the Stars price as a whole number.",
            "price_usd": "Send the USD price, for example 4.99",
            "max": "Send the maximum quantity per order.",
        }
        await state.set_state(AdminFlow.product_field)
        await state.update_data(product_id=product.id, field=field)
        await callback.message.edit_text(
            f"<b>{FIELD_LABELS.get(field, field)}</b>\n\n"
            f"Current: <code>{_current_value(product, field)}</code>\n\n"
            f"{prompts.get(field, 'Send the new value.')}",
            reply_markup=keyboards.cancel_edit(product_id=product.id),
        )
        await callback.answer()

    @router.message(AdminFlow.product_field)
    async def on_field_value(
        message: Message, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        data = await state.get_data()
        product = await catalog.get_product(session, int(data.get("product_id", 0)))
        field = str(data.get("field", ""))
        if product is None:
            await state.clear()
            await message.answer("Product not found.")
            return

        raw = (message.text or "").strip()
        try:
            if field in TEXT_FIELDS:
                value: object = raw
            elif field == "price_stars":
                value = int(raw)
            elif field == "price_usd":
                value = int((Decimal(raw) * 100).to_integral_value())
            elif field == "max":
                value = max(1, int(raw))
            else:
                await state.clear()
                await message.answer("That field cannot be edited.")
                return
        except (ValueError, InvalidOperation):
            await message.answer("That value is not valid. Try again.")
            return

        await catalog.update_product(session, product, **{field: value})
        await session.commit()
        await state.clear()
        shown = texts.usd(product.price_usd_cents) if field == "price_usd" else raw
        await message.answer(
            f"✅ <b>{FIELD_LABELS.get(field, field)}</b> updated to <code>{shown}</code>."
        )

    @router.callback_query(F.data.startswith("ap:setcat:"))
    async def on_set_category(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        _, _, raw_cat, raw_id = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        category_id = int(raw_cat) or None
        await catalog.update_product(session, product, category_id=category_id)
        await session.commit()
        await _render_product(callback, session, product)
        await callback.answer("Category updated.")

    @router.callback_query(F.data.startswith("ap:toggle:"))
    async def on_toggle_product(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        await catalog.update_product(session, product, is_active=not product.is_active)
        await session.commit()
        await _render_product(callback, session, product)
        await callback.answer("Product is now " + ("active." if product.is_active else "hidden."))

    @router.callback_query(F.data.startswith("ap:del:"))
    async def on_delete_product(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        await callback.message.edit_text(
            f"Delete <b>{texts.escape(product.title)}</b> and all its unsold stock?\n"
            "Orders already placed keep their delivered items.",
            reply_markup=keyboards.confirm(f"ap:delok:{product.id}", f"ap:v:{product.id}"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ap:delok:"))
    async def on_delete_product_confirm(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        try:
            await catalog.delete_product(session, product)
        except catalog.ProductHasOrders:
            await callback.message.edit_text(
                f"<b>{texts.escape(product.title)}</b> has orders against it, so it "
                "cannot be deleted.\n\nHide it instead — buyers stop seeing it but the "
                "order history stays intact.",
                reply_markup=keyboards.panel_product(product, 0),
            )
            await callback.answer("Product has orders.", show_alert=True)
            return
        await session.commit()
        await _show_products(callback, session)
        await callback.answer("Product deleted.")

    # ------------------------------------------------------------- stock

    @router.callback_query(F.data.startswith("ap:stock:"))
    async def on_add_stock(
        callback: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        await state.set_state(AdminFlow.add_stock)
        await state.update_data(product_id=product.id)
        await callback.message.edit_text(
            f"Send the stock for <b>{texts.escape(product.title)}</b>, one item per line.\n\n"
            "Each line is one deliverable unit, for example a license key or a link.",
            reply_markup=keyboards.cancel_edit(product_id=product.id),
        )
        await callback.answer()

    @router.message(AdminFlow.add_stock)
    async def on_add_stock_text(
        message: Message, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        data = await state.get_data()
        product = await catalog.get_product(session, int(data.get("product_id", 0)))
        if product is None:
            await state.clear()
            await message.answer("Product not found.")
            return
        payloads = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
        if not payloads:
            await message.answer("Send at least one line.")
            return
        added = await catalog.add_stock(session, product.id, payloads)
        await session.commit()
        await state.clear()
        total = await catalog.stock_count(session, product.id)
        await message.answer(
            f"✅ Added {added} unit(s) to <b>{texts.escape(product.title)}</b>.\n"
            f"Available now: {total}"
        )

    @router.callback_query(F.data.startswith("ap:view:"))
    async def on_view_stock(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        items = await catalog.list_stock(session, product.id, limit=20)
        if not items:
            await callback.answer("No stock available.", show_alert=True)
            return
        listing = "\n".join(f"{item.id}: <code>{texts.escape(item.payload)}</code>" for item in items)
        await callback.message.edit_text(
            f"<b>Stock for {texts.escape(product.title)}</b>\n\n{listing}",
            reply_markup=keyboards.cancel_edit(product_id=product.id),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ap:clear:"))
    async def on_clear_stock(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        await callback.message.edit_text(
            f"Delete all unsold stock for <b>{texts.escape(product.title)}</b>?",
            reply_markup=keyboards.confirm(f"ap:clearok:{product.id}", f"ap:v:{product.id}"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ap:clearok:"))
    async def on_clear_stock_confirm(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        product = await catalog.get_product(session, int(callback.data.split(":")[2]))
        if product is None:
            await callback.answer("Product not found.", show_alert=True)
            return
        removed = await catalog.clear_available_stock(session, product.id)
        await session.commit()
        await _render_product(callback, session, product)
        await callback.answer(f"Removed {removed} unit(s).")

    # -------------------------------------------------------- categories

    async def _show_categories(callback: CallbackQuery, session: AsyncSession) -> None:
        categories = await catalog.list_categories(session, only_active=False)
        products = await catalog.list_products(session, only_active=False)
        counts: dict[int, int] = {}
        for product in products:
            if product.category_id is not None:
                counts[product.category_id] = counts.get(product.category_id, 0) + 1
        await callback.message.edit_text(
            "<b>🗂 Categories</b>\n\nTap a category to edit it:",
            reply_markup=keyboards.panel_categories(categories, counts),
        )

    @router.callback_query(F.data == "ac:list")
    async def on_category_list(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await _show_categories(callback, session)
        await callback.answer()

    @router.callback_query(F.data == "ac:new")
    async def on_new_category(
        callback: CallbackQuery, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await state.set_state(AdminFlow.category_name)
        await callback.message.answer(
            "Send the category name. You can prefix an emoji, for example "
            "<code>🎬 Streaming</code>.",
            reply_markup=keyboards.cancel_edit(),
        )
        await callback.answer()

    @router.message(AdminFlow.category_name)
    async def on_category_name(
        message: Message, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        raw = (message.text or "").strip()
        if not raw:
            await message.answer("Send a name.")
            return
        emoji, name = _split_emoji(raw)
        category = await catalog.upsert_category(session, name=name, emoji=emoji)
        await session.commit()
        await state.clear()
        await message.answer(
            f"✅ Category <b>{texts.escape(category.emoji)} "
            f"{texts.escape(category.name)}</b> saved."
        )

    @router.callback_query(F.data.startswith("ac:v:"))
    async def on_category_view(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        category = await catalog.get_category(session, int(callback.data.split(":")[2]))
        if category is None:
            await callback.answer("Category not found.", show_alert=True)
            return
        await callback.message.edit_text(
            f"<b>{texts.escape(category.emoji)} {texts.escape(category.name)}</b>\n"
            f"<code>{category.slug}</code>",
            reply_markup=keyboards.panel_category(category),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ac:e:"))
    async def on_category_edit(
        callback: CallbackQuery, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        _, _, field, raw_id = callback.data.split(":")
        category = await catalog.get_category(session, int(raw_id))
        if category is None:
            await callback.answer("Category not found.", show_alert=True)
            return
        state_name = (
            AdminFlow.category_name if field == "name" else AdminFlow.category_emoji
        )
        await state.set_state(state_name)
        await state.update_data(category_id=category.id)
        await callback.message.edit_text(
            "Send the new name." if field == "name" else "Send the emoji.",
            reply_markup=keyboards.cancel_edit(category_id=category.id),
        )
        await callback.answer()

    @router.message(AdminFlow.category_emoji)
    async def on_category_emoji(
        message: Message, session: AsyncSession, settings: Settings, state: FSMContext
    ) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        data = await state.get_data()
        category = await catalog.get_category(session, int(data.get("category_id", 0)))
        if category is None:
            await state.clear()
            await message.answer("Category not found.")
            return
        category.emoji = (message.text or "").strip()
        await session.commit()
        await state.clear()
        await message.answer("✅ Emoji updated.")

    @router.callback_query(F.data.startswith("ac:del:"))
    async def on_delete_category(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        category = await catalog.get_category(session, int(callback.data.split(":")[2]))
        if category is None:
            await callback.answer("Category not found.", show_alert=True)
            return
        await callback.message.edit_text(
            f"Delete <b>{texts.escape(category.name)}</b>?\n"
            "Its products become uncategorised; nothing is deleted.",
            reply_markup=keyboards.confirm(f"ac:delok:{category.id}", f"ac:v:{category.id}"),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("ac:delok:"))
    async def on_delete_category_confirm(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        category = await catalog.get_category(session, int(callback.data.split(":")[2]))
        if category is None:
            await callback.answer("Category not found.", show_alert=True)
            return
        await catalog.delete_category(session, category)
        await session.commit()
        await _show_categories(callback, session)
        await callback.answer("Category deleted.")

    # ------------------------------------------------------------- stats

    @router.message(Command("stats"))
    async def on_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
        if not _is_admin(settings, message.from_user.id):
            return
        await message.answer(await _stats_text(session))

    @router.callback_query(F.data == "as")
    async def on_stats_cb(
        callback: CallbackQuery, session: AsyncSession, settings: Settings
    ) -> None:
        if not _is_admin(settings, callback.from_user.id):
            await callback.answer("Admins only.", show_alert=True)
            return
        await callback.message.edit_text(
            await _stats_text(session),
            reply_markup=keyboards.cancel_edit(),
        )
        await callback.answer()

    async def _stats_text(session: AsyncSession) -> str:
        stats = await catalog.sales_stats(session)
        revenue = await catalog.revenue_minor(session)
        lines = ["<b>📊 Store stats</b>", ""]
        for status in OrderStatus:
            label = texts.STATUS_LABELS.get(status.value, status.value)
            lines.append(f"{label}: {stats.get(status.value, 0)}")
        if revenue:
            lines.append("")
            lines.append("<b>Delivered revenue</b>")
            for currency, total in revenue.items():
                lines.append(f"{currency}: {texts.format_money(total, currency)}")
        recent = list(
            (
                await session.execute(
                    select(Order).order_by(Order.created_at.desc()).limit(5)
                )
            ).scalars()
        )
        if recent:
            lines.append("")
            lines.append("<b>Recent orders</b>")
            for order in recent:
                lines.append(
                    f"#{order.id[:8]} · {order.status} · "
                    f"{order.quantity}× · {texts.format_money(order.amount_minor, order.currency)} "
                    f"{order.currency}"
                )
        return "\n".join(lines)

    return router


def _current_value(product: Product, field: str) -> str:
    if field == "price_usd":
        return texts.usd(product.price_usd_cents)
    if field == "max":
        return str(product.max_per_order)
    if field == "price_stars":
        return str(product.price_stars)
    if field == "features":
        return " / ".join(product.feature_list()) or "—"
    return str(getattr(product, field, "") or "—")


def _split_emoji(raw: str) -> tuple[str, str]:
    """Split a leading emoji off a category label, e.g. "🎬 Streaming"."""
    head, _, tail = raw.partition(" ")
    if tail and not head.isalnum():
        return head, tail.strip()
    return "", raw
