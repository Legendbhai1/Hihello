"""Webhook signature verification for Crypto Pay and OxaPay.

These are the security boundary: an unsigned or wrongly signed callback must be
rejected before any order state changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.providers import cryptopay, oxapay
from app.providers.base import PaymentState, SignatureError

CRYPTOPAY_TOKEN = "12345:test-cryptopay-token"
OXAPAY_KEY = "test-oxapay-merchant-key"


# ---------------------------------------------------------------- Crypto Pay


def _cryptopay_signature(token: str, body: bytes) -> str:
    secret = hashlib.sha256(token.encode()).digest()
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _cryptopay_body(**overrides) -> bytes:
    payload = {
        "update_id": 987654321,
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": 4242,
            "status": "paid",
            "amount": "4.99",
            "payload": "order-abc",
        },
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_cryptopay_valid_signature_is_accepted() -> None:
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    body = _cryptopay_body()
    update = provider.parse_webhook(
        body, {cryptopay.SIGNATURE_HEADER: _cryptopay_signature(CRYPTOPAY_TOKEN, body)}
    )
    assert update is not None
    assert update.state is PaymentState.PAID
    assert update.order_id == "order-abc"
    assert update.paid_amount_minor == 499
    assert update.provider_invoice_id == "4242"


def test_cryptopay_tampered_body_is_rejected() -> None:
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    body = _cryptopay_body()
    signature = _cryptopay_signature(CRYPTOPAY_TOKEN, body)
    tampered = _cryptopay_body(update_id=1)  # attacker rewrites the payload
    with pytest.raises(SignatureError):
        provider.parse_webhook(tampered, {cryptopay.SIGNATURE_HEADER: signature})


def test_cryptopay_missing_signature_is_rejected() -> None:
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    with pytest.raises(SignatureError):
        provider.parse_webhook(_cryptopay_body(), {})


def test_cryptopay_wrong_token_signature_is_rejected() -> None:
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    body = _cryptopay_body()
    with pytest.raises(SignatureError):
        provider.parse_webhook(body, {cryptopay.SIGNATURE_HEADER: _cryptopay_signature("other", body)})


def test_cryptopay_ignores_non_payment_updates() -> None:
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    body = _cryptopay_body(update_type="invoice_comment")
    update = provider.parse_webhook(
        body, {cryptopay.SIGNATURE_HEADER: _cryptopay_signature(CRYPTOPAY_TOKEN, body)}
    )
    assert update is None


def test_cryptopay_signature_is_stable_for_identical_input() -> None:
    body = b'{"a":1,"b":"x"}'
    assert cryptopay.compute_signature(CRYPTOPAY_TOKEN, body) == cryptopay.compute_signature(
        CRYPTOPAY_TOKEN, body
    )


def test_cryptopay_crypto_invoice_amount_is_not_read_as_fiat() -> None:
    """A non-fiat invoice amount is in crypto, so it must not become USD cents."""
    provider = cryptopay.CryptoPayProvider(token=CRYPTOPAY_TOKEN)
    body = _cryptopay_body(
        payload={
            "invoice_id": 4242,
            "status": "paid",
            "currency_type": "crypto",
            "amount": "0.0005",  # BTC
            "payload": "order-abc",
        }
    )
    update = provider.parse_webhook(
        body, {cryptopay.SIGNATURE_HEADER: _cryptopay_signature(CRYPTOPAY_TOKEN, body)}
    )
    assert update is not None
    assert update.paid_amount_minor is None  # unknown, not 0


# -------------------------------------------------------------------- OxaPay


def _oxapay_signature(key: str, body: bytes) -> str:
    return hmac.new(key.encode(), body, hashlib.sha512).hexdigest()


def _oxapay_body(**overrides) -> bytes:
    payload = {
        "track_id": "168931368",
        "status": "Paid",
        "type": "invoice",
        "amount": 4.99,
        "currency": "USD",
        "order_id": "order-xyz",
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def test_oxapay_valid_signature_is_accepted() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = _oxapay_body()
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.state is PaymentState.PAID
    assert update.order_id == "order-xyz"
    assert update.paid_amount_minor == 499


def test_oxapay_tampered_body_is_rejected() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = _oxapay_body()
    signature = _oxapay_signature(OXAPAY_KEY, body)
    with pytest.raises(SignatureError):
        provider.parse_webhook(_oxapay_body(amount=0.01), {"HMAC": signature})


def test_oxapay_missing_signature_is_rejected() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    with pytest.raises(SignatureError):
        provider.parse_webhook(_oxapay_body(), {})


@pytest.mark.parametrize("status", ["paying", "waiting", "new", "underpaid", "refunding"])
def test_oxapay_intermediate_statuses_are_ignored(status: str) -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = _oxapay_body(status=status)
    assert provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)}) is None


def test_oxapay_expired_status_maps_to_expired() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = _oxapay_body(status="Expired")
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.state is PaymentState.EXPIRED


def test_oxapay_manual_accept_counts_as_paid() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = _oxapay_body(status="manual_accept")
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.state is PaymentState.PAID


def test_oxapay_real_webhook_shape_uses_fiat_value_not_crypto_amount() -> None:
    """OxaPay reports the payer's crypto in `currency` and the fiat worth in `value`.

    The crypto amount must never be compared against our USD bill.
    """
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = json.dumps(
        {
            "track_id": "151811887",
            "status": "Paid",
            "type": "invoice",
            "amount": 10,  # 10 POL sent by the payer
            "value": 9.99,  # worth 9.99 USD, which is what we billed
            "currency": "POL",
            "order_id": "order-real",
        }
    ).encode()
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.state is PaymentState.PAID
    assert update.currency == "USD"
    assert update.paid_amount_minor == 999
    assert update.paid_amount_minor != 1000  # not 10 POL scaled as USD


def test_oxapay_underpaid_value_is_reported_as_short() -> None:
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = json.dumps(
        {
            "track_id": "151811887",
            "status": "Paid",
            "amount": 0.001,
            "value": 0.01,
            "currency": "BTC",
            "order_id": "order-under",
        }
    ).encode()
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.paid_amount_minor == 1


def test_oxapay_crypto_amount_is_never_treated_as_fiat() -> None:
    """With no `value`, a crypto amount must not be misread as USD cents."""
    provider = oxapay.OxaPayProvider(api_key=OXAPAY_KEY)
    body = json.dumps(
        {"track_id": "1", "status": "Paid", "amount": 10, "currency": "POL", "order_id": "o"}
    ).encode()
    update = provider.parse_webhook(body, {"HMAC": _oxapay_signature(OXAPAY_KEY, body)})
    assert update is not None
    assert update.paid_amount_minor is None  # unknown, not 1000
