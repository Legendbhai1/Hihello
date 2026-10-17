"""FSM states for multi-step interactions."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class BuyFlow(StatesGroup):
    """Buyer is entering a quantity as text."""

    quantity = State()


class AdminFlow(StatesGroup):
    """Operator is entering a value for one product field."""

    product_field = State()
    new_product = State()
    add_stock = State()
    category_name = State()
    category_emoji = State()
