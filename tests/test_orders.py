"""Order API: creation, validation, reads, pagination, transitions, If-Match and the audit trail."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.support import ITEMS, SUBTOTAL, World, advance, error_code, make_courier, make_order, order_body


def test_create_order_returns_201_and_computes_subtotal(client: TestClient, world: World) -> None:
    order = make_order(client, world, "key-1")
    assert order["id"].startswith("ord_")
    assert order["state"] == "CREATED"
    assert order["subtotal_cents"] == SUBTOTAL
    assert order["items"] == ITEMS
    assert order["version"] == 1 and order["courier_id"] is None
    assert order["created_at"].endswith("Z")


def test_get_order_round_trips_and_unknown_id_is_404(client: TestClient, world: World) -> None:
    order = make_order(client, world, "key-1")
    assert client.get(f"/orders/{order['id']}").json() == order
    missing = client.get("/orders/ord_does_not_exist")
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "not_found", "message": "order 'ord_does_not_exist' not found"}}


def test_missing_idempotency_key_is_422(client: TestClient, world: World) -> None:
    response = client.post("/orders", json=order_body(world))
    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert "idempotency-key" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("patch", "fragment"),
    [
        ({"items": []}, "items"),
        ({"items": [{"sku": "A", "quantity": 0, "unit_price_cents": 1}]}, "quantity"),
        ({"items": [{"sku": "A", "quantity": 1, "unit_price_cents": -1}]}, "unit_price_cents"),
        ({"items": [{"sku": "A", "quantity": 1, "unit_price_cents": 1, "note": "x"}]}, "note"),
        ({"merchant_id": ""}, "merchant_id"),
        ({"tip_cents": 100}, "tip_cents"),
    ],
)
def test_invalid_bodies_are_422_with_the_field_named(
    client: TestClient, world: World, patch: dict[str, Any], fragment: str
) -> None:
    response = client.post("/orders", json={**order_body(world), **patch}, headers={"Idempotency-Key": "k"})
    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert fragment in response.json()["error"]["message"]


def test_unknown_merchant_or_customer_is_422(client: TestClient, world: World) -> None:
    bad_merchant = client.post(
        "/orders", json={**order_body(world), "merchant_id": "mer_nope"}, headers={"Idempotency-Key": "a"}
    )
    bad_customer = client.post(
        "/orders", json={**order_body(world), "customer_id": "cus_nope"}, headers={"Idempotency-Key": "b"}
    )
    assert (bad_merchant.status_code, error_code(bad_merchant)) == (422, "unknown_merchant")
    assert (bad_customer.status_code, error_code(bad_customer)) == (422, "unknown_customer")


def test_list_filters_by_state_and_merchant(client: TestClient, world: World) -> None:
    other = World(client.post("/merchants", json={"name": "Other", "lat": 1, "lng": 1}).json()["id"], world.customer_id)
    first = make_order(client, world, "k1")
    second = make_order(client, world, "k2")
    third = make_order(client, other, "k3")
    advance(client, second["id"], "accept")

    everything = client.get("/orders").json()
    assert [o["id"] for o in everything["items"]] == [first["id"], second["id"], third["id"]]
    created = client.get("/orders", params={"state": "CREATED"}).json()["items"]
    assert {o["id"] for o in created} == {first["id"], third["id"]}
    mine = client.get("/orders", params={"merchant_id": world.merchant_id, "state": "ACCEPTED"}).json()["items"]
    assert [o["id"] for o in mine] == [second["id"]]
    assert client.get("/orders", params={"state": "FLYING"}).status_code == 422


def test_list_filters_by_courier_id_for_a_courier_app(client: TestClient, world: World) -> None:
    near = make_courier(client, "Near", 0.01, 0.0)
    far = make_courier(client, "Far", 0.05, 0.0)
    first = make_order(client, world, "k1")
    second = make_order(client, world, "k2")
    unassigned = make_order(client, world, "k3")
    advance(client, first["id"], "accept", "assign")  # takes the nearest courier
    advance(client, second["id"], "accept", "assign")  # only the far one is left
    advance(client, second["id"], "pickup")

    mine = client.get("/orders", params={"courier_id": near["id"]}).json()
    assert [o["id"] for o in mine["items"]] == [first["id"]] and mine["next_offset"] is None
    assert [o["id"] for o in client.get("/orders", params={"courier_id": far["id"]}).json()["items"]] == [second["id"]]
    picked_up = client.get("/orders", params={"courier_id": far["id"], "state": "PICKED_UP"}).json()["items"]
    assert [o["id"] for o in picked_up] == [second["id"]]
    assert client.get("/orders", params={"courier_id": near["id"], "state": "PICKED_UP"}).json()["items"] == []
    assert client.get("/orders", params={"courier_id": "cur_nobody"}).json()["items"] == []
    assert unassigned["id"] not in {o["id"] for o in mine["items"]}


def test_events_carry_a_global_id_in_commit_order_across_orders(client: TestClient, world: World) -> None:
    first = make_order(client, world, "k1")  # event 1
    second = make_order(client, world, "k2")  # event 2
    advance(client, second["id"], "accept")  # event 3
    advance(client, first["id"], "accept")  # event 4

    first_events = client.get(f"/orders/{first['id']}/events").json()
    second_events = client.get(f"/orders/{second['id']}/events").json()
    assert [(e["id"], e["seq"], e["order_id"]) for e in first_events] == [(1, 1, first["id"]), (4, 2, first["id"])]
    assert [(e["id"], e["seq"], e["order_id"]) for e in second_events] == [(2, 1, second["id"]), (3, 2, second["id"])]


def test_pagination_walks_every_order_exactly_once(client: TestClient, world: World) -> None:
    ids = [make_order(client, world, f"k{i}")["id"] for i in range(7)]
    seen, offset = [], 0
    while offset is not None:
        page = client.get("/orders", params={"limit": 3, "offset": offset}).json()
        assert page["limit"] == 3 and page["offset"] == offset
        seen += [o["id"] for o in page["items"]]
        offset = page["next_offset"]
    assert seen == ids
    assert client.get("/orders", params={"limit": 0}).status_code == 422
    assert client.get("/orders", params={"limit": 101}).status_code == 422
    assert client.get("/orders", params={"offset": -1}).status_code == 422


def test_full_lifecycle_and_courier_release(client: TestClient, world: World) -> None:
    courier = make_courier(client, "Near", 0.01, 0.0)
    order = make_order(client, world, "k1")
    assigned = advance(client, order["id"], "accept", "assign")
    assert assigned["state"] == "ASSIGNED" and assigned["courier_id"] == courier["id"]
    assert client.get(f"/couriers/{courier['id']}").json()["available"] is False
    delivered = advance(client, order["id"], "pickup", "deliver")
    assert delivered["state"] == "DELIVERED" and delivered["version"] == 5
    assert client.get(f"/couriers/{courier['id']}").json()["available"] is True


@pytest.mark.parametrize(
    ("setup", "action"),
    [((), "pickup"), ((), "deliver"), ((), "assign"), (("accept",), "pickup"), (("accept", "cancel"), "accept")],
)
def test_illegal_transitions_are_409_and_change_nothing(
    client: TestClient, world: World, setup: tuple[str, ...], action: str
) -> None:
    order = make_order(client, world, "k1")
    before = advance(client, order["id"], *setup) if setup else order
    response = client.post(f"/orders/{order['id']}/{action}")
    assert response.status_code == 409
    assert error_code(response) == "invalid_transition"
    assert client.get(f"/orders/{order['id']}").json() == before


def test_cancel_allowed_from_created_and_accepted_but_not_after_assignment(client: TestClient, world: World) -> None:
    make_courier(client, "C", 0.0, 0.0)
    fresh = make_order(client, world, "k1")
    assert advance(client, fresh["id"], "cancel")["state"] == "CANCELED"
    accepted = make_order(client, world, "k2")
    assert advance(client, accepted["id"], "accept", "cancel")["state"] == "CANCELED"
    assigned = make_order(client, world, "k3")
    advance(client, assigned["id"], "accept", "assign")
    assert client.post(f"/orders/{assigned['id']}/cancel").status_code == 409


def test_transitions_on_a_missing_order_are_404(client: TestClient) -> None:
    for action in ("accept", "assign", "pickup", "deliver", "cancel"):
        response = client.post(f"/orders/ord_missing/{action}")
        assert (response.status_code, error_code(response)) == (404, "not_found"), action
    assert client.get("/orders/ord_missing/events").status_code == 404


def test_if_match_with_stale_version_is_409(client: TestClient, world: World) -> None:
    order = make_order(client, world, "k1")
    accepted = client.post(f"/orders/{order['id']}/accept", headers={"If-Match": "1"})
    assert accepted.status_code == 200 and accepted.json()["version"] == 2
    stale = client.post(f"/orders/{order['id']}/cancel", headers={"If-Match": "1"})
    assert stale.status_code == 409
    assert error_code(stale) == "version_conflict"
    assert client.get(f"/orders/{order['id']}").json()["state"] == "ACCEPTED"
    quoted = client.post(f"/orders/{order['id']}/cancel", headers={"If-Match": '"2"'})
    assert quoted.status_code == 200 and quoted.json()["state"] == "CANCELED"


def test_malformed_if_match_is_422(client: TestClient, world: World) -> None:
    order = make_order(client, world, "k1")
    response = client.post(f"/orders/{order['id']}/accept", headers={"If-Match": "latest"})
    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_audit_trail_records_every_transition_and_nothing_else(client: TestClient, world: World) -> None:
    make_courier(client, "C", 0.0, 0.0)
    order = make_order(client, world, "k1", **{"X-Actor": "customer:app"})
    advance(client, order["id"], "accept", **{"X-Actor": "merchant:tablet"})
    advance(client, order["id"], "assign", **{"X-Actor": "dispatcher"})
    advance(client, order["id"], "pickup", "deliver", **{"X-Actor": "courier:phone"})
    assert client.post(f"/orders/{order['id']}/cancel").status_code == 409  # rejected, so no event

    events = client.get(f"/orders/{order['id']}/events").json()
    assert [(e["seq"], e["from_state"], e["to_state"], e["actor"]) for e in events] == [
        (1, None, "CREATED", "customer:app"),
        (2, "CREATED", "ACCEPTED", "merchant:tablet"),
        (3, "ACCEPTED", "ASSIGNED", "dispatcher"),
        (4, "ASSIGNED", "PICKED_UP", "courier:phone"),
        (5, "PICKED_UP", "DELIVERED", "courier:phone"),
    ]
    assert events[-1]["seq"] == client.get(f"/orders/{order['id']}").json()["version"]
    timestamps = [e["created_at"] for e in events]
    assert timestamps == sorted(timestamps)
