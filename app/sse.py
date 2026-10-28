"""Server-sent events for GET /orders/stream.

The wire format is plain text: an event is a few "field: value" lines ended by
a blank line, and a line starting with ":" is a comment that browsers ignore,
which is what the heartbeat uses. Every event carries "id: <order_events.id>";
a browser EventSource remembers the last id it saw and sends it back as the
Last-Event-ID header when it reconnects, which is how a client resumes without
a gap.

Why nothing is missed: the stream subscribes to the in-process feed before it
reads the table for catch-up, so an event committed during the read is either
in the result or in the queue (or both, which the id check dedupes). While
live, an id more than one ahead of the last one sent means something was
published out of order or dropped, and an id behind it is a duplicate; both
fall back to reading the table. The heartbeat tick also reads the table, which
is what covers events written by another process on the same database.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from sqlalchemy import Engine, select
from starlette.concurrency import run_in_threadpool

from app.db import read_connection
from app.feed import OrderFeed
from app.models import order_events
from app.schemas import EventOut

CONTENT_TYPE = "text/event-stream; charset=utf-8"
EVENT_NAME = "order"
RECONNECT_MS = 3000
CATCH_UP_BATCH = 200
HEARTBEAT = b": heartbeat\n\n"
PREAMBLE = f"retry: {RECONNECT_MS}\n\n".encode()


def format_event(event: EventOut) -> bytes:
    """One SSE event block. The JSON is one line, so no "data:" continuation lines are needed."""
    data = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
    return f"id: {event.id}\nevent: {EVENT_NAME}\ndata: {data}\n\n".encode()


def events_after(engine: Engine, after: int, limit: int = CATCH_UP_BATCH) -> list[EventOut]:
    """Audit events with id greater than `after`, in id order, at most `limit` of them."""
    stmt = select(order_events).where(order_events.c.id > after).order_by(order_events.c.id).limit(limit)
    with read_connection(engine) as conn:
        rows = conn.execute(stmt).all()
    return [EventOut.model_validate(dict(row._mapping)) for row in rows]


async def event_stream(engine: Engine, feed: OrderFeed, after: int, heartbeat_seconds: float) -> AsyncIterator[bytes]:
    """Yield SSE bytes forever: catch-up from the table, then live events, with heartbeats when idle."""
    last = after

    async def catch_up() -> AsyncIterator[bytes]:
        nonlocal last
        while True:
            batch = await run_in_threadpool(events_after, engine, last)
            for event in batch:
                last = event.id
                yield format_event(event)
            if len(batch) < CATCH_UP_BATCH:
                return

    with feed.subscribe() as subscription:  # before the read, so nothing committed in between is lost
        yield PREAMBLE
        async for chunk in catch_up():
            yield chunk
        while True:
            event = await subscription.get(heartbeat_seconds)
            if event is None:
                yield HEARTBEAT
                async for chunk in catch_up():  # covers writers in other processes
                    yield chunk
            elif event.id <= last:
                continue  # already sent: the catch-up read and the queue overlapped
            elif event.id == last + 1:
                last = event.id
                yield format_event(event)
            else:
                async for chunk in catch_up():  # a gap: out-of-order or dropped publish
                    yield chunk
                if event.id > last:  # not in the table yet, which should be impossible; send it anyway
                    last = event.id
                    yield format_event(event)
