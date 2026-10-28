"""The transactional outbox and the webhook worker, delivering to the repository's own receiver script."""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from app.config import Settings
from app.db import read_connection, write_transaction
from app.factory import create_app
from app.metrics import build_metrics
from app.models import outbox
from app.outbox import BATCH_SIZE, MAX_ATTEMPTS, OutboxWorker, backoff_seconds, claim, enqueue
from scripts.webhook_receiver import Receiver, serve
from tests.support import World, advance, error_code, make_courier, make_order, make_world, metric_value, wait_until

FAST = {"webhook_backoff_seconds": 0.02, "outbox_poll_seconds": 0.05, "webhook_timeout_seconds": 2.0}


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    server = Receiver(("127.0.0.1", 0), quiet=True)
    serve(server)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def failing_receiver() -> Iterator[Receiver]:
    server = Receiver(("127.0.0.1", 0), fail_first=2, quiet=True)
    serve(server)
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def webhook_app(settings: Settings, receiver: Receiver) -> Iterator[FastAPI]:
    """An app whose outbox worker posts to the receiver, with short backoff and poll intervals."""
    app = create_app(replace(settings, webhook_url=receiver.url, **FAST))
    yield app
    app.state.engine.dispose()


def outbox_rows(engine: Engine) -> list[dict[str, Any]]:
    with read_connection(engine) as conn:
        return [dict(row._mapping) for row in conn.execute(select(outbox).order_by(outbox.c.id)).all()]


