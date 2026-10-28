"""Request ids, JSON logging, the body limit, health and the error envelope for framework errors."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import make_engine
from app.request_logging import JsonFormatter, request_id_var
from tests.support import World, error_code, order_body


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    first = client.get("/health")
    second = client.get("/health")
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


def test_request_id_is_echoed_when_valid_and_replaced_when_not(client: TestClient) -> None:
    assert client.get("/health", headers={"X-Request-ID": "trace-123"}).headers["x-request-id"] == "trace-123"
    replaced = client.get("/health", headers={"X-Request-ID": "bad id with spaces"}).headers["x-request-id"]
    assert replaced != "bad id with spaces" and len(replaced) == 32


def test_request_log_line_carries_the_request_id(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="dispatch.http"):
        client.get("/health", headers={"X-Request-ID": "trace-xyz"})
    records = [r for r in caplog.records if getattr(r, "fields", {}).get("request_id") == "trace-xyz"]
    assert len(records) == 1
    fields = records[0].fields  # type: ignore[attr-defined]
    assert fields["method"] == "GET" and fields["route"] == "/health" and fields["status"] == 200
    assert fields["duration_ms"] >= 0
    rendered = json.loads(JsonFormatter().format(records[0]))
    assert rendered["msg"] == "request" and rendered["request_id"] == "trace-xyz" and rendered["level"] == "INFO"


def test_request_id_context_is_cleared_between_requests(client: TestClient) -> None:
    client.get("/health", headers={"X-Request-ID": "lingering"})
    assert request_id_var.get() == "-"


def test_declared_body_over_the_limit_is_413(client: TestClient, world: World) -> None:
    oversized = json.dumps({**order_body(world), "merchant_id": "x" * 70_000}).encode()  # above 64 KiB
    response = client.post(
        "/orders", content=oversized, headers={"Idempotency-Key": "k", "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert error_code(response) == "payload_too_large"


def test_streamed_body_over_the_limit_is_413(client: TestClient) -> None:
    chunks = iter([b"{" + b" " * 70_000 + b"}"])  # no Content-Length header: sent chunked
    response = client.post(
        "/orders", content=chunks, headers={"Idempotency-Key": "k", "Content-Type": "application/json"}
    )
    assert response.status_code == 413
    assert error_code(response) == "payload_too_large"


def test_unknown_route_and_wrong_method_use_the_error_envelope(client: TestClient) -> None:
    missing = client.get("/no-such-route")
    assert missing.status_code == 404 and missing.json() == {"error": {"code": "not_found", "message": "Not Found"}}
    wrong = client.delete("/orders")
    assert wrong.status_code == 405 and error_code(wrong) == "method_not_allowed"


def test_unexpected_exceptions_become_500_with_the_envelope(app: FastAPI) -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    app.add_api_route("/explode", explode, methods=["GET"])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/explode")
    assert response.status_code == 500
    assert response.json() == {"error": {"code": "internal_error", "message": "unexpected server error"}}
    assert "x-request-id" in response.headers


def test_health_reports_ok_and_503_when_the_database_is_unreachable(client: TestClient, app: FastAPI) -> None:
    assert client.get("/health").json() == {"status": "ok", "database": "ok"}
    working = app.state.engine
    app.state.engine = make_engine("sqlite:////nonexistent-dir/for-sure/dispatch.sqlite")
    try:
        response = client.get("/health")
    finally:
        app.state.engine = working
    assert response.status_code == 503
    assert error_code(response) == "service_unavailable"
