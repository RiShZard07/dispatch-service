"""CORS: preflight and simple requests from configured browser origins, and nothing without configuration."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app
from tests.support import error_code, make_world, metric_value, order_body

ALLOWED = "http://localhost:5173"
PREFLIGHT = {
    "Origin": ALLOWED,
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type,idempotency-key,if-match",
}


@pytest.fixture
def cors_app(settings: Settings) -> Iterator[FastAPI]:
    app = create_app(replace(settings, cors_origins=(ALLOWED, "https://dashboard.example")))
    yield app
    app.state.engine.dispose()


@pytest.fixture
def cors_client(cors_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(cors_app) as client:
        yield client


def test_preflight_from_an_allowed_origin_is_answered(cors_client: TestClient) -> None:
    response = cors_client.options("/orders", headers=PREFLIGHT)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert "POST" in response.headers["access-control-allow-methods"]
    allowed_headers = response.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed_headers and "if-match" in allowed_headers
    assert response.headers["access-control-max-age"] == "600"
    assert "x-request-id" in response.headers  # the preflight still went through the observability layer
    exposition = cors_client.get("/metrics").text  # and was counted, under "unmatched" since the router never ran
    assert metric_value(exposition, 'http_requests_total{method="OPTIONS",route="unmatched",status="200"}') == 1


def test_preflight_from_an_unlisted_origin_is_refused(cors_client: TestClient) -> None:
    response = cors_client.options("/orders", headers={**PREFLIGHT, "Origin": "http://evil.example"})
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_simple_requests_carry_the_origin_and_expose_the_request_id(cors_client: TestClient) -> None:
    world = make_world(cors_client)
    created = cors_client.post("/orders", json=order_body(world), headers={"Origin": ALLOWED, "Idempotency-Key": "k1"})
    assert created.status_code == 201
    assert created.headers["access-control-allow-origin"] == ALLOWED
    assert "x-request-id" in created.headers["access-control-expose-headers"].lower()
    missing = cors_client.get("/orders/ord_missing", headers={"Origin": ALLOWED})
    assert missing.status_code == 404 and missing.headers["access-control-allow-origin"] == ALLOWED


def test_a_413_from_the_body_limit_still_carries_cors_headers(cors_client: TestClient) -> None:
    oversized = json.dumps({"merchant_id": "x" * 70_000}).encode()
    response = cors_client.post(
        "/orders",
        content=oversized,
        headers={"Origin": ALLOWED, "Idempotency-Key": "k", "Content-Type": "application/json"},
    )
    assert response.status_code == 413 and error_code(response) == "payload_too_large"
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_without_configuration_no_cors_headers_are_sent(client: TestClient) -> None:
    simple = client.get("/health", headers={"Origin": ALLOWED})
    assert simple.status_code == 200 and "access-control-allow-origin" not in simple.headers
    preflight = client.options("/orders", headers=PREFLIGHT)
    assert preflight.status_code == 405 and error_code(preflight) == "method_not_allowed"
    assert "access-control-allow-origin" not in preflight.headers
