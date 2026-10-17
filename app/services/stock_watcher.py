"""Live stock refresh on a product card.

A product card shows the current stock count and keeps it fresh by editing the
message every few seconds while the buyer is looking at it. Each card owns a
short-lived task; navigating away cancels it, and a maximum lifetime stops a card
the buyer abandoned from polling forever.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = logging.getLogger(__name__)

# Called with the fresh count; returns the new text and markup, or None to stop.
RenderFn = Callable[[int], Awaitable[tuple[str, object] | None]]


class StockWatcher:
    """Edits one message periodically with a fresh stock count."""

    def __init__(
        self,
        *,
        bot: object,
        session_factory: async_sessionmaker[AsyncSession],
        chat_id: int,
        message_id: int,
        product_id: int,
        render: RenderFn,
        interval: float = 5.0,
        max_lifetime: float = 900.0,
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._chat_id = chat_id
        self._message_id = message_id
        self._product_id = product_id
        self._render = render
        self._interval = interval
        self._max_lifetime = max_lifetime
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None and not self._stopped:
            self._task = asyncio.create_task(self._run(), name=f"stock:{self._product_id}")

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        elapsed = 0.0
        try:
            while not self._stopped and elapsed < self._max_lifetime:
                await asyncio.sleep(self._interval)
                elapsed += self._interval
                if self._stopped:
                    return
                await self._tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("stock watcher failed for product %s", self._product_id)

    async def _tick(self) -> None:
        from app.services import catalog

        async with self._session_factory() as session:
            count = await catalog.stock_count(session, self._product_id)
        rendered = await self._render(count)
        if rendered is None:
            self._stopped = True
            return
        text, markup = rendered
        try:
            await self._bot.edit_message_text(  # type: ignore[attr-defined]
                text=text,
                chat_id=self._chat_id,
                message_id=self._message_id,
                reply_markup=markup,
            )
        except Exception as exc:  # noqa: BLE001 - a watcher must never crash the bot
            # "message is not modified" and "message to edit not found" are both
            # expected: the count did not change, or the buyer navigated away.
            if _is_benign_edit_error(exc):
                return
            log.debug("stock watcher edit failed for product %s: %s", self._product_id, exc)


def _is_benign_edit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not modified" in text or "not found" in text or "message can't be edited" in text


class WatcherRegistry:
    """Tracks live watchers so they can all be stopped on shutdown."""

    def __init__(self) -> None:
        self._watchers: dict[tuple[int, int], StockWatcher] = {}

    def add(self, watcher: StockWatcher) -> None:
        key = (watcher._chat_id, watcher._message_id)
        existing = self._watchers.pop(key, None)
        if existing is not None:
            asyncio.create_task(existing.stop())
        self._watchers[key] = watcher
        watcher.start()

    async def stop_all(self) -> None:
        watchers = list(self._watchers.values())
        self._watchers.clear()
        for watcher in watchers:
            await watcher.stop()
