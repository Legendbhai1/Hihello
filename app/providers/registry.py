"""Payment providers registry.

Holds the configured provider instances and exposes a lookup by order provider
name. Telegram Stars is always present because it needs no credentials.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from app.config import Settings
from app.providers.base import PaymentProvider
from app.providers.cryptopay import CryptoPayProvider
from app.providers.oxapay import OxaPayProvider
from app.providers.stars import StarsProvider

log = logging.getLogger(__name__)


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, PaymentProvider] = {}

    def register(self, provider: PaymentProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> PaymentProvider | None:
        return self._providers.get(name)

    def require(self, name: str) -> PaymentProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise KeyError(f"payment provider '{name}' is not configured")
        return provider

    def names(self) -> list[str]:
        return list(self._providers)

    def enabled(self) -> list[str]:
        """Provider names in the order they should be offered to buyers."""
        order = ["stars", "cryptopay", "oxapay"]
        return [name for name in order if name in self._providers]


def build_registry(settings: Settings, bot: Bot) -> ProviderRegistry:
    registry = ProviderRegistry()
    for name in settings.enabled_providers():
        if name == "stars":
            registry.register(StarsProvider(bot=bot))
        elif name == "cryptopay":
            registry.register(
                CryptoPayProvider(
                    token=settings.cryptopay_token,
                    base_url=settings.cryptopay_base_url,
                )
            )
        elif name == "oxapay":
            registry.register(
                OxaPayProvider(
                    api_key=settings.oxapay_api_key,
                    base_url=settings.oxapay_base_url,
                    sandbox=settings.oxapay_sandbox,
                    callback_url=settings.oxapay_callback_url,
                )
            )
    log.info("payment providers enabled: %s", ", ".join(registry.enabled()) or "none")
    return registry
