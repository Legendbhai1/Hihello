"""Money conversion must be exact and never go through floats."""

from decimal import Decimal

import pytest

from app.services.money import (
    amounts_match,
    format_amount,
    from_minor,
    parse_amount,
    to_minor,
)


@pytest.mark.parametrize(
    ("value", "currency", "expected"),
    [
        ("1.23", "USD", 123),
        ("0.01", "USD", 1),
        ("4.99", "USD", 499),
        (50, "XTR", 50),
        ("1", "TON", 1_000_000_000),
        ("0.00000001", "BTC", 1),
        ("1.000001", "USDT", 1_000_001),
    ],
)
def test_to_minor_is_exact(value: str, currency: str, expected: int) -> None:
    assert to_minor(value, currency) == expected


def test_to_minor_avoids_float_rounding() -> None:
    # 0.29 as a float is 0.28999999999999998; the Decimal path must not care.
    assert to_minor(Decimal("0.29"), "USD") == 29
    assert to_minor("2.675", "USD") == 268  # ROUND_HALF_UP


def test_round_trip() -> None:
    for minor, currency in [(123, "USD"), (499, "USD"), (50, "XTR"), (123456789, "ETH")]:
        assert to_minor(from_minor(minor, currency), currency) == minor


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (123, "USD", "1.23"),
        (50, "XTR", "50"),
        (1000000, "USD", "10000"),
        (1, "BTC", "0.00000001"),
    ],
)
def test_format_amount(minor: int, currency: str, expected: str) -> None:
    assert format_amount(minor, currency) == expected


def test_parse_amount_tolerates_garbage() -> None:
    assert parse_amount(None, "USD") is None
    assert parse_amount("abc", "USD") is None
    assert parse_amount("1.5", "USD") == 150


def test_amounts_match_requires_at_least_the_asked_amount() -> None:
    assert amounts_match(100, 100, "USD")
    assert amounts_match(100, 150, "USD")
    assert not amounts_match(100, 99, "USD")
    assert not amounts_match(100, None, "USD")
