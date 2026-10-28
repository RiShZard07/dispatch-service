"""Courier endpoints: registration, location, availability and listing."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import EngineDep, ExpectedVersionDep, PageDep
from app.schemas import CourierAvailability, CourierCreate, CourierLocation, CourierOut, CourierPage
from app.services.couriers import create_courier, get_courier, list_couriers, update_courier

router = APIRouter(prefix="/couriers", tags=["couriers"])


@router.post("", response_model=CourierOut, status_code=201)
def create(payload: CourierCreate, engine: EngineDep) -> CourierOut:
    return create_courier(engine, payload)


@router.get("", response_model=CourierPage)
def index(engine: EngineDep, page: PageDep, available: bool | None = None) -> CourierPage:
    """List couriers, optionally only the available ones."""
    items, next_offset = list_couriers(engine, available, page.limit, page.offset)
    return CourierPage(items=items, limit=page.limit, offset=page.offset, next_offset=next_offset)


@router.get("/{courier_id}", response_model=CourierOut)
def show(courier_id: str, engine: EngineDep) -> CourierOut:
    return get_courier(engine, courier_id)


@router.post("/{courier_id}/location", response_model=CourierOut)
def set_location(
    courier_id: str, payload: CourierLocation, engine: EngineDep, expected_version: ExpectedVersionDep
) -> CourierOut:
    """Report a new position. Bumps the courier's version."""
    return update_courier(engine, courier_id, expected_version, lat=payload.lat, lng=payload.lng)


@router.post("/{courier_id}/availability", response_model=CourierOut)
def set_availability(
    courier_id: str, payload: CourierAvailability, engine: EngineDep, expected_version: ExpectedVersionDep
) -> CourierOut:
    """Go on or off shift. Bumps the courier's version."""
    return update_courier(engine, courier_id, expected_version, available=payload.available)
