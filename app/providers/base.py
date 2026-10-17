"""Payment provider abstraction.

Each provider converts an :class:`~app.db.models.Order` into a hosted invoice and
converts provider notifications into a single :class:`PaymentUpdate` shape, so the
order/fulfillment layer never branches on provider specifics.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Protocol, runtime_checkable

from app.db.models import Order, Product


class PaymentState(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"
    REFUNDED = "refunded"


@dataclasses.dataclass(slots=True)
class Invoice:
    """Result of asking a provider to create a payable invoice."""

    provider_invoice_id: str
    pay_url: str | None
    expires_at_unix: int | None = None


@dataclasses.dataclass(slots=True)
class PaymentUpdate:
    """Normalised provider notification."""

    provider: str
    # Stable identifier for this notification, used for replay protection.
    event_id: str
    state: PaymentState
    order_id: str | None = None
    provider_invoice_id: str | None = None
    paid_amount_minor: int | None = None
    currency: str | None = None
    telegram_payment_charge_id: str | None = None
    provider_payment_charge_id: str | None = None
    event_type: str = ""


@runtime_checkable
class PaymentProvider(Protocol):
    name: str

    async def create_invoice(self, order: Order, product: Product) -> Invoice:
        """Create a payable invoice for ``order``."""

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> PaymentUpdate | None:
        """Verify and normalise a raw provider callback.

        Returns ``None`` when the payload is not a payment status change we act on.
        Raises :class:`SignatureError` when verification fails.
        """

    async def check_status(self, order: Order) -> PaymentUpdate | None:
        """Authoritative status lookup, used by the reconciliation loop."""


class SignatureError(Exception):
    """Raised when a callback signature cannot be verified."""


class ProviderError(Exception):
    """Raised when a provider API call fails."""

    def __init__(self, provider: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.retryable = retryable
