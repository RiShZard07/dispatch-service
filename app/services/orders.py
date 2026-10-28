"""Order use cases: idempotent creation, listing, transitions and courier assignment.

Every state change runs inside one write transaction together with its
order_events row and its outbox row, so the audit trail and the notification
queue can never disagree with the order. The order row is updated with a
compare-and-set on its version column; the row's new version doubles as the
event's sequence number. Once the transaction has committed, the event is
published to the in-process feed (which drives GET /orders/stream and wakes
the outbox worker); publishing before the commit would announce something
that might still roll back.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import Connection, Engine, Row, func, insert, select, update

from app.db import read_connection, write_transaction
from app.errors import ApiError, Conflict, NotFound
from app.feed import OrderFeed
from app.geo import haversine_km
from app.metrics import AppMetrics
from app.models import couriers, customers, merchants, order_events, orders
from app.outbox import enqueue
from app.request_logging import get_logger
from app.schemas import EventOut, OrderCreate, OrderOut
from app.services.common import check_expected_version, new_id, next_id, utcnow
from app.services.couriers import release_courier
from app.state import ACTIONS, OrderState, assert_transition

log = get_logger("orders")


def request_hash(payload: OrderCreate) -> str:
    """Hash of the canonical body, used to detect an Idempotency-Key reused with different data."""
    canonical = json.dumps(payload.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def row_to_order(row: Row[Any]) -> OrderOut:
    data = dict(row._mapping)
    data["items"] = json.loads(data["items"])
    return OrderOut.model_validate(data)


def event_type(target: OrderState) -> str:
    """Outbox event type for a transition, e.g. order.picked_up."""
    return f"order.{target.value.lower()}"


def _fetch_order(conn: Connection, order_id: str) -> OrderOut:
    row = conn.execute(select(orders).where(orders.c.id == order_id)).first()
    if row is None:
        raise NotFound("order", order_id)
    return row_to_order(row)


def _lock_order(conn: Connection, order_id: str) -> Row[Any]:
    """Read the order row for update. FOR UPDATE applies on PostgreSQL; SQLite relies on BEGIN IMMEDIATE."""
    row = conn.execute(select(orders).where(orders.c.id == order_id).with_for_update()).first()
    if row is None:
        raise NotFound("order", order_id)
    return row


def _append_event(conn: Connection, order: OrderOut, from_state: OrderState | None, actor: str) -> EventOut:
    """Write the audit event for the order's current state plus its outbox row, in the caller's transaction."""
    event = EventOut(
        id=next_id(conn, order_events.c.id),
        order_id=order.id,
        seq=order.version,
        from_state=from_state,
        to_state=order.state,
        actor=actor,
        created_at=order.updated_at,
    )
    conn.execute(
        insert(order_events).values(
            id=event.id,
            order_id=event.order_id,
            seq=event.seq,
            from_state=None if from_state is None else from_state.value,
            to_state=event.to_state.value,
            actor=event.actor,
            created_at=event.created_at,
        )
    )
    rendered = event.model_dump(mode="json")
    payload = {
        "event_id": event.id,  # the outbox row has an id of its own; this one is the audit id
        "order_id": event.order_id,
        "seq": event.seq,
        "from_state": rendered["from_state"],
        "to_state": rendered["to_state"],
        "actor": event.actor,
        "occurred_at": rendered["created_at"],
        "order": {
            "id": order.id,
            "state": order.state.value,
            "courier_id": order.courier_id,
            "version": order.version,
            "merchant_id": order.merchant_id,
            "customer_id": order.customer_id,
        },
    }
    enqueue(conn, event_type(order.state), payload, order.updated_at)
    return event


def _record_transition(
    conn: Connection, row: Row[Any], target: OrderState, actor: str, courier_id: str | None
) -> tuple[OrderOut, EventOut]:
    """Compare-and-set the order row and append its audit event and outbox row: all happen or none does."""
    now = utcnow()
    result = conn.execute(
        update(orders)
        .where(orders.c.id == row.id, orders.c.version == row.version)
        .values(state=target.value, courier_id=courier_id, version=row.version + 1, updated_at=now)
    )
    if result.rowcount != 1:
        raise Conflict("version_conflict", f"order {row.id} changed concurrently, retry")
    order = _fetch_order(conn, row.id)
    return order, _append_event(conn, order, OrderState(row.state), actor)


def create_order(engine: Engine, feed: OrderFeed, key: str, payload: OrderCreate, actor: str) -> tuple[OrderOut, bool]:
    """Create an order, or replay the one already created with this key. Returns (order, created)."""
    digest = request_hash(payload)
    with write_transaction(engine) as conn:
        existing = conn.execute(select(orders).where(orders.c.idempotency_key == key)).first()
        if existing is not None:
            if existing.request_hash != digest:
                raise Conflict(
                    "idempotency_conflict", f"Idempotency-Key {key!r} was already used with a different body"
                )
            return row_to_order(existing), False
        if conn.execute(select(merchants.c.id).where(merchants.c.id == payload.merchant_id)).first() is None:
            raise ApiError(422, "unknown_merchant", f"merchant {payload.merchant_id!r} does not exist")
        if conn.execute(select(customers.c.id).where(customers.c.id == payload.customer_id)).first() is None:
            raise ApiError(422, "unknown_customer", f"customer {payload.customer_id!r} does not exist")
        order_id, now = new_id("ord"), utcnow()
        conn.execute(
            insert(orders).values(
                id=order_id,
                idempotency_key=key,
                request_hash=digest,
                merchant_id=payload.merchant_id,
                customer_id=payload.customer_id,
                items=json.dumps([item.model_dump() for item in payload.items]),
                subtotal_cents=payload.subtotal_cents(),
                state=OrderState.CREATED.value,
                courier_id=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        order = _fetch_order(conn, order_id)
        event = _append_event(conn, order, None, actor)
    feed.publish(event)
    return order, True


def get_order(engine: Engine, order_id: str) -> OrderOut:
    with read_connection(engine) as conn:
        return _fetch_order(conn, order_id)


def list_orders(
    engine: Engine,
    state: OrderState | None,
    merchant_id: str | None,
    courier_id: str | None,
    limit: int,
    offset: int,
) -> tuple[list[OrderOut], int | None]:
    """Offset pagination ordered by creation time; one extra row tells us whether a next page exists."""
    stmt = select(orders).order_by(orders.c.created_at, orders.c.id).limit(limit + 1).offset(offset)
    if state is not None:
        stmt = stmt.where(orders.c.state == state.value)
    if merchant_id is not None:
        stmt = stmt.where(orders.c.merchant_id == merchant_id)
    if courier_id is not None:
        stmt = stmt.where(orders.c.courier_id == courier_id)
    with read_connection(engine) as conn:
        rows = conn.execute(stmt).all()
    next_offset = offset + limit if len(rows) > limit else None
    return [row_to_order(row) for row in rows[:limit]], next_offset


def list_events(engine: Engine, order_id: str) -> list[EventOut]:
    stmt = select(order_events).where(order_events.c.order_id == order_id).order_by(order_events.c.seq)
    with read_connection(engine) as conn:
        _fetch_order(conn, order_id)
        rows = conn.execute(stmt).all()
    return [EventOut.model_validate(dict(row._mapping)) for row in rows]


def count_by_state(engine: Engine) -> dict[str, int]:
    """Feeds the orders_by_state gauge on every metrics scrape."""
    with read_connection(engine) as conn:
        rows = conn.execute(select(orders.c.state, func.count()).group_by(orders.c.state)).all()
    return {state: count for state, count in rows}


def transition_order(
    engine: Engine, feed: OrderFeed, order_id: str, action: str, actor: str, expected_version: int | None
) -> OrderOut:
    """Apply accept, pickup, deliver or cancel. Assignment has its own function below."""
    target = ACTIONS[action]
    with write_transaction(engine) as conn:
        row = _lock_order(conn, order_id)
        check_expected_version(row.version, expected_version, f"order {order_id}")
        assert_transition(OrderState(row.state), target)
        order, event = _record_transition(conn, row, target, actor, row.courier_id)
        if target is OrderState.DELIVERED and row.courier_id is not None:
            release_courier(conn, row.courier_id)
    feed.publish(event)
    return order


def assign_order(
    engine: Engine, feed: OrderFeed, metrics: AppMetrics, order_id: str, actor: str, expected_version: int | None
) -> OrderOut:
    """Assign the nearest available courier, inside one write transaction.

    On SQLite the transaction holds the database write lock from BEGIN IMMEDIATE,
    so two assigns can never both read the same courier as available. On
    PostgreSQL the FOR UPDATE clauses lock the order row and every candidate
    courier row. The courier update is additionally a compare-and-set on its
    version and availability, so a double booking cannot slip through even if
    the locking were misconfigured.
    """
    with write_transaction(engine) as conn:
        row = _lock_order(conn, order_id)
        check_expected_version(row.version, expected_version, f"order {order_id}")
        assert_transition(OrderState(row.state), OrderState.ASSIGNED)
        merchant = conn.execute(select(merchants).where(merchants.c.id == row.merchant_id)).one()
        candidates = conn.execute(select(couriers).where(couriers.c.available.is_(True)).with_for_update()).all()
        if not candidates:
            metrics.assignment_conflicts.inc()
            raise Conflict("no_courier_available", "no courier is available right now, retry later")
        distance, nearest = min(
            ((haversine_km(merchant.lat, merchant.lng, c.lat, c.lng), c) for c in candidates),
            key=lambda pair: (pair[0], pair[1].id),
        )
        claimed = conn.execute(
            update(couriers)
            .where(couriers.c.id == nearest.id, couriers.c.version == nearest.version, couriers.c.available.is_(True))
            .values(available=False, version=nearest.version + 1)
        )
        if claimed.rowcount != 1:
            metrics.assignment_conflicts.inc()
            raise Conflict("courier_conflict", f"courier {nearest.id} changed during assignment, retry")
        order, event = _record_transition(conn, row, OrderState.ASSIGNED, actor, nearest.id)
    metrics.assignments.inc()
    feed.publish(event)
    log.info(
        "order assigned",
        extra={"fields": {"order_id": order_id, "courier_id": nearest.id, "distance_km": round(distance, 3)}},
    )
    return order
