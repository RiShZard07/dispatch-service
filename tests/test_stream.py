"""GET /orders/stream: server-sent events over a real uvicorn server, plus the generator on its own.

The TestClient buffers a whole response, so the endless stream is read with
httpx against uvicorn running in a thread (tests/support.py::live_server).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from app.feed import OrderFeed
from app.schemas import EventOut, OrderCreate
from app.services.orders import create_order, transition_order
from app.sse import CONTENT_TYPE, HEARTBEAT, PREAMBLE, event_stream, events_after, format_event
from tests.support import (
    World,
    advance,
    error_code,
    live_server,
    make_courier,
    make_order,
    make_world,
    order_body,
    wait_until,
)

FAST_HEARTBEAT = 0.05
PREAMBLE_LINE = PREAMBLE.decode().strip()
HEARTBEAT_LINE = HEARTBEAT.decode().strip()


def parse_events(lines: Iterator[str], count: int, comments: list[str] | None = None) -> list[dict[str, Any]]:
    """Read SSE lines until `count` events have arrived. Comment lines are collected separately."""
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in lines:
        if line.startswith(":"):
            if comments is not None:
                comments.append(line)
        elif line == "":
            if "data" in current:
                events.append(current)
                if len(events) == count:
                    return events
            current = {}
        else:
            field, _, value = line.partition(":")
            current[field] = value.strip()
    raise AssertionError(f"stream ended after {len(events)} events, expected {count}")


def ids(events: list[dict[str, Any]]) -> list[int]:
    return [int(event["id"]) for event in events]


def test_stream_delivers_live_events_in_order_with_the_sse_framing(app: FastAPI) -> None:
    with live_server(app) as base, httpx.Client(base_url=base, timeout=10) as http:
        world = make_world(http)
        make_courier(http, "C", 0.0, 0.0)
        with http.stream("GET", "/orders/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == CONTENT_TYPE
            assert response.headers["cache-control"] == "no-cache"
            assert len(response.headers["x-request-id"]) == 32
            lines = response.iter_lines()
            assert next(lines) == PREAMBLE_LINE  # the subscription exists once this arrives
            assert next(lines) == ""
            order = make_order(http, world, "k1")
            advance(http, order["id"], "accept", "assign")

            events = parse_events(lines, 3)
        assert all(event["event"] == "order" for event in events)
        assert ids(events) == [1, 2, 3]
        payloads = [json.loads(event["data"]) for event in events]
        assert [(p["id"], p["order_id"], p["seq"], p["to_state"]) for p in payloads] == [
            (1, order["id"], 1, "CREATED"),
            (2, order["id"], 2, "ACCEPTED"),
            (3, order["id"], 3, "ASSIGNED"),
        ]
        assert payloads[2]["from_state"] == "ACCEPTED" and payloads[2]["created_at"].endswith("Z")
        wait_until(lambda: app.state.feed.subscriber_count == 0)  # the disconnect unsubscribed the stream


def test_reconnecting_with_last_event_id_replays_exactly_what_was_missed(app: FastAPI) -> None:
    with live_server(app) as base, httpx.Client(base_url=base, timeout=10) as http:
        world = make_world(http)
        first = make_order(http, world, "k1")
        with http.stream("GET", "/orders/stream") as response:
            seen = parse_events(response.iter_lines(), 1)  # the catch-up read delivers event 1
        assert ids(seen) == [1]

        advance(http, first["id"], "accept", "cancel")  # events 2 and 3 happen while nobody is listening
        second = make_order(http, world, "k2")  # event 4

        with http.stream("GET", "/orders/stream", headers={"Last-Event-ID": "1"}) as response:
            lines = response.iter_lines()
            missed = parse_events(lines, 3)
            advance(http, second["id"], "accept")  # event 5, live
            live = parse_events(lines, 1)
        assert ids(missed) == [2, 3, 4]
        assert [json.loads(e["data"])["to_state"] for e in missed] == ["ACCEPTED", "CANCELED", "CREATED"]
        assert ids(live) == [5] and json.loads(live[0]["data"])["order_id"] == second["id"]


def test_after_query_parameter_resumes_and_the_header_takes_precedence(app: FastAPI) -> None:
    with live_server(app) as base, httpx.Client(base_url=base, timeout=10) as http:
        world = make_world(http)
        order = make_order(http, world, "k1")
        advance(http, order["id"], "accept", "cancel")  # events 1, 2, 3

        with http.stream("GET", "/orders/stream", params={"after": 1}) as response:
            assert ids(parse_events(response.iter_lines(), 2)) == [2, 3]
        with http.stream("GET", "/orders/stream", params={"after": 1}, headers={"Last-Event-ID": "2"}) as response:
            assert ids(parse_events(response.iter_lines(), 1)) == [3]
        with http.stream("GET", "/orders/stream", params={"after": 0}) as response:
            assert ids(parse_events(response.iter_lines(), 3)) == [1, 2, 3]


def test_heartbeat_comments_are_sent_while_nothing_happens(settings: Settings) -> None:
    app = create_app(replace(settings, stream_heartbeat_seconds=FAST_HEARTBEAT))
    try:
        with live_server(app) as base, httpx.Client(base_url=base, timeout=10) as http:
            with http.stream("GET", "/orders/stream") as response:
                lines = response.iter_lines()
                assert next(lines) == PREAMBLE_LINE
                comments = []
                for line in lines:
                    if line.startswith(":"):
                        comments.append(line)
                    if len(comments) == 3:
                        break
            assert comments == [HEARTBEAT_LINE] * 3
    finally:
        app.state.engine.dispose()


def test_events_written_by_another_process_arrive_through_the_catch_up_read(settings: Settings) -> None:
    """Two apps on one database file stand in for two processes. The streaming app never hears the
    writing app's in-process feed, so only the catch-up read at the heartbeat can deliver the event."""
    streaming = create_app(replace(settings, stream_heartbeat_seconds=FAST_HEARTBEAT))
    writing = create_app(settings)
    try:
        with live_server(streaming) as base, httpx.Client(base_url=base, timeout=10) as http:
            with TestClient(writing) as writer, http.stream("GET", "/orders/stream") as response:
                lines = response.iter_lines()
                assert next(lines) == PREAMBLE_LINE
                for line in lines:  # the first heartbeat means the initial catch-up is over
                    if line == HEARTBEAT_LINE:
                        break
                order = make_order(writer, make_world(writer), "k1")
                events = parse_events(lines, 1)
            assert ids(events) == [1] and json.loads(events[0]["data"])["order_id"] == order["id"]
    finally:
        streaming.state.engine.dispose()
        writing.state.engine.dispose()


