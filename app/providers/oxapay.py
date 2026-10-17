"""OxaPay merchant API client.

Docs: https://docs.oxapay.com

Webhooks are signed with ``HMAC_SHA512(raw_body, merchant_api_key)`` sent in an
``HMAC`` header. Both ``paying`` and ``paid`` callbacks arrive; only ``paid`` (or a
manual accept) means the money is settled.
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
from app.services.money import parse_amount

log = logging.getLogger(__name__)

HMAC_HEADER = "HMAC"

_STATUS_MAP = {
    "new": PaymentState.PENDING,
    "waiting": PaymentState.PENDING,
    "paying": PaymentState.PENDING,
    "paid": PaymentState.PAID,
    "manual_accept": PaymentState.PAID,
    "expired": PaymentState.EXPIRED,
    "failed": PaymentState.FAILED,
    "refunded": PaymentState.REFUNDED,
    "refunding": PaymentState.REFUNDED,
}

# Statuses that are not yet settled; the callback is acknowledged but no delivery happens.
_IGNORED_STATUSES = {"new", "waiting", "paying", "underpaid", "refunding"}


def compute_signature(api_key: str, raw_body: bytes) -> str:
    return hmac.new(api_key.encode(), raw_body, hashlib.sha512).hexdigest()


def verify_signature(api_key: str, raw_body: bytes, signature: str | None) -> bool:
    if not signature or not api_key:
        return False
    return hmac.compare_digest(compute_signature(api_key, raw_body), signature)


class OxaPayProvider:
    name = "oxapay"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.oxapay.com",
        sandbox: bool = False,
        callback_url: str | None = None,
        session: aiohttp.ClientSession | None = None,
        fiat: str = "USD",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._sandbox = sandbox
        self._callback_url = callback_url
        self._session = session
        self._fiat = fiat

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "merchant_api_key": self._api_key,
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
        if int(data.get("status", 0)) != 200:
            raise ProviderError(self.name, f"{path} failed: {data.get('message') or data.get('error')}")
        return data.get("data") or {}

    async def create_invoice(self, order: Order, product: Product) -> Invoice:
        payload: dict[str, Any] = {
            "amount": float(order.amount_decimal()),
            "currency": order.currency,
            "order_id": order.id,
            "description": f"{product.title} (order {order.id[:8]})",
            "sandbox": self._sandbox,
        }
        callback_url = self._callback_url_for(order)
        if callback_url:
            payload["callback_url"] = callback_url
        lifetime = _minutes_until(order)
        if lifetime:
            payload["lifetime"] = lifetime

        data = await self._post("/v1/payment/invoice", payload)
        track_id = data.get("track_id")
        pay_url = data.get("payment_url")
        if track_id is None or not pay_url:
            raise ProviderError(self.name, "invoice response missing track_id/payment_url")
        return Invoice(
            provider_invoice_id=str(track_id),
            pay_url=pay_url,
            expires_at_unix=data.get("expired_at"),
        )

    async def get_payment(self, track_id: str) -> dict[str, Any]:
        return await self._post(f"/v1/payment/{track_id}", {})

    # ----------------------------------------------------------------- webhooks

    def parse_webhook(self, raw_body: bytes, headers: dict[str, str]) -> PaymentUpdate | None:
        signature = headers.get(HMAC_HEADER) or headers.get(HMAC_HEADER.lower())
        if not verify_signature(self._api_key, raw_body, signature):
            raise SignatureError("oxapay: invalid HMAC signature")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureError(f"oxapay: unreadable body: {exc}") from exc

        status = str(payload.get("status") or "").lower()
        if status in _IGNORED_STATUSES or not status:
            log.debug("oxapay: ignoring status=%s", status)
            return None

        state = _STATUS_MAP.get(status)
        if state is None:
            log.warning("oxapay: unknown status=%s", status)
            return None

        track_id = payload.get("track_id")
        # In a webhook, `amount`/`currency` describe what the payer sent in crypto
        # (e.g. 10 POL). `value` is that payment's worth in the invoice currency
        # (USD), which is what we billed, so it is what we compare against.
        paid = parse_amount(payload.get("value"), self._fiat)
        if paid is None:
            paid = parse_amount(payload.get("sent_value"), self._fiat)
        if paid is None and str(payload.get("currency") or "").upper() == self._fiat.upper():
            paid = parse_amount(payload.get("amount"), self._fiat)

        return PaymentUpdate(
            provider=self.name,
            event_id=f"{track_id}:{status}",
            state=state,
            order_id=str(payload.get("order_id")) if payload.get("order_id") else None,
            provider_invoice_id=str(track_id) if track_id is not None else None,
            paid_amount_minor=paid,
            currency=self._fiat,
            provider_payment_charge_id=str(track_id) if track_id is not None else None,
            event_type=f"oxapay_{status}",
        )

    async def check_status(self, order: Order) -> PaymentUpdate | None:
        if not order.provider_invoice_id:
            return None
        data = await self.get_payment(order.provider_invoice_id)
        status = str(data.get("status") or "").lower()
        state = _STATUS_MAP.get(status)
        if state is None or status in _IGNORED_STATUSES:
            return None
        return PaymentUpdate(
            provider=self.name,
            event_id=f"{order.provider_invoice_id}:{status}",
            state=state,
            order_id=order.id,
            provider_invoice_id=order.provider_invoice_id,
            paid_amount_minor=parse_amount(data.get("amount"), order.currency),
            currency=order.currency,
            event_type=f"poll:{status}",
        )

    def _callback_url_for(self, order: Order) -> str | None:
        return self._callback_url


def _minutes_until(order: Order) -> int | None:
    if order.expires_at is None:
        return None

    from app.db.base import utcnow

    expires = order.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    delta = int((expires - utcnow()).total_seconds() // 60)
    return max(15, min(2880, delta)) if delta > 0 else None
