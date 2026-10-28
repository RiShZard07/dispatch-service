"""Merchants and customers. Both are synthetic: an id, a name for merchants, a location."""

from __future__ import annotations

from sqlalchemy import Engine, insert, select

from app.db import read_connection, write_transaction
from app.errors import NotFound
from app.models import customers, merchants
from app.schemas import CustomerOut, MerchantCreate, MerchantOut
from app.services.common import new_id, utcnow


def create_merchant(engine: Engine, payload: MerchantCreate) -> MerchantOut:
    merchant_id = new_id("mer")
    with write_transaction(engine) as conn:
        conn.execute(insert(merchants).values(id=merchant_id, name=payload.name, lat=payload.lat, lng=payload.lng))
    return MerchantOut(id=merchant_id, name=payload.name, lat=payload.lat, lng=payload.lng)


def get_merchant(engine: Engine, merchant_id: str) -> MerchantOut:
    with read_connection(engine) as conn:
        row = conn.execute(select(merchants).where(merchants.c.id == merchant_id)).first()
    if row is None:
        raise NotFound("merchant", merchant_id)
    return MerchantOut.model_validate(dict(row._mapping))


def create_customer(engine: Engine) -> CustomerOut:
    customer_id, now = new_id("cus"), utcnow()
    with write_transaction(engine) as conn:
        conn.execute(insert(customers).values(id=customer_id, created_at=now))
    return CustomerOut(id=customer_id, created_at=now)


def get_customer(engine: Engine, customer_id: str) -> CustomerOut:
    with read_connection(engine) as conn:
        row = conn.execute(select(customers).where(customers.c.id == customer_id)).first()
    if row is None:
        raise NotFound("customer", customer_id)
    return CustomerOut.model_validate(dict(row._mapping))
