"""Background reconciliation.

Webhooks can be lost, so open orders are periodically re-checked against the
provider. This is the safety net that turns a missed callback into a delivery,
and it also expires orders that were never paid.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.providers.base import PaymentUpdate, ProviderError
from app.providers.registry import ProviderRegistry
from app.services.orders import (
    apply_payment_update,
    expire_stale_orders,
    open_orders_for_reconciliation,
    record_event,
)
from app.web.types import NotifyCallback

log = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        on_result: NotifyCallback | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._registry = registry
        self._on_result = on_result
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="reconciler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        interval = max(15, self._settings.reconcile_interval_seconds)
        log.info("reconciler started (every %ss)", interval)
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconciliation tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def tick(self) -> None:
        async with self._session_factory() as session:
            expired = await expire_stale_orders(session)
            await session.commit()
        if expired:
            log.info("expired %s stale order(s)", expired)

        async with self._session_factory() as session:
            orders = await open_orders_for_reconciliation(
                session, min_age_seconds=self._settings.reconcile_min_age_seconds
            )
        if not orders:
            return

        log.info("reconciling %s open order(s)", len(orders))
        for order in orders:
            provider = self._registry.get(order.provider)
            if provider is None:
                continue
            try:
                update = await provider.check_status(order)
            except ProviderError as exc:
                log.warning("reconcile order %s: %s", order.id, exc)
                continue
            if update is None:
                continue
            await self._apply(update)

    async def _apply(self, update: PaymentUpdate) -> None:
        async with self._session_factory() as session:
            fresh = await record_event(
                session, update, raw_body=f"reconcile:{update.event_id}", signature_valid=True
            )
            if not fresh:
                await session.commit()
                return
            order, outcome = await apply_payment_update(session, update)
            await session.commit()
        if order is not None and outcome in {"delivered", "no_stock"} and self._on_result:
            try:
                await self._on_result(order, outcome)
            except Exception:
                log.exception("post-payment notification failed for order %s", order.id)
