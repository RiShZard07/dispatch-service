"""Concurrency: many threads hit one app instance backed by one file-based SQLite database.

Each request runs in its own worker thread with its own pooled connection, so the
write transactions genuinely contend for SQLite's write lock.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from tests.support import World, advance, error_code, make_courier, make_order, make_world, metric_value, order_body


def hit_together(calls: list[Callable[[], httpx.Response]]) -> list[httpx.Response]:
    """Run every call on its own thread, released at the same instant by a barrier."""
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], httpx.Response]) -> httpx.Response:
        barrier.wait(timeout=60)
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(run, calls))


def accepted_orders(client: TestClient, world: World, count: int) -> list[str]:
    ids = [make_order(client, world, f"conc-{index}")["id"] for index in range(count)]
    for order_id in ids:
        advance(client, order_id, "accept")
    return ids


def test_fifty_concurrent_assigns_with_fifty_couriers_book_fifty_distinct_couriers(
    client: TestClient, world: World
) -> None:
    for index in range(50):
        make_courier(client, f"c{index}", 0.001 * index, 0.0)
    order_ids = accepted_orders(client, world, 50)

    responses = hit_together([lambda oid=oid: client.post(f"/orders/{oid}/assign") for oid in order_ids])

    assert Counter(r.status_code for r in responses) == {200: 50}
    courier_ids = [r.json()["courier_id"] for r in responses]
    assert len(set(courier_ids)) == 50
    assert client.get("/couriers", params={"available": "true", "limit": 100}).json()["items"] == []
    assert metric_value(client.get("/metrics").text, "assignments_total") == 50


def test_hundred_concurrent_assigns_with_ten_couriers_give_exactly_ten_assignments(
    client: TestClient, world: World
) -> None:
    for index in range(10):
        make_courier(client, f"c{index}", 0.001 * index, 0.0)
    order_ids = accepted_orders(client, world, 100)

    responses = hit_together([lambda oid=oid: client.post(f"/orders/{oid}/assign") for oid in order_ids])

    assert Counter(r.status_code for r in responses) == {200: 10, 409: 90}
    winners = [r.json()["courier_id"] for r in responses if r.status_code == 200]
    assert len(set(winners)) == 10
    assert all(error_code(r) == "no_courier_available" for r in responses if r.status_code == 409)
    assigned = client.get("/orders", params={"state": "ASSIGNED", "limit": 100}).json()["items"]
    assert Counter(o["courier_id"] for o in assigned) == {courier: 1 for courier in winners}
    exposition = client.get("/metrics").text
    assert metric_value(exposition, "assignments_total") == 10
    assert metric_value(exposition, "assignment_conflicts_total") == 90


def test_two_app_instances_on_one_database_file_never_double_book(settings: Settings) -> None:
    """Two apps stand in for two processes: each has its own engine, pool and in-process write
    queue, so only SQLite's BEGIN IMMEDIATE stands between them and a double booking."""
    app_a, app_b = create_app(settings), create_app(settings)
    with TestClient(app_a) as a, TestClient(app_b) as b:
        world = make_world(a)
        for index in range(10):
            make_courier(b, f"c{index}", 0.001 * index, 0.0)
        order_ids = accepted_orders(a, world, 40)
        calls = [
            lambda oid=oid, client=client: client.post(f"/orders/{oid}/assign")
            for oid, client in zip(order_ids, [a, b] * 20, strict=True)
        ]

        responses = hit_together(calls)

        assert Counter(r.status_code for r in responses) == {200: 10, 409: 30}
        assert len({r.json()["courier_id"] for r in responses if r.status_code == 200}) == 10
        assert b.get("/couriers", params={"available": "true"}).json()["items"] == []
    app_a.state.engine.dispose()
    app_b.state.engine.dispose()


def test_concurrent_creates_with_one_idempotency_key_make_one_order(client: TestClient, world: World) -> None:
    body = order_body(world)
    responses = hit_together(
        [lambda: client.post("/orders", json=body, headers={"Idempotency-Key": "same-key"}) for _ in range(20)]
    )
    assert Counter(r.status_code for r in responses) == {201: 1, 200: 19}
    assert len({r.json()["id"] for r in responses}) == 1
    assert len(client.get("/orders").json()["items"]) == 1


def test_concurrent_transitions_on_one_order_apply_exactly_once(client: TestClient, world: World) -> None:
    order = make_order(client, world, "k1")
    responses = hit_together([lambda: client.post(f"/orders/{order['id']}/accept") for _ in range(20)])
    assert Counter(r.status_code for r in responses) == {200: 1, 409: 19}
    final = client.get(f"/orders/{order['id']}").json()
    assert final["state"] == "ACCEPTED" and final["version"] == 2
    assert len(client.get(f"/orders/{order['id']}/events").json()) == 2
