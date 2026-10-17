"""Buyer order history."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards, texts
from app.db.models import Order, OrderStatus, Product
from app.services import catalog, delivery


async def _user_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


def build_router() -> Router:
    router = Router(name="orders")

    @router.message(Command("orders"))
    async def on_orders(message: Message, session: AsyncSession) -> None:
        orders = await _user_orders(session, message.from_user.id)
        if not orders:
            await message.answer("You have no orders yet.", reply_markup=keyboards.main_menu())
            return
        await message.answer("<b>🧾 Your orders</b>", reply_markup=keyboards.order_list(orders))

    @router.callback_query(F.data == "ol")
    async def on_orders_cb(callback: CallbackQuery, session: AsyncSession) -> None:
        orders = await _user_orders(session, callback.from_user.id)
        if not orders:
            await callback.message.edit_text(
                "You have no orders yet.", reply_markup=keyboards.main_menu()
            )
            await callback.answer()
            return
        await callback.message.edit_text(
            "<b>🧾 Your orders</b>", reply_markup=keyboards.order_list(orders)
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("o:"))
    async def on_order_detail(callback: CallbackQuery, session: AsyncSession) -> None:
        order = await session.get(Order, callback.data.split(":", 1)[1])
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return

        product = await session.get(Product, order.product_id)
        await callback.message.edit_text(
            texts.order_card(order, product_title=product.title if product else None),
            reply_markup=(
                keyboards.pay_now(order) if order.is_open() else keyboards.main_menu()
            ),
        )
        await callback.answer()

    @router.callback_query(F.data.startswith("r:"))
    async def on_resend(callback: CallbackQuery, session: AsyncSession) -> None:
        """Re-send a delivered order's file."""
        order = await session.get(Order, callback.data.split(":", 1)[1])
        if order is None or order.user_id != callback.from_user.id:
            await callback.answer("Order not found.", show_alert=True)
            return
        if order.status != OrderStatus.DELIVERED.value:
            await callback.answer("This order has not been delivered yet.", show_alert=True)
            return
        product = await catalog.get_product(session, order.product_id)
        if product is None:
            await callback.answer("This product no longer exists.", show_alert=True)
            return
        await callback.message.answer_document(
            delivery.build_delivery_file(order, product),
            caption=delivery.delivery_caption(order, product),
        )
        await callback.answer()

    return router
