"""
Core schema for the MVP: users (our own auth), checkout_profiles (saved,
reusable delivery info), monei_connections (per-user Monei Connect OAuth
tokens, encrypted at rest), and orders (history/audit trail).

UUIDs are stored as plain String(36) rather than Postgres's native UUID
type, keeps the models portable across SQLite (used for local testing)
and Postgres (used in production) without any dialect-specific branching.
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, Boolean, DateTime, ForeignKey, Numeric, Text
from sqlalchemy.orm import relationship

from db.base import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    checkout_profiles = relationship(
        "CheckoutProfile", back_populates="user", cascade="all, delete-orphan"
    )
    monei_connection = relationship(
        "MoneiConnection", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")


class CheckoutProfile(Base):
    """A saved, reusable checkout info set, 'Home', 'Office', etc."""

    __tablename__ = "checkout_profiles"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String, nullable=False, default="Home")
    full_name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    address = Column(String, nullable=True)
    area = Column(String, nullable=True)
    delivery_or_pickup = Column(String, nullable=False, default="delivery")
    is_default = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="checkout_profiles")


class MoneiConnection(Base):
    """One row per user, their Monei Connect OAuth grant. Tokens are
    encrypted at rest (see db/crypto.py); this table only ever stores the
    ciphertext, never the raw token."""

    __tablename__ = "monei_connections"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    monei_user_id = Column(String, nullable=True)
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=False)
    scopes = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="monei_connection")


class Order(Base):
    """History/audit trail, one row per placed order."""

    __tablename__ = "orders"

    id = Column(String(36), primary_key=True, default=gen_uuid)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    order_identifier = Column(String, nullable=True, index=True)
    items_json = Column(Text, nullable=False)
    checkout_info_json = Column(Text, nullable=True)
    total_ngn = Column(Numeric(12, 2), nullable=False)
    status = Column(String, nullable=False, default="pending")
    monei_transaction_ref = Column(String, nullable=True)
    payment_details_json = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="orders")