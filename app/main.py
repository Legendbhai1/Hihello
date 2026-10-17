"""Application entrypoint.

Runs the bot and the callback HTTP server in one event loop:

* ``BOT_MODE=polling`` — long polling plus the callback server on HTTP_PORT.
* ``BOT_MODE=webhook`` — Telegram updates are posted to
  ``/telegram/webhook/<WEBHOOK_SECRET>`` on the same server.

``build_application`` assembles every component without touching the network, so
the wiring can be booted and inspected in tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
from collections.abc import Awaitable

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.bot.dispatcher import create_bot, create_dispatcher
from app.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory, init_models
from app.logging_config import configure_logging
from app.providers.registry import ProviderRegistry, build_registry
from app.services.notifier import PaymentNotifier
from app.web.reconciler import Reconciler
from app.web.server import WebhookServer

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="Open the shop"),
    BotCommand(command="menu", description="Main menu"),
    BotCommand(command="orders", description="My orders"),
]


@dataclasses.dataclass
class AppContext:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    bot: Bot
    registry: ProviderRegistry
    dispatcher: Dispatcher
    notifier: PaymentNotifier
    web_app: web.Application
    reconciler: Reconciler

    async def aclose(self) -> None:
        await self.reconciler.stop()
        await self.bot.session.close()
        await self.engine.dispose()


async def build_application(settings: Settings) -> AppContext:
    settings.validate_runtime()

    engine = create_engine(settings.database_url)
    await init_models(engine)
    session_factory = create_session_factory(engine)

    bot = create_bot(settings)
    registry = build_registry(settings, bot)
    dispatcher = create_dispatcher(
        settings=settings, session_factory=session_factory, registry=registry
    )
    notifier = PaymentNotifier(
        bot=bot, session_factory=session_factory, admin_ids=settings.admin_ids
    )

    server = WebhookServer(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        bot=bot,
        dispatcher=dispatcher if settings.bot_mode == "webhook" else None,
        on_result=notifier,
    )
    reconciler = Reconciler(
        settings=settings,
        session_factory=session_factory,
        registry=registry,
        on_result=notifier,
    )
    return AppContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        bot=bot,
        registry=registry,
        dispatcher=dispatcher,
        notifier=notifier,
        web_app=server.build_app(),
        reconciler=reconciler,
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    ctx = await build_application(settings)
    runner = web.AppRunner(ctx.web_app)
    await runner.setup()
    site = web.TCPSite(runner, settings.http_host, settings.http_port)
    await site.start()
    log.info("callback server listening on %s:%s", settings.http_host, settings.http_port)

    stop_event = asyncio.Event()

    def _request_stop(*_: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        ctx.reconciler.start()
        allowed_updates = ctx.dispatcher.resolve_used_update_types()
        if settings.bot_mode == "webhook":
            await ctx.bot.set_webhook(
                settings.telegram_webhook_url,
                allowed_updates=allowed_updates,
                drop_pending_updates=True,
            )
            log.info("webhook mode: Telegram -> %s", settings.telegram_webhook_url)
            # Command registration is cosmetic; never let it block startup.
            await _try_set_commands(ctx)
            await stop_event.wait()
        else:
            await _best_effort(
                ctx.bot.delete_webhook(drop_pending_updates=True),
                "delete webhook",
            )
            await _try_set_commands(ctx)
            log.info("polling mode: starting long polling")
            polling = asyncio.create_task(
                ctx.dispatcher.start_polling(ctx.bot, allowed_updates=allowed_updates)
            )
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {polling, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    finally:
        log.info("shutting down")
        if settings.bot_mode == "webhook":
            try:
                await ctx.bot.delete_webhook()
            except Exception:
                log.exception("could not remove webhook")
        await runner.cleanup()
        await ctx.aclose()


async def _try_set_commands(ctx: AppContext) -> None:
    await _best_effort(ctx.bot.set_my_commands(BOT_COMMANDS), "register bot commands")


async def _best_effort(awaitable: Awaitable[object], what: str) -> None:
    """Run a non-essential Telegram call without aborting startup.

    A bad token still fails loudly: long polling raises the auth error itself.
    """
    try:
        await awaitable
    except Exception:
        log.warning("could not %s (non-fatal)", what, exc_info=True)


def run() -> None:
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    run()
