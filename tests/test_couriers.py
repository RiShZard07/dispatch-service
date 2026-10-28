"""Courier API: registration, location and availability updates, listing and validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.support import error_code, make_courier


def test_create_courier_defaults_to_available_at_version_1(client: TestClient) -> None:
    courier = make_courier(client, "Synthetic Courier", 40.0, -75.0)
    assert courier["id"].startswith("cur_")
    assert courier["available"] is True and courier["version"] == 1
    assert client.get(f"/couriers/{courier['id']}").json() == courier


def test_location_update_moves_the_courier_and_bumps_version(client: TestClient) -> None:
    courier = make_courier(client, "C", 0.0, 0.0)
    moved = client.post(f"/couriers/{courier['id']}/location", json={"lat": 0.5, "lng": -0.5})
    assert moved.status_code == 200
    assert (moved.json()["lat"], moved.json()["lng"], moved.json()["version"]) == (0.5, -0.5, 2)


def test_availability_update_respects_if_match(client: TestClient) -> None:
    courier = make_courier(client, "C", 0.0, 0.0)
    off = client.post(f"/couriers/{courier['id']}/availability", json={"available": False}, headers={"If-Match": "1"})
    assert off.status_code == 200 and off.json()["available"] is False and off.json()["version"] == 2
    stale = client.post(f"/couriers/{courier['id']}/availability", json={"available": True}, headers={"If-Match": "1"})
    assert (stale.status_code, error_code(stale)) == (409, "version_conflict")
    assert client.get(f"/couriers/{courier['id']}").json()["available"] is False


def test_list_filters_on_availability_and_paginates(client: TestClient) -> None:
    make_courier(client, "A", 0.0, 0.0)
    make_courier(client, "B", 0.0, 0.0, available=False)
    make_courier(client, "C", 0.0, 0.0)
    everyone = client.get("/couriers").json()
    assert [c["name"] for c in everyone["items"]] == ["A", "B", "C"] and everyone["next_offset"] is None
    free = client.get("/couriers", params={"available": "true"}).json()["items"]
    assert [c["name"] for c in free] == ["A", "C"]
    busy = client.get("/couriers", params={"available": "false"}).json()["items"]
    assert [c["name"] for c in busy] == ["B"]
    page = client.get("/couriers", params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_offset"] == 2


def test_unknown_courier_is_404(client: TestClient) -> None:
    assert client.get("/couriers/cur_missing").status_code == 404
    response = client.post("/couriers/cur_missing/location", json={"lat": 0, "lng": 0})
    assert (response.status_code, error_code(response)) == (404, "not_found")


def test_coordinates_out_of_range_are_422(client: TestClient) -> None:
    response = client.post("/couriers", json={"name": "C", "lat": 91.0, "lng": 0.0})
    assert response.status_code == 422 and "lat" in response.json()["error"]["message"]
    courier = make_courier(client, "C", 0.0, 0.0)
    moved = client.post(f"/couriers/{courier['id']}/location", json={"lat": 0.0, "lng": 181.0})
    assert moved.status_code == 422 and "lng" in moved.json()["error"]["message"]
