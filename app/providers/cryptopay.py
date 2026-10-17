"""Crypto Pay API client (the @CryptoBot payment app).

Docs: https://help.send.tg/en/articles/10279948-crypto-pay-api

Webhooks carry an ``crypto-pay-api-signature`` header computed as
``HMAC_SHA256(SHA256(api_token), raw_body)``. The raw body must be hashed exactly
as received.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC
from typing import Any

import aiohttp

from app.db.models import Order, Product
from app.providers.base import (
    Invoice,
    PaymentState,
    PaymentUpdate,
    ProviderError,
    SignatureError,
)
from app.services.money import format_amount, parse_amount

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "crypto-pay-api-signature"

# Crypto Pay invoice status -> normalised state.
_STATUS_MAP = {
    "active": PaymentState.PENDING,
    "paid": PaymentState.PAID,
    "expired": PaymentState.EXPIRED,
}

# Webhook update types that represent a terminal-ish payment state change.
_UPDATE_STATE_MAP = {
    "invoice_paid": PaymentState.PAID,
    "invoice_expired": PaymentState.EXPIRED,
}


def compute_signature(api_token: str, raw_body: bytes) -> str:
    secret = hashlib.sha256(api_token.encode()).digest()
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def verify_signature(api_token: str, raw_body: bytes, signature: str | None) -> bool:
    if not signature or not api_token:
        return False
    return hmac.compare_digest(compute_signature(api_token, raw_body), signature)


class CryptoPayProvider:
    name = "cryptopay"

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://pay.crypt.bot",
        session: aiohttp.ClientSession | None = None,
        fiat: str = "USD",
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._fiat = fiat

    # ---------------------------------------------------------------- API calls

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/api/{method}"
        headers = {
            "Crypto-Pay-API-Token": self._token,
            "Content-Type": "application/json",
        }
        try:
            if self._session is not None:
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)
            else:
                timeout = aiohttp.ClientTimeout(total=30)
                async with (
                    aiohttp.ClientSession(timeout=timeout) as session,
                    session.post(url, json=payload, headers=headers) as resp,
                ):
                    data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ProviderError(self.name, f"transport error: {exc}", retryable=True) from exc

        if not isinstance(data, dict):
            raise ProviderError(self.name, f"unexpected response: {data!r}")
        if not data.get("ok"):
            raise ProviderError(self.name, f"{method} failed: {data.get('error')}")
        return data.get("result") or {}

    async def create_invoice(self, order: Order, product: Product) -> Invoice:
        amount = format_amount(order.amount_minor, order.currency)
        payload: dict[str, Any] = {
            "currency_type": "fiat",
            "fiat": order.currency,
            "amount": amount,
            "description": f"{product.title} (order {order.id[:8]})",
            "payload": order.id,
            "allow_comments": False,
            "allow_anonymous": False,
        }
        expires_in = _seconds_until(order)
        if expires_in:
            payload["expires_in"] = expires_in

        result = await self._call("createInvoice", payload)
        invoice_id = result.get("invoice_id")
        if invoice_id is None:
            raise ProviderError(self.name, "createInvoice returned no invoice_id")
        return Invoice(
            provider_invoice_id=str(invoice_id),
            pay_url=result.get("bot_invoice_url") or result.get("pay_url"),
            expires_at_unix=result.get("expiration_date"),
        )

    async def get_invoices(self, invoice_ids: list[str]) -> list[dict[str, Any]]:
        if not invoice_ids:
            return []
        result = await self._call("getInvoices", {"invoice_ids": ",".join(invoice_ids)})
        items = result.get("items") if isinstance(result, dict) else None
        return list(items or [])

    # ----------------------------------------------------------------- webhooks

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> PaymentUpdate | None:
        signature = headers.get(SIGNATURE_HEADER)
        if not verify_signature(self._token, raw_body, signature):
            raise SignatureError("cryptopay: invalid webhook signature")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureError(f"cryptopay: unreadable body: {exc}") from exc

        update_type = str(payload.get("update_type") or "")
        invoice = payload.get("payload") or {}
        invoice_id = invoice.get("invoice_id")
        update_id = payload.get("update_id")

        state = _UPDATE_STATE_MAP.get(update_type)
        if state is None:
            # Not a payment-state change (e.g. a comment); acknowledge and ignore.
            log.debug("cryptopay: ignoring update_type=%s", update_type)
            return None

        # We only ever create fiat invoices. If a crypto invoice ever reaches this
        # handler, `amount` would be in that coin, so it must not be read as USD.
        currency_type = str(invoice.get("currency_type") or "fiat")
        paid_amount_minor = None
        if currency_type == "fiat":
            paid_amount_minor = parse_amount(invoice.get("amount"), self._fiat)
        else:
            log.warning("cryptopay: ignoring amount for non-fiat invoice %s", invoice_id)

        return PaymentUpdate(
            provider=self.name,
            event_id=f"{update_id or invoice_id}:{update_type}",
            state=state,
            order_id=str(invoice.get("payload")) if invoice.get("payload") else None,
            provider_invoice_id=str(invoice_id) if invoice_id is not None else None,
            paid_amount_minor=paid_amount_minor,
            currency=self._fiat,
            event_type=update_type,
        )

    async def check_status(self, order: Order) -> PaymentUpdate | None:
        if not order.provider_invoice_id:
            return None
        invoices = await self.get_invoices([order.provider_invoice_id])
        if not invoices:
            return None
        invoice = invoices[0]
        status = str(invoice.get("status") or "").lower()
        state = _STATUS_MAP.get(status)
        if state is None:
            return None
        currency_type = str(invoice.get("currency_type") or "fiat")
        paid = (
            parse_amount(invoice.get("amount"), self._fiat)
            if currency_type == "fiat"
            else None
        )
        return PaymentUpdate(
            provider=self.name,
            event_id=f"{invoice.get('invoice_id')}:{status}",
            state=state,
            order_id=order.id,
            provider_invoice_id=str(invoice.get("invoice_id")),
            paid_amount_minor=paid,
            currency=self._fiat,
            event_type=f"poll:{status}",
        )


def _seconds_until(order: Order) -> int | None:
    if order.expires_at is None:
        return None
    from app.db.base import utcnow

    expires = order.expires_at
    if expires.tzinfo is None:

        expires = expires.replace(tzinfo=UTC)
    delta = int((expires - utcnow()).total_seconds())
    return delta if delta > 0 else None
