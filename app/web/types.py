"""Shared types for the web layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.db.models import Order

# Called after an order reaches a post-payment state, to notify the buyer.
NotifyCallback = Callable[[Order, str], Awaitable[None]]
