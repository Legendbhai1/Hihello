"""Database models.

Money is never stored as a float. An order amount is kept as an integer
``amount_minor`` plus the currency exponent, so comparisons between the amount we
asked for and the amount a provider reports are exact.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utcnow

# Decimal exponent per supported settlement currency.
CURRENCY_EXPONENTS: dict[str, int] = {
    "XTR": 0,  # Telegram Stars
    "USD": 2,  # fiat, as billed by OxaPay
    "USDT": 6,
    "USDC": 6,
    "TON": 9,
    "BTC": 8,
    "ETH": 18,
    "TRX": 6,
    "LTC": 8,
    "BNB": 18,
}


def exponent_for(currency: str) -> int:
    return CURRENCY_EXPONENTS.get(currency.upper(), 8)


class Provider(str, enum.Enum):
    STARS = "stars"
    CRYPTOPAY = "cryptopay"
    OXAPAY = "oxapay"


class OrderStatus(str, enum.Enum):
    CREATED = "created"
    PENDING = "pending"
    PAID = "paid"
    DELIVERED = "delivered"
    EXPIRED = "expired"
    FAILED = "failed"
    REFUNDED = "refunded"


TERMINAL_STATUSES = {
    OrderStatus.DELIVERED,
    OrderStatus.REFUNDED,
    OrderStatus.EXPIRED,
    OrderStatus.FAILED,
}


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    orders: Mapped[list[Order]] = relationship(back_populates="user")


class Category(Base, TimestampMixin):
    """A store section. Products with no category show under "Uncategorised"."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    emoji: Mapped[str] = mapped_column(String(8), default="", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[list[Product]] = relationship(back_populates="category")


class Product(Base, TimestampMixin):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Bullet points, one per line, shown on the product card.
    features: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Free-text guarantee shown on the product card.
    guarantee: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Price in Telegram Stars (integer, XTR has no decimals).
    price_stars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Price in US cents; used for Crypto Pay and OxaPay settlement.
    price_usd_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Most a buyer may order in one checkout.
    max_per_order: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    stock_items: Mapped[list[StockItem]] = relationship(back_populates="product")
    category: Mapped[Category | None] = relationship(back_populates="products")

    def feature_list(self) -> list[str]:
        return [line.strip() for line in (self.features or "").splitlines() if line.strip()]


class StockItem(Base, TimestampMixin):
    """A single unit of deliverable content (a license key, a link, an account)."""

    __tablename__ = "stock_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True
    )
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="stock_items")


class Order(Base, TimestampMixin):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)
    currency_exponent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Unit price and total, both in minor units. total = unit * quantity.
    unit_amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Exact decimal string handed to the provider, kept verbatim for reconciliation.
    amount_str: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(
        String(16), default=OrderStatus.CREATED.value, nullable=False, index=True
    )
    provider_invoice_id: Mapped[str | None] = mapped_column(String(64), index=True)
    pay_url: Mapped[str | None] = mapped_column(Text)
    # Stars payments are refunded by charge id; keep both identifiers.
    telegram_payment_charge_id: Mapped[str | None] = mapped_column(String(128))
    provider_payment_charge_id: Mapped[str | None] = mapped_column(String(128))
    delivery_payload: Mapped[str | None] = mapped_column(Text)
    # All delivered units, one per line. Kept separately from delivery_payload so
    # a partial multi-unit delivery can still be retried and re-sent whole.
    delivered_items: Mapped[str | None] = mapped_column(Text)
    stock_item_id: Mapped[int | None] = mapped_column(BigInteger)
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="orders")
    product: Mapped[Product] = relationship()

    def amount_decimal(self) -> Decimal:
        return Decimal(self.amount_minor).scaleb(-self.currency_exponent)

    def unit_amount_decimal(self) -> Decimal:
        return Decimal(self.unit_amount_minor or self.amount_minor).scaleb(
            -self.currency_exponent
        )

    def delivered_list(self) -> list[str]:
        if self.delivered_items:
            return [line for line in self.delivered_items.splitlines() if line]
        return [self.delivery_payload] if self.delivery_payload else []

    def is_open(self) -> bool:
        return self.status in {OrderStatus.CREATED.value, OrderStatus.PENDING.value}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Order {self.id} {self.provider} {self.status} {self.amount_str}>"


class PaymentEvent(Base):
    """Every processed provider notification, used for replay-safe idempotency."""

    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    # Provider-supplied unique id (Crypto Pay update_id, OxaPay track_id+status, Stars charge id).
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[str] = mapped_column(Text, default="", nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
