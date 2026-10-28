"""Nearest-courier assignment on a constructed geometry, plus the haversine helper itself."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.geo import haversine_km
from tests.support import World, advance, error_code, make_courier, make_order, make_world, metric_value


def test_haversine_matches_known_distances() -> None:
    assert haversine_km(0, 0, 0, 0) == 0.0
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.195, abs=0.01)  # one degree of longitude at the equator
    assert haversine_km(0, 0, 1, 0) == pytest.approx(111.195, abs=0.01)
    assert haversine_km(10, 20, 30, 40) == haversine_km(30, 40, 10, 20)


def test_assign_picks_the_nearest_available_courier(client: TestClient) -> None:
    world = make_world(client, lat=0.0, lng=0.0)
    far = make_courier(client, "far", 0.05, 0.05)  # about 7.9 km
    near = make_courier(client, "near", 0.005, 0.0)  # about 0.56 km
    mid = make_courier(client, "mid", 0.0, 0.02)  # about 2.2 km
    make_courier(client, "nearest-but-off-shift", 0.001, 0.0, available=False)

    first = make_order(client, world, "k1")
    assert advance(client, first["id"], "accept", "assign")["courier_id"] == near["id"]
    second = make_order(client, world, "k2")
    assert advance(client, second["id"], "accept", "assign")["courier_id"] == mid["id"]
    third = make_order(client, world, "k3")
    assert advance(client, third["id"], "accept", "assign")["courier_id"] == far["id"]
    free = client.get("/couriers", params={"available": "true"}).json()["items"]
    assert free == []


def test_assignment_uses_the_courier_position_at_assignment_time(client: TestClient) -> None:
    world = make_world(client, lat=0.0, lng=0.0)
    wanderer = make_courier(client, "wanderer", 1.0, 1.0)
    settled = make_courier(client, "settled", 0.1, 0.1)
    client.post(f"/couriers/{wanderer['id']}/location", json={"lat": 0.001, "lng": 0.001})
    order = make_order(client, world, "k1")
    assert advance(client, order["id"], "accept", "assign")["courier_id"] == wanderer["id"]
    assert client.get(f"/couriers/{settled['id']}").json()["available"] is True


def test_assign_without_available_courier_is_409_and_counted(client: TestClient, world: World) -> None:
    make_courier(client, "busy", 0.0, 0.0, available=False)
    order = make_order(client, world, "k1")
    advance(client, order["id"], "accept")
    response = client.post(f"/orders/{order['id']}/assign")
    assert response.status_code == 409
    assert error_code(response) == "no_courier_available"
    assert client.get(f"/orders/{order['id']}").json()["state"] == "ACCEPTED"
    exposition = client.get("/metrics").text
    assert metric_value(exposition, "assignment_conflicts_total") == 1
    assert metric_value(exposition, "assignments_total") == 0


def test_assign_requires_an_accepted_order(client: TestClient, world: World) -> None:
    courier = make_courier(client, "C", 0.0, 0.0)
    order = make_order(client, world, "k1")
    response = client.post(f"/orders/{order['id']}/assign")
    assert (response.status_code, error_code(response)) == (409, "invalid_transition")
    assert client.get(f"/couriers/{courier['id']}").json()["available"] is True
