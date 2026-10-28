"""Idempotent order creation: same key and body replays, same key with a different body conflicts."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support import World, error_code, make_order, order_body


def test_same_key_and_body_returns_the_same_order_with_200(client: TestClient, world: World) -> None:
    first = client.post("/orders", json=order_body(world), headers={"Idempotency-Key": "retry-me"})
    second = client.post("/orders", json=order_body(world), headers={"Idempotency-Key": "retry-me"})
    assert (first.status_code, second.status_code) == (201, 200)
    assert first.json() == second.json()
    assert len(client.get("/orders").json()["items"]) == 1


def test_replay_is_based_on_body_meaning_not_formatting(client: TestClient, world: World) -> None:
    body = order_body(world)
    first = client.post("/orders", json=body, headers={"Idempotency-Key": "k"})
    reordered = {"items": body["items"], "customer_id": body["customer_id"], "merchant_id": body["merchant_id"]}
    second = client.post("/orders", json=reordered, headers={"Idempotency-Key": "k"})
    assert second.status_code == 200 and second.json()["id"] == first.json()["id"]


def test_same_key_with_a_different_body_is_409(client: TestClient, world: World) -> None:
    make_order(client, world, "k")
    changed = order_body(world, items=[{"sku": "SYN-001", "quantity": 3, "unit_price_cents": 450}])
    response = client.post("/orders", json=changed, headers={"Idempotency-Key": "k"})
    assert response.status_code == 409
    assert error_code(response) == "idempotency_conflict"
    assert len(client.get("/orders").json()["items"]) == 1


def test_different_keys_create_different_orders(client: TestClient, world: World) -> None:
    ids = {make_order(client, world, key)["id"] for key in ("a", "b", "c")}
    assert len(ids) == 3


def test_replay_after_transitions_returns_current_state_and_adds_no_event(client: TestClient, world: World) -> None:
    order = make_order(client, world, "k")
    client.post(f"/orders/{order['id']}/accept")
    replay = client.post("/orders", json=order_body(world), headers={"Idempotency-Key": "k"})
    assert replay.status_code == 200
    assert replay.json()["state"] == "ACCEPTED" and replay.json()["version"] == 2
    assert len(client.get(f"/orders/{order['id']}/events").json()) == 2
