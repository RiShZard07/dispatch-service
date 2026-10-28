"""Order endpoints: idempotent creation, reads, the audit trail, the live feed and state transitions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Response
from fastapi.responses import StreamingResponse

from app.api.deps import (
    ActorDep,
    EngineDep,
    ExpectedVersionDep,
    FeedDep,
    MetricsDep,
    PageDep,
    SettingsDep,
    StreamCursorDep,
)
from app.schemas import EventOut, OrderCreate, OrderOut, OrderPage
from app.services.orders import assign_order, create_order, get_order, list_events, list_orders, transition_order
from app.sse import CONTENT_TYPE, event_stream
from app.state import OrderState

router = APIRouter(prefix="/orders", tags=["orders"])

IdempotencyKey = Annotated[str, Header(min_length=1, max_length=128)]


@router.post("", response_model=OrderOut, status_code=201)
def create(
    payload: OrderCreate,
    response: Response,
    engine: EngineDep,
    feed: FeedDep,
    actor: ActorDep,
    idempotency_key: IdempotencyKey,
) -> OrderOut:
    """Create an order. Retrying with the same Idempotency-Key and body returns the original order with 200."""
    order, created = create_order(engine, feed, idempotency_key, payload, actor)
    response.status_code = 201 if created else 200
    return order


@router.get("", response_model=OrderPage)
def index(
    engine: EngineDep,
    page: PageDep,
    state: OrderState | None = None,
    merchant_id: str | None = None,
    courier_id: str | None = None,
) -> OrderPage:
    """List orders, oldest first, filtered by state, merchant and/or courier, with limit/offset pagination."""
    items, next_offset = list_orders(engine, state, merchant_id, courier_id, page.limit, page.offset)
    return OrderPage(items=items, limit=page.limit, offset=page.offset, next_offset=next_offset)


# Declared before /{order_id} so the literal path wins over the parameter.
@router.get("/stream", response_class=StreamingResponse)
async def stream(engine: EngineDep, feed: FeedDep, settings: SettingsDep, after: StreamCursorDep) -> StreamingResponse:
    """Server-sent events: every audit event with an id greater than Last-Event-ID (or ?after=), then live ones.

    Each event is `id: <event id>`, `event: order`, `data: <EventOut JSON>`. A comment line is sent
    after DISPATCH_STREAM_HEARTBEAT_SECONDS of silence so proxies and clients can tell idle from dead.
    """
    body = event_stream(engine, feed, after, settings.stream_heartbeat_seconds)
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(body, media_type=CONTENT_TYPE, headers=headers)


@router.get("/{order_id}", response_model=OrderOut)
def show(order_id: str, engine: EngineDep) -> OrderOut:
    return get_order(engine, order_id)


@router.get("/{order_id}/events", response_model=list[EventOut])
def show_events(order_id: str, engine: EngineDep) -> list[EventOut]:
    """The append-only audit trail, in the order the transitions happened."""
    return list_events(engine, order_id)


@router.post("/{order_id}/accept", response_model=OrderOut)
def accept(
    order_id: str, engine: EngineDep, feed: FeedDep, actor: ActorDep, expected_version: ExpectedVersionDep
) -> OrderOut:
    """CREATED -> ACCEPTED."""
    return transition_order(engine, feed, order_id, "accept", actor, expected_version)


@router.post("/{order_id}/assign", response_model=OrderOut)
def assign(
    order_id: str,
    engine: EngineDep,
    feed: FeedDep,
    metrics: MetricsDep,
    actor: ActorDep,
    expected_version: ExpectedVersionDep,
) -> OrderOut:
    """ACCEPTED -> ASSIGNED, booking the nearest available courier in the same transaction."""
    return assign_order(engine, feed, metrics, order_id, actor, expected_version)


@router.post("/{order_id}/pickup", response_model=OrderOut)
def pickup(
    order_id: str, engine: EngineDep, feed: FeedDep, actor: ActorDep, expected_version: ExpectedVersionDep
) -> OrderOut:
    """ASSIGNED -> PICKED_UP."""
    return transition_order(engine, feed, order_id, "pickup", actor, expected_version)


@router.post("/{order_id}/deliver", response_model=OrderOut)
def deliver(
    order_id: str, engine: EngineDep, feed: FeedDep, actor: ActorDep, expected_version: ExpectedVersionDep
) -> OrderOut:
    """PICKED_UP -> DELIVERED. The courier becomes available again."""
    return transition_order(engine, feed, order_id, "deliver", actor, expected_version)


@router.post("/{order_id}/cancel", response_model=OrderOut)
def cancel(
    order_id: str, engine: EngineDep, feed: FeedDep, actor: ActorDep, expected_version: ExpectedVersionDep
) -> OrderOut:
    """CREATED or ACCEPTED -> CANCELED."""
    return transition_order(engine, feed, order_id, "cancel", actor, expected_version)
