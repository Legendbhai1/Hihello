"""Exact decimal money helpers.

Amounts crossing a provider boundary are compared as integers in the smallest
unit of the currency. Floats are never used for money.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.db.models import exponent_for


def to_minor(amount: Decimal | str | int, currency: str) -> int:
    """Convert a human amount to integer minor units (e.g. 1.23 USD -> 123)."""
    dec = Decimal(str(amount))
    scaled = dec.scaleb(exponent_for(currency))
    return int(scaled.to_integral_value(rounding=ROUND_HALF_UP))


def from_minor(minor: int, currency: str) -> Decimal:
    """Inverse of :func:`to_minor`."""
    return Decimal(minor).scaleb(-exponent_for(currency))


def format_amount(minor: int, currency: str) -> str:
    """Render a minor-unit amount as a provider-ready decimal string."""
    exp = exponent_for(currency)
    value = from_minor(minor, currency)
    if exp == 0:
        return str(int(value))
    return f"{value:.{exp}f}".rstrip("0").rstrip(".")


def parse_amount(value: object, currency: str) -> int | None:
    """Best-effort conversion of a provider-reported amount to minor units."""
    if value is None:
        return None
    try:
        return to_minor(Decimal(str(value)), currency)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def amounts_match(expected_minor: int, received_minor: int | None, currency: str) -> bool:
    """True when the provider paid at least what we asked for."""
    if received_minor is None:
        return False
    return received_minor >= expected_minor
