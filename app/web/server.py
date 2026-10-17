"""HTTP server for provider callbacks and the Telegram webhook.

Only Crypto Pay and OxaPay use HTTP callbacks; Telegram Stars updates arrive via
the bot's own update stream. Every callback is answered with a non-2xx status when
the signature is bad, so the provider retries a notification we could not trust.
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.providers.base import PaymentProvider, SignatureError
from app.providers.registry import ProviderRegistry
from app.services.orders import apply_payment_update, record_event
from app.web.types import NotifyCallback

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        registry: ProviderRegistry,
        bot: Bot,
        dispatcher: Dispatcher | None = None,
        on_result: NotifyCallback | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._registry = registry
        self._bot = bot
        self._dispatcher = dispatcher
        self._on_result = on_result

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/health", self._health)
        if self._dispatcher is not None:
            app.router.add_post(
                "/telegram/webhook/{secret}", self._telegram_webhook
            )
        app.router.add_post("/webhooks/cryptopay", self._cryptopay_webhook)
        app.router.add_post("/webhooks/cryptopay/{secret}", self._cryptopay_webhook)
        app.router.add_post("/webhooks/oxapay", self._oxapay_webhook)
        return app

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "providers": self._registry.enabled()}
        )

    async def _telegram_webhook(self, request: web.Request) -> web.Response:
        expected = self._settings.webhook_secret
        if not expected or request.match_info.get("secret") != expected:
            log.warning("telegram webhook: rejected request with bad secret path")
            raise web.HTTPForbidden()
        assert self._dispatcher is not None
        try:
            update = Update.model_validate(await request.json(), context={"bot": self._bot})
        except Exception:
            log.exception("telegram webhook: malformed update")
            raise web.HTTPBadRequest()
        await self._dispatcher.feed_update(self._bot, update)
        return web.Response(text="ok")

    async def _cryptopay_webhook(self, request: web.Request) -> web.Response:
        provider = self._registry.get("cryptopay")
        if provider is None:
            raise web.HTTPNotFound()
        if self._settings.cryptopay_webhook_secret and (
            request.match_info.get("secret") != self._settings.cryptopay_webhook_secret
        ):
            log.warning("cryptopay webhook: bad secret path")
            raise web.HTTPForbidden()
        return await self._handle(request, provider, "cryptopay")

    async def _oxapay_webhook(self, request: web.Request) -> web.Response:
        provider = self._registry.get("oxapay")
        if provider is None:
            raise web.HTTPNotFound()
        return await self._handle(request, provider, "oxapay")

    async def _handle(self, request: web.Request, provider: PaymentProvider, name: str) -> web.Response:
        raw_body = await request.read()
        headers = {k.lower(): v for k, v in request.headers.items()}
        # OxaPay sends the header as "HMAC"; normalise for lookup.
        if "hmac" in headers:
            headers["HMAC"] = headers["hmac"]

        try:
            update = provider.parse_webhook(raw_body, headers)
        except SignatureError as exc:
            log.warning("%s webhook rejected: %s", name, exc)
            # 400 tells the provider the notification was not accepted.
            raise web.HTTPBadRequest(text="invalid signature") from exc
        except Exception:
            log.exception("%s webhook: failed to parse callback", name)
            raise web.HTTPBadRequest(text="bad request")

        if update is None:
            # Nothing actionable (e.g. an intermediate status); acknowledge so the
            # provider stops retrying this specific notification.
            return web.Response(text="ok")

        async with self._session_factory() as session:
            fresh = await record_event(
                session, update, raw_body=raw_body, signature_valid=True
            )
            if not fresh:
                await session.commit()
                return web.Response(text="ok")

            order, outcome = await apply_payment_update(session, update)
            await session.commit()

        log.info("%s webhook processed order=%s outcome=%s", name, getattr(order, "id", None), outcome)

        if order is not None and outcome in {"delivered", "no_stock"} and self._on_result:
            try:
                await self._on_result(order, outcome)
            except Exception:
                log.exception("post-payment notification failed for order %s", order.id)

        return web.Response(text="ok")
