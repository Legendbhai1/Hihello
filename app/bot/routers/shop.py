"""Catalog browsing: /start, the product list and product cards."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.providers.registry import ProviderRegistry
from app.services import catalog

WELCOME = (
    "👋 <b>Welcome to the shop</b>\n\n"
    "Pick a product, then choose how you want to pay: Telegram Stars, "
    "Crypto (@CryptoBot) or OxaPay."
)

def build_router() -> Router:
    router = Router(name="shop")

    @router.message(CommandStart())
    async def on_start(message: Message, providers: ProviderRegistry) -> None:
        if not providers.enabled():
            await message.answer("The shop is not configured yet. Please try again later.")
            return
        await message.answer(WELCOME, reply_markup=keyboards.main_menu())

    @router.message(Command("menu"))
    async def on_menu(message: Message) -> None:
        await message.answer("Main menu:", reply_markup=keyboards.main_menu())

    @router.callback_query(F.data == "menu")
    async def on_menu_cb(callback: CallbackQuery) -> None:
        await callback.message.edit_text("Main menu:", reply_markup=keyboards.main_menu())
        await callback.answer()

    @router.callback_query(F.data == "shop:list")
    async def on_shop_list(
        callback: CallbackQuery, session: AsyncSession, providers: ProviderRegistry
    ) -> None:
        products = await catalog.list_products(session)
        if not products:
            await callback.message.edit_text(
                "No products are available right now.", reply_markup=keyboards.main_menu()
            )
            await callback.answer()
            return
        available = await catalog.stock_counts(session)
        await callback.message.edit_text(
            "<b>🛍 Products</b>\nChoose an item:",
            reply_markup=keyboards.product_list(products, available),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("product:"))
    async def on_product(
        callback: CallbackQuery, session: AsyncSession, providers: ProviderRegistry
    ) -> None:
        product_id = int(callback.data.split(":")[1])
        product = await catalog.get_product(session, product_id)
        if product is None or not product.is_active:
            await callback.answer("This product is no longer available.", show_alert=True)
            return

        available = await catalog.stock_counts(session)
        stock = available.get(product.id, 0)
        await callback.message.edit_text(
            texts.product_card(product, providers.enabled(), stock),
            reply_markup=keyboards.product_detail(product, providers.enabled(), stock > 0),
        )
        await callback.answer()

    return router