def free_port() -> int:
    """A port nothing listens on, so a webhook to it fails with a connection error."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_every_transition_writes_one_outbox_row_in_the_same_transaction(
    client: TestClient, app: FastAPI, world: World
) -> None:
    make_courier(client, "C", 0.0, 0.0)
    order = make_order(client, world, "k1", **{"X-Actor": "customer:app"})
    delivered = advance(client, order["id"], "accept", "assign", "pickup", "deliver")
    events = client.get(f"/orders/{order['id']}/events").json()

    rows = outbox_rows(app.state.engine)
    assert [row["event_type"] for row in rows] == [
        "order.created",
        "order.accepted",
        "order.assigned",
        "order.picked_up",
        "order.delivered",
    ]
    assert all(row["attempts"] == 0 and row["delivered_at"] is None and row["last_error"] is None for row in rows)
    assert all(row["next_attempt_at"] == row["created_at"] for row in rows)  # due immediately
    payloads = [json.loads(row["payload"]) for row in rows]
    assert [p["event_id"] for p in payloads] == [e["id"] for e in events]
    assert [p["seq"] for p in payloads] == [1, 2, 3, 4, 5]
    assert payloads[0]["actor"] == "customer:app" and payloads[0]["from_state"] is None
    assert payloads[2]["order"]["courier_id"] == delivered["courier_id"]
    assert payloads[4]["order"] == {
        "id": order["id"],
        "state": "DELIVERED",
        "courier_id": delivered["courier_id"],
        "version": 5,
        "merchant_id": world.merchant_id,
        "customer_id": world.customer_id,
    }
    assert payloads[4]["occurred_at"] == delivered["updated_at"]


def test_failed_transitions_and_replays_write_no_outbox_row(client: TestClient, app: FastAPI, world: World) -> None:
    order = make_order(client, world, "k1")
    assert len(outbox_rows(app.state.engine)) == 1
    assert error_code(client.post(f"/orders/{order['id']}/pickup")) == "invalid_transition"
    advance(client, order["id"], "accept")
    assert error_code(client.post(f"/orders/{order['id']}/assign")) == "no_courier_available"
    assert error_code(client.post(f"/orders/{order['id']}/cancel", headers={"If-Match": "1"})) == "version_conflict"
    replay = client.post(
        "/orders",
        json={"merchant_id": world.merchant_id, "customer_id": world.customer_id, "items": order["items"]},
        headers={"Idempotency-Key": "k1"},
    )
    assert replay.status_code == 200
    assert [row["event_type"] for row in outbox_rows(app.state.engine)] == ["order.created", "order.accepted"]


def test_worker_delivers_each_row_once_to_the_local_receiver(webhook_app: FastAPI, receiver: Receiver) -> None:
    with TestClient(webhook_app) as client:
        world = make_world(client)
        order = make_order(client, world, "k1")
        advance(client, order["id"], "accept")
        wait_until(lambda: len(receiver.deliveries) == 2)
        wait_until(lambda: all(row["delivered_at"] for row in outbox_rows(webhook_app.state.engine)))
        time.sleep(0.2)  # long enough for a wrongful second delivery to show up
        exposition = client.get("/metrics").text

    assert [(d["id"], d["type"], d["attempt"]) for d in receiver.deliveries] == [
        (1, "order.created", 1),
        (2, "order.accepted", 1),
    ]
    body = receiver.deliveries[1]
    assert body["synthetic"] is True and body["created_at"].endswith("Z")
    assert body["data"]["order_id"] == order["id"] and body["data"]["order"]["state"] == "ACCEPTED"
    headers = receiver.headers[1]
    assert headers["x-dispatch-delivery-id"] == "2" and headers["x-dispatch-event-type"] == "order.accepted"
    assert headers["x-dispatch-attempt"] == "1" and headers["content-type"] == "application/json"
    assert headers["user-agent"].startswith("dispatch-service/")
    rows = outbox_rows(webhook_app.state.engine)
    assert [(row["attempts"], row["last_error"]) for row in rows] == [(1, None), (1, None)]
    assert metric_value(exposition, "webhook_deliveries_total") == 2
    assert metric_value(exposition, "webhook_failures_total") == 0
    assert metric_value(exposition, 'outbox_rows{status="delivered"}') == 2
    assert metric_value(exposition, 'outbox_rows{status="pending"}') == 0


def test_failed_deliveries_are_retried_until_the_receiver_accepts(
    settings: Settings, failing_receiver: Receiver
) -> None:
    app = create_app(replace(settings, webhook_url=failing_receiver.url, **FAST))
    try:
        with TestClient(app) as client:
            make_order(client, make_world(client), "k1")
            wait_until(lambda: any(row["delivered_at"] for row in outbox_rows(app.state.engine)))
            exposition = client.get("/metrics").text
        assert [(d["id"], d["attempt"]) for d in failing_receiver.deliveries] == [(1, 1), (1, 2), (1, 3)]
        (row,) = outbox_rows(app.state.engine)
        assert row["attempts"] == 3 and row["last_error"] is None
        assert metric_value(exposition, "webhook_failures_total") == 2
        assert metric_value(exposition, "webhook_deliveries_total") == 1
    finally:
        app.state.engine.dispose()


def test_run_once_schedules_exponential_backoff_and_gives_up_after_five_attempts(
    client: TestClient, app: FastAPI, world: World
) -> None:
    """Drive the delivery pass by hand with a fake clock, so the schedule can be checked exactly."""
    always_failing = Receiver(("127.0.0.1", 0), fail_first=100, quiet=True)
    serve(always_failing)
    worker = OutboxWorker(app.state.engine, app.state.metrics, always_failing.url, backoff_base_seconds=1.0)
    worker.clock = lambda: now
    try:
        make_order(client, world, "k1")
        now = outbox_rows(app.state.engine)[0]["next_attempt_at"]  # the fake clock starts when the row is due
        schedule = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            outcomes = worker.run_once()
            assert [(o.row_id, o.attempt, o.delivered, o.error) for o in outcomes] == [(1, attempt, False, "HTTP 500")]
            (row,) = outbox_rows(app.state.engine)
            schedule.append((row["next_attempt_at"] - now).total_seconds())
            assert row["attempts"] == attempt and row["last_error"] == "HTTP 500" and row["delivered_at"] is None
            now = row["next_attempt_at"]  # jump the clock to the moment the row is due again
        assert schedule == [1.0, 2.0, 4.0, 8.0, 16.0]
        assert worker.run_once() == []  # exhausted: never tried again
        assert len(always_failing.deliveries) == MAX_ATTEMPTS
        assert metric_value(client.get("/metrics").text, 'outbox_rows{status="exhausted"}') == 1
    finally:
        always_failing.shutdown()
        always_failing.server_close()


def test_row_is_not_due_before_its_backoff_has_elapsed(client: TestClient, app: FastAPI, world: World) -> None:
    always_failing = Receiver(("127.0.0.1", 0), fail_first=100, quiet=True)
    serve(always_failing)
    worker = OutboxWorker(app.state.engine, app.state.metrics, always_failing.url, backoff_base_seconds=10.0)
    worker.clock = lambda: now
    try:
        make_order(client, world, "k1")
        now = outbox_rows(app.state.engine)[0]["next_attempt_at"]
        assert len(worker.run_once()) == 1
        now += timedelta(seconds=9.9)
        assert worker.run_once() == []  # the first retry is 10 s out
        now += timedelta(seconds=0.2)
        assert len(worker.run_once()) == 1
        assert len(always_failing.deliveries) == 2
    finally:
        always_failing.shutdown()
        always_failing.server_close()


def test_backoff_doubles_from_the_base() -> None:
    assert [backoff_seconds(n, 1.0) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert [backoff_seconds(n, 0.5) for n in range(1, 4)] == [0.5, 1.0, 2.0]


def test_nothing_is_delivered_when_the_webhook_url_is_unset(
    client: TestClient, app: FastAPI, world: World, receiver: Receiver
) -> None:
    assert app.state.outbox_worker is None
    make_order(client, world, "k1")
    time.sleep(0.3)
    assert receiver.deliveries == []
    (row,) = outbox_rows(app.state.engine)
    assert row["attempts"] == 0 and row["delivered_at"] is None
    assert metric_value(client.get("/metrics").text, 'outbox_rows{status="pending"}') == 1


def test_claim_is_a_compare_and_set_so_two_workers_cannot_both_try_a_row(app: FastAPI) -> None:
    now = datetime(2026, 10, 28, 12, 0, 0)
    with write_transaction(app.state.engine) as conn:
        enqueue(conn, "order.created", {"synthetic": True}, now)
    with read_connection(app.state.engine) as conn:
        row = conn.execute(select(outbox)).one()
    lease = timedelta(seconds=5)
    assert claim(app.state.engine, row, now, lease) is True
    assert claim(app.state.engine, row, now, lease) is False  # the row moved on: attempts is 1 now
    (stored,) = outbox_rows(app.state.engine)
    assert stored["attempts"] == 1 and stored["next_attempt_at"] == now + lease


def test_unreachable_receiver_is_recorded_as_an_error_and_retried(
    client: TestClient, app: FastAPI, world: World
) -> None:
    worker = OutboxWorker(app.state.engine, app.state.metrics, f"http://127.0.0.1:{free_port()}/webhook")
    make_order(client, world, "k1")
    (outcome,) = worker.run_once()
    assert outcome.delivered is False and outcome.error is not None and outcome.error.startswith("ConnectError")
    (row,) = outbox_rows(app.state.engine)
    assert row["attempts"] == 1 and row["last_error"] == outcome.error and row["delivered_at"] is None
    assert row["next_attempt_at"] > row["created_at"]


def test_worker_starts_with_the_app_stops_with_it_and_can_start_again(webhook_app: FastAPI) -> None:
    worker = webhook_app.state.outbox_worker
    assert worker is not None and not worker.running
    with TestClient(webhook_app):
        assert worker.running
    assert not worker.running
    with TestClient(webhook_app):
        assert worker.running
    assert not worker.running


def test_stop_interrupts_a_long_poll_sleep(settings: Settings, receiver: Receiver) -> None:
    app = create_app(replace(settings, webhook_url=receiver.url, outbox_poll_seconds=60.0))
    worker = app.state.outbox_worker
    try:
        worker.start()
        started = time.monotonic()
        worker.stop()
        assert time.monotonic() - started < 5 and not worker.running
    finally:
        app.state.engine.dispose()


def test_a_commit_wakes_the_worker_instead_of_waiting_for_the_poll(settings: Settings, receiver: Receiver) -> None:
    app = create_app(replace(settings, webhook_url=receiver.url, outbox_poll_seconds=60.0))
    try:
        with TestClient(app) as client:
            make_order(client, make_world(client), "k1")
            wait_until(lambda: len(receiver.deliveries) == 1, timeout=5)
    finally:
        app.state.engine.dispose()


def test_drain_clears_a_backlog_larger_than_one_batch_without_sleeping(app: FastAPI, receiver: Receiver) -> None:
    now = datetime(2026, 10, 28, 12, 0, 0)
    backlog = BATCH_SIZE * 2 + 7
    with write_transaction(app.state.engine) as conn:
        for index in range(backlog):
            enqueue(conn, "order.created", {"index": index, "synthetic": True}, now)
    worker = OutboxWorker(app.state.engine, app.state.metrics, receiver.url)
    worker.clock = lambda: now

    assert len(worker.run_once()) == BATCH_SIZE  # one pass is one batch
    assert len(worker.drain()) == backlog - BATCH_SIZE  # drain keeps going until a pass comes up short
    assert worker.drain() == []
    assert [d["data"]["index"] for d in receiver.deliveries] == list(range(backlog))  # in id order, once each
    assert all(row["delivered_at"] for row in outbox_rows(app.state.engine))


def test_outbox_metrics_start_at_zero_for_every_status() -> None:
    metrics = build_metrics(lambda: {}, [], lambda: {})
    text = metrics.registry.render()
    for status in ("pending", "delivered", "exhausted"):
        assert metric_value(text, f'outbox_rows{{status="{status}"}}') == 0
    assert metric_value(text, "webhook_deliveries_total") == 0
    assert metric_value(text, "webhook_failures_total") == 0
