"""Pydantic request and response models. Inputs forbid unknown fields so typos fail with 422."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.state import OrderState


def _iso_utc(value: datetime) -> str:
    """Timestamps are stored as naive UTC; render them with an explicit Z."""
    return value.replace(tzinfo=None).isoformat(timespec="milliseconds") + "Z"


UtcTimestamp = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
Name = Annotated[str, Field(min_length=1, max_length=64)]


class StrictModel(BaseModel):
    """Base for request bodies: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class OrderItem(StrictModel):
    sku: Name
    quantity: int = Field(ge=1, le=1000)
    unit_price_cents: int = Field(ge=0, le=10_000_000)


class OrderCreate(StrictModel):
    merchant_id: Name
    customer_id: Name
    items: list[OrderItem] = Field(min_length=1, max_length=50)

    def subtotal_cents(self) -> int:
        """Computed server side so the stored subtotal always matches the items."""
        return sum(item.quantity * item.unit_price_cents for item in self.items)


class OrderOut(BaseModel):
    id: str
    idempotency_key: str
    merchant_id: str
    customer_id: str
    items: list[OrderItem]
    subtotal_cents: int
    state: OrderState
    courier_id: str | None
    version: int
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class OrderPage(BaseModel):
    items: list[OrderOut]
    limit: int
    offset: int
    next_offset: int | None


class EventOut(BaseModel):
    """One audit event. id is global across orders and is the cursor for GET /orders/stream."""

    id: int
    order_id: str
    seq: int
    from_state: OrderState | None
    to_state: OrderState
    actor: str
    created_at: UtcTimestamp


class CourierCreate(StrictModel):
    name: Name
    lat: Latitude
    lng: Longitude
    available: bool = True


class CourierLocation(StrictModel):
    lat: Latitude
    lng: Longitude


class CourierAvailability(StrictModel):
    available: bool


class CourierOut(BaseModel):
    id: str
    name: str
    lat: float
    lng: float
    available: bool
    version: int


class CourierPage(BaseModel):
    items: list[CourierOut]
    limit: int
    offset: int
    next_offset: int | None


class MerchantCreate(StrictModel):
    name: Name
    lat: Latitude
    lng: Longitude


class MerchantOut(BaseModel):
    id: str
    name: str
    lat: float
    lng: float


class CustomerOut(BaseModel):
    id: str
    created_at: UtcTimestamp


class HealthOut(BaseModel):
    status: str
    database: str
