"""Buyer-facing shop flow.

    /start  ->  category list  ->  product list  ->  product card
            ->  quantity  ->  Continue  ->  payment method  ->  invoice

The product card keeps its stock count fresh by editing the message every few
seconds while the buyer looks at it.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.bot.states import BuyFlow
from app.config import Settings
from app.db.models import Category, Product
from app.providers.registry import ProviderRegistry
from app.services import catalog
from app.services.checkout import CheckoutError, start_checkout
from app.services.stock_watcher import StockWatcher, WatcherRegistry

log = logging.getLogger(__name__)

WELCOME = (
    "👋 <b>Welcome to the shop!</b>\n\n"
    "Browse the catalog, pick an item, and pay with Telegram Stars or crypto. "
    "Digital items are delivered instantly as a file.\n\n"
    "Use the buttons below to get started."
)

MAX_CAPTION = 1024


def build_router() -> Router:
    router = Router(name="shop")

    async def _category_products_in(session: AsyncSession) -> dict[int, int]:
        """Active product count per category id."""
        products = await catalog.list_products(session, only_active=True)
        counts: dict[int, int] = {}
        for product in products:
            if product.category_id is not None:
                counts[product.category_id] = counts.get(product.category_id, 0) + 1
        return counts

    async def _send_catalog(message: Message, session: AsyncSession) -> None:
        categories = await catalog.list_categories(session, only_active=True)
        if not categories:
            await _send_all_products(message, session)
            return
        counts = await _category_products_in(session)
        await message.answer(
            "<b>🗂 Categories</b>\n\nPick a category:",
            reply_markup=keyboards.category_list(categories, products_in=counts),
        )

    async def _send_all_products(message: Message, session: AsyncSession) -> None:
        products = await catalog.list_products(session, only_active=True)
        if not products:
            await message.answer(
                "The shop is empty right now. Please check back soon.",
                reply_markup=keyboards.main_menu(),
            )
            return
        available = await catalog.stock_counts(session)
        await message.answer(
            "<b>🛍 Products</b>\n\nPick an item to see details:",
            reply_markup=keyboards.product_list(products, available, back_to_categories=False),
        )

    # ------------------------------------------------------------- entry

    @router.message(CommandStart())
    async def on_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
        await state.clear()
        await _send_catalog(message, session)
        await message.answer(WELCOME, reply_markup=keyboards.main_menu())

    @router.message(Command("menu"))
    async def on_menu(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("<b>Main menu</b>", reply_markup=keyboards.main_menu())

    @router.message(Command("shop"))
    async def on_shop(message: Message, session: AsyncSession, state: FSMContext) -> None:
        await state.clear()
        await _send_catalog(message, session)

    @router.callback_query(F.data == "m")
    async def on_menu_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.message.edit_text("<b>Main menu</b>", reply_markup=keyboards.main_menu())
        await callback.answer()

    @router.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    # ------------------------------------------------------------ catalog

    @router.callback_query(F.data == "c")
    async def on_categories(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        await state.clear()
        categories = await catalog.list_categories(session, only_active=True)
        if not categories:
            products = await catalog.list_products(session, only_active=True)
            if not products:
                await callback.answer("The shop is empty right now.", show_alert=True)
                return
            available = await catalog.stock_counts(session)
            await callback.message.edit_text(
                "<b>🛍 Products</b>\n\nPick an item to see details:",
                reply_markup=keyboards.product_list(
                    products, available, back_to_categories=False
                ),
            )
            await callback.answer()
            return
        counts = await _category_products_in(session)
        await callback.message.edit_text(
            "<b>🗂 Categories</b>\n\nPick a category:",
            reply_markup=keyboards.category_list(categories, products_in=counts),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("c:"))
    async def on_category(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        await state.clear()
        category_id = int(callback.data.split(":", 1)[1])
        category: Category | None = await catalog.get_category(session, category_id)
        if category is None:
            await callback.answer("That category no longer exists.", show_alert=True)
            return
        products = await catalog.list_products(
            session, only_active=True, category_id=category_id
        )
        if not products:
            await callback.answer("No products in this category yet.", show_alert=True)
            return
        available = await catalog.stock_counts(session)
        await callback.message.edit_text(
            f"<b>{texts.escape(category.emoji)} {texts.escape(category.name)}</b>\n\n"
            "Pick an item to see details:",
            reply_markup=keyboards.product_list(products, available),
        )
        await callback.answer()

    # ------------------------------------------------------- product card

    async def _render_card(
        session: AsyncSession,
        product: Product,
        *,
        quantity: int = 1,
        providers: list[str],
    ) -> tuple[str, object]:
        stock = await catalog.stock_count(session, product.id)
        category = None
        if product.category_id is not None:
            category = await catalog.get_category(session, product.category_id)
        text = texts.product_card(
            product,
            providers,
            stock,
            category_name=category.name if category else None,
        )
        markup = keyboards.product_detail(product, providers, stock, quantity)
        return text[:MAX_CAPTION], markup

    @router.callback_query(F.data.startswith("p:"))
    async def on_product(
        callback: CallbackQuery,
        session: AsyncSession,
        state: FSMContext,
        providers: ProviderRegistry,
        stock_watchers: WatcherRegistry,
    ) -> None:
        await state.clear()
        product_id = int(callback.data.split(":", 1)[1])
        product = await catalog.get_product(session, product_id)
        if product is None or not product.is_active:
            await callback.answer("That product is no longer available.", show_alert=True)
            return

        enabled = providers.enabled()
        text, markup = await _render_card(session, product, providers=enabled)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()

        # Keep the stock count live while the buyer reads the card.
        watcher = StockWatcher(
            bot=callback.bot,
            session_factory=session.info["session_factory"],
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            product_id=product.id,
            render=lambda count: _render_card_for(product, enabled, count),
        )
        stock_watchers.add(watcher)

    def _render_card_for(
        product: Product, providers: list[str], stock: int
    ) -> tuple[str, object]:
        """Synchronous render used by the watcher with a pre-fetched count."""
        text = texts.product_card(product, providers, stock)
        return text[:MAX_CAPTION], keyboards.product_detail(product, providers, stock, 1)

    # ---------------------------------------------------------- quantity

    @router.callback_query(F.data.startswith("q:"))
    async def on_quantity(
        callback: CallbackQuery, session: AsyncSession, providers: ProviderRegistry
    ) -> None:
        _, raw_id, raw_qty = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None or not product.is_active:
            await callback.answer("That product is no longer available.", show_alert=True)
            return

        stock = await catalog.stock_count(session, product.id)
        quantity = max(1, min(int(raw_qty), max(1, product.max_per_order), max(1, stock)))
        enabled = providers.enabled()
        text, markup = await _render_card(session, product, quantity=quantity, providers=enabled)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer()

    @router.callback_query(F.data.startswith("buy:"))
    async def on_buy_quantity_prompt(
        callback: CallbackQuery, session: AsyncSession, state: FSMContext
    ) -> None:
        """Legacy entry point: ask the buyer for a quantity as a typed number."""
        product_id = int(callback.data.split(":", 1)[1])
        product = await catalog.get_product(session, product_id)
        if product is None or not product.is_active:
            await callback.answer("That product is no longer available.", show_alert=True)
            return
        await state.set_state(BuyFlow.quantity)
        await state.update_data(product_id=product.id)
        await callback.message.answer(
            f"How many <b>{texts.escape(product.title)}</b> would you like?\n"
            f"Send a number from 1 to {product.max_per_order}."
        )
        await callback.answer()

    @router.message(BuyFlow.quantity)
    async def on_quantity_text(
        message: Message, session: AsyncSession, state: FSMContext, providers: ProviderRegistry
    ) -> None:
        data = await state.get_data()
        product = await catalog.get_product(session, int(data.get("product_id", 0)))
        if product is None:
            await state.clear()
            await message.answer("That product is no longer available.")
            return

        raw = (message.text or "").strip()
        if not raw.isdigit():
            await message.answer("Please send a whole number, for example 2.")
            return

        stock = await catalog.stock_count(session, product.id)
        limit = min(max(1, product.max_per_order), max(1, stock))
        quantity = max(1, min(int(raw), limit))
        if int(raw) > limit:
            await message.answer(
                f"Only {limit} available, so I set the quantity to {limit}."
            )
        await state.clear()

        enabled = providers.enabled()
        text, markup = await _render_card(session, product, quantity=quantity, providers=enabled)
        await message.answer(text, reply_markup=markup)

    # ---------------------------------------------------- payment choice

    @router.callback_query(F.data.startswith("b:"))
    async def on_continue(
        callback: CallbackQuery, session: AsyncSession, providers: ProviderRegistry
    ) -> None:
        _, raw_id, raw_qty = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None or not product.is_active:
            await callback.answer("That product is no longer available.", show_alert=True)
            return

        stock = await catalog.stock_count(session, product.id)
        if stock <= 0:
            await callback.answer("Sorry, this item just sold out.", show_alert=True)
            return

        quantity = max(1, min(int(raw_qty), max(1, product.max_per_order), stock))
        enabled = providers.enabled()
        if not enabled:
            await callback.answer("No payment methods are configured.", show_alert=True)
            return

        total_stars = product.price_stars * quantity
        total_usd = product.price_usd_cents * quantity
        await callback.message.edit_text(
            f"<b>{texts.escape(product.title)}</b> × {quantity}\n\n"
            f"Total: ⭐ {total_stars} Stars · 💵 {texts.usd(total_usd)}\n\n"
            "Choose how you would like to pay:",
            reply_markup=keyboards.payment_methods(product, enabled, quantity),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("pm:"))
    async def on_pay_method(
        callback: CallbackQuery,
        session: AsyncSession,
        settings: Settings,
        providers: ProviderRegistry,
        state: FSMContext,
    ) -> None:
        _, raw_id, raw_qty, provider_name = callback.data.split(":")
        product = await catalog.get_product(session, int(raw_id))
        if product is None or not product.is_active:
            await callback.answer("That product is no longer available.", show_alert=True)
            return

        await state.clear()
        await callback.answer("Creating your order…")
        try:
            order = await start_checkout(
                session,
                settings=settings,
                registry=providers,
                user_id=callback.from_user.id,
                product=product,
                provider_name=provider_name,
                quantity=int(raw_qty),
            )
        except CheckoutError as exc:
            await callback.message.answer(f"⚠️ {exc}")
            return

        await callback.message.answer(
            texts.order_card(order, product_title=product.title)
            + "\n\nComplete the payment with the button below. "
            "Your file is sent automatically once the payment is confirmed.",
            reply_markup=keyboards.pay_now(order),
        )

    @router.callback_query(F.data.startswith("notify:"))
    async def on_notify(callback: CallbackQuery) -> None:
        await callback.answer(
            "We do not have automatic restock alerts yet. Please check back soon.",
            show_alert=True,
        )

    return router
