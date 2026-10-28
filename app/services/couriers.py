"""Courier use cases: registration, location and availability updates."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, Row, insert, select, update

from app.db import read_connection, write_transaction
from app.errors import Conflict, NotFound
from app.models import couriers
from app.schemas import CourierCreate, CourierOut
from app.services.common import check_expected_version, new_id


def row_to_courier(row: Row[Any]) -> CourierOut:
    return CourierOut.model_validate(dict(row._mapping))


def _fetch_courier(conn: Connection, courier_id: str) -> CourierOut:
    row = conn.execute(select(couriers).where(couriers.c.id == courier_id)).first()
    if row is None:
        raise NotFound("courier", courier_id)
    return row_to_courier(row)


def create_courier(engine: Engine, payload: CourierCreate) -> CourierOut:
    with write_transaction(engine) as conn:
        courier_id = new_id("cur")
        conn.execute(
            insert(couriers).values(
                id=courier_id,
                name=payload.name,
                lat=payload.lat,
                lng=payload.lng,
                available=payload.available,
                version=1,
            )
        )
        return _fetch_courier(conn, courier_id)


def get_courier(engine: Engine, courier_id: str) -> CourierOut:
    with read_connection(engine) as conn:
        return _fetch_courier(conn, courier_id)


def list_couriers(
    engine: Engine, available: bool | None, limit: int, offset: int
) -> tuple[list[CourierOut], int | None]:
    """Offset pagination: one extra row is fetched to learn whether a next page exists."""
    stmt = select(couriers).order_by(couriers.c.name, couriers.c.id).limit(limit + 1).offset(offset)
    if available is not None:
        stmt = stmt.where(couriers.c.available.is_(available))
    with read_connection(engine) as conn:
        rows = conn.execute(stmt).all()
    next_offset = offset + limit if len(rows) > limit else None
    return [row_to_courier(row) for row in rows[:limit]], next_offset


def update_courier(engine: Engine, courier_id: str, expected_version: int | None, **changes: Any) -> CourierOut:
    """Compare-and-set update shared by the location and availability endpoints."""
    with write_transaction(engine) as conn:
        row = conn.execute(select(couriers).where(couriers.c.id == courier_id).with_for_update()).first()
        if row is None:
            raise NotFound("courier", courier_id)
        check_expected_version(row.version, expected_version, f"courier {courier_id}")
        result = conn.execute(
            update(couriers)
            .where(couriers.c.id == courier_id, couriers.c.version == row.version)
            .values(version=row.version + 1, **changes)
        )
        if result.rowcount != 1:
            raise Conflict("version_conflict", f"courier {courier_id} changed concurrently, retry")
        return _fetch_courier(conn, courier_id)


def release_courier(conn: Connection, courier_id: str) -> None:
    """Make a courier available again. Called inside the delivery transaction."""
    conn.execute(
        update(couriers).where(couriers.c.id == courier_id).values(available=True, version=couriers.c.version + 1)
    )