def test_disconnecting_removes_the_subscription_promptly(app: FastAPI) -> None:
    with live_server(app) as base, httpx.Client(base_url=base, timeout=10) as http:
        with http.stream("GET", "/orders/stream") as response:
            assert next(response.iter_lines()) == PREAMBLE_LINE
            assert app.state.feed.subscriber_count == 1
        wait_until(lambda: app.state.feed.subscriber_count == 0, timeout=5)


def test_malformed_cursor_is_422(client: TestClient) -> None:
    bad_header = client.get("/orders/stream", headers={"Last-Event-ID": "latest"})
    assert bad_header.status_code == 422 and error_code(bad_header) == "validation_error"
    assert "Last-Event-ID" in bad_header.json()["error"]["message"]
    negative = client.get("/orders/stream", params={"after": -1})
    assert negative.status_code == 422 and error_code(negative) == "validation_error"


def test_format_event_follows_the_wire_format() -> None:
    event = EventOut.model_validate(
        {
            "id": 7,
            "order_id": "ord_x",
            "seq": 2,
            "from_state": "CREATED",
            "to_state": "ACCEPTED",
            "actor": "merchant:tablet",
            "created_at": "2026-10-28T12:00:00.123456",
        }
    )
    lines = format_event(event).decode().split("\n")
    assert lines[0] == "id: 7" and lines[1] == "event: order" and lines[3:] == ["", ""]
    assert json.loads(lines[2].removeprefix("data: ")) == {
        "id": 7,
        "order_id": "ord_x",
        "seq": 2,
        "from_state": "CREATED",
        "to_state": "ACCEPTED",
        "actor": "merchant:tablet",
        "created_at": "2026-10-28T12:00:00.123Z",
    }


def test_out_of_order_publish_is_repaired_by_a_catch_up_read(client: TestClient, app: FastAPI, world: World) -> None:
    """Publishing happens after the write lock is released, so event 4 can ring the bell before
    event 3 does. The stream must still emit 3 then 4, exactly once each."""
    order = make_order(client, world, "k1")
    advance(client, order["id"], "accept")  # events 1 and 2 are in the table
    engine, feed = app.state.engine, app.state.feed

    async def run() -> list[bytes]:
        stream = event_stream(engine, feed, after=0, heartbeat_seconds=0.2)
        chunks = [await anext(stream) for _ in range(3)]  # preamble, event 1, event 2
        quiet = OrderFeed()  # write two more events without ringing the subscribed feed's bell
        transition_order(engine, quiet, order["id"], "cancel", "api", None)  # event 3
        create_order(engine, quiet, "k2", OrderCreate.model_validate(order_body(world)), "api")  # event 4
        third, fourth = events_after(engine, 2)
        assert (third.id, fourth.id) == (3, 4)
        feed.publish(fourth)
        feed.publish(third)
        chunks += [await anext(stream) for _ in range(3)]  # 3 and 4 from the catch-up read, then a heartbeat
        await stream.aclose()
        return chunks

    chunks = asyncio.run(run())
    assert chunks[0] == PREAMBLE
    assert [chunk.split(b"\n", 1)[0] for chunk in chunks[1:5]] == [b"id: 1", b"id: 2", b"id: 3", b"id: 4"]
    assert chunks[5] == HEARTBEAT
    assert feed.subscriber_count == 0
