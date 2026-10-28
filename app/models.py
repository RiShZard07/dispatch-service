"""SQLAlchemy Core table definitions.

These mirror schema.sql column for column. schema.sql is what actually creates
the tables; the objects here only build queries. tests/test_schema.py reflects
the live database and fails if the two ever disagree.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
)

metadata = MetaData()

merchants = Table(
    "merchants",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lng", Float, nullable=False),
)

customers = Table(
    "customers",
    metadata,
    Column("id", String, primary_key=True),
    Column("created_at", DateTime, nullable=False),
)

couriers = Table(
    "couriers",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("lat", Float, nullable=False),
    Column("lng", Float, nullable=False),
    Column("available", Boolean, nullable=False),
    Column("version", Integer, nullable=False),
)

orders = Table(
    "orders",
    metadata,
    Column("id", String, primary_key=True),
    Column("idempotency_key", String, nullable=False, unique=True),
    Column("request_hash", String, nullable=False),
    Column("merchant_id", String, ForeignKey("merchants.id"), nullable=False),
    Column("customer_id", String, ForeignKey("customers.id"), nullable=False),
    Column("items", Text, nullable=False),
    Column("subtotal_cents", Integer, nullable=False),
    Column("state", String, nullable=False),
    Column("courier_id", String, ForeignKey("couriers.id"), nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

order_events = Table(
    "order_events",
    metadata,
    Column("id", Integer, nullable=False, unique=True),
    Column("order_id", String, ForeignKey("orders.id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("from_state", String, nullable=True),
    Column("to_state", String, nullable=False),
    Column("actor", String, nullable=False),
    Column("created_at", DateTime, nullable=False),
    PrimaryKeyConstraint("order_id", "seq"),
)

outbox = Table(
    "outbox",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("next_attempt_at", DateTime, nullable=False),
    Column("delivered_at", DateTime, nullable=True),
    Column("last_error", String, nullable=True),
    Column("created_at", DateTime, nullable=False),
)
