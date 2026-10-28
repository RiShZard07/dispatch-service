"""Helpers shared by the test modules. Everything created here is synthetic."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

ITEMS = [
    {"sku": "SYN-001", "quantity": 2, "unit_price_cents": 450},
    {"sku": "SYN-002", "quantity": 1, "unit_price_cents": 1200},
]
SUBTOTAL = 2 * 450 + 1 * 1200


@dataclass(frozen=True)
class World:
    """A merchant and a customer that orders can reference."""

    merchant_id: str
    customer_id: str


def make_world(client: TestClient, lat: float = 0.0, lng: float = 0.0) -> World:
    merchant = client.post("/merchants", json={"name": "Synthetic Merchant", "lat": lat, "lng": lng})
    customer = client.post("/customers")
    assert merchant.status_code == 201 and customer.status_code == 201
    return World(merchant.json()["id"], customer.json()["id"])


def make_courier(client: TestClient, name: str, lat: float, lng: float, available: bool = True) -> dict[str, Any]:
    response = client.post("/couriers", json={"name": name, "lat": lat, "lng": lng, "available": available})
    assert response.status_code == 201, response.text
    return response.json()


def order_body(world: World, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"merchant_id": world.merchant_id, "customer_id": world.customer_id, "items": items or ITEMS}


def make_order(client: TestClient, world: World, key: str, **headers: str) -> dict[str, Any]:
    response = client.post("/orders", json=order_body(world), headers={"Idempotency-Key": key, **headers})
    assert response.status_code == 201, response.text
    return response.json()


def advance(client: TestClient, order_id: str, *actions: str, **headers: str) -> dict[str, Any]:
    """Apply transitions in sequence, asserting each succeeds, and return the final order."""
    payload: dict[str, Any] = {}
    for action in actions:
        response = client.post(f"/orders/{order_id}/{action}", headers=headers)
        assert response.status_code == 200, f"{action}: {response.text}"
        payload = response.json()
    return payload


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def metric_value(exposition: str, series: str) -> float:
    """Value of one sample line, e.g. metric_value(text, 'assignments_total')."""
    for line in exposition.splitlines():
        if line.startswith(series + " "):
            return float(line.split(" ")[-1])
    raise AssertionError(f"series {series!r} not found in exposition")


def wait_until(condition: Callable[[], bool], timeout: float = 10.0, interval: float = 0.01) -> None:
    """Poll until condition() is true. Background threads make exact timing impossible; deadlines are not."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(interval)


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    """Serve the app with uvicorn on a free localhost port, in a thread, for the duration of the block.

    The TestClient buffers a whole response before returning it, so an endless
    server-sent event stream cannot be read through it. A real server can be, and
    it also exercises the lifespan (the outbox worker) exactly as production would.
    Close every streaming connection before leaving the block: the server waits for
    open responses to finish, up to the graceful timeout.
    """
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", lifespan="on", timeout_graceful_shutdown=2
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn-test", daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started or not thread.is_alive())
        assert thread.is_alive(), "uvicorn failed to start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
