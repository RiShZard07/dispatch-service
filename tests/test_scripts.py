"""Smoke tests for the seed and load scripts, run in-process against the test app."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.load import STEPS, LoadReport, percentile, run_load
from scripts.seed import seed
from scripts.webhook_receiver import Receiver, serve

REPO = Path(__file__).resolve().parent.parent


def test_seed_creates_the_requested_synthetic_entities(client: TestClient) -> None:
    ids = seed(client, merchants=2, customers=3, couriers=4, rng_seed=7)
    assert {kind: len(values) for kind, values in ids.items()} == {"merchants": 2, "customers": 3, "couriers": 4}
    assert len(client.get("/couriers", params={"available": "true"}).json()["items"]) == 4
    assert client.get(f"/merchants/{ids['merchants'][0]}").json()["name"] == "Synthetic Merchant 1"
    assert client.get(f"/customers/{ids['customers'][0]}").status_code == 200


def test_seed_is_reproducible_for_a_given_seed(client: TestClient) -> None:
    first = seed(client, merchants=1, customers=0, couriers=1, rng_seed=1)
    second = seed(client, merchants=1, customers=0, couriers=1, rng_seed=1)
    merchant_a = client.get(f"/merchants/{first['merchants'][0]}").json()
    merchant_b = client.get(f"/merchants/{second['merchants'][0]}").json()
    assert (merchant_a["lat"], merchant_a["lng"]) == (merchant_b["lat"], merchant_b["lng"])


def test_scripts_have_a_working_command_line() -> None:
    for script in ("seed.py", "load.py", "export_to_s3.py", "webhook_receiver.py"):
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / script), "--help"], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr
        assert "--base-url" in result.stdout or "--bucket" in result.stdout or "--fail-first" in result.stdout


def test_webhook_receiver_prints_each_delivery_and_flags_repeats(capsys: pytest.CaptureFixture[str]) -> None:
    receiver = Receiver(("127.0.0.1", 0), fail_first=1)
    serve(receiver)
    order = {"id": "ord_x", "state": "ACCEPTED"}
    delivery = {"id": 7, "type": "order.accepted", "attempt": 1, "data": {"order": order}}
    try:
        with httpx.Client(timeout=5) as http:
            first = http.post(receiver.url, json=delivery)
            second = http.post(receiver.url, json={**delivery, "attempt": 2})
            third = http.post(receiver.url, json={**delivery, "attempt": 3})
            malformed = http.post(receiver.url, content=b"not json", headers={"Content-Type": "application/json"})
    finally:
        receiver.shutdown()
        receiver.server_close()
    assert (first.status_code, second.status_code, third.status_code, malformed.status_code) == (500, 200, 200, 400)
    assert len(receiver.deliveries) == 3 and receiver.seen == {7}
    lines = capsys.readouterr().out.splitlines()
    summary = "delivery id=7 type=order.accepted attempt={} order=ord_x state=ACCEPTED"
    assert lines[0] == summary.format(1) + " -> answering 500 on purpose"
    assert lines[1] == summary.format(2)
    assert lines[2].endswith("attempt=3 order=ord_x state=ACCEPTED -> duplicate, already processed, ignoring")


def test_percentile_uses_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 95) == 95.0
    assert percentile(values, 99) == 99.0
    assert percentile([], 50) == 0.0
    assert percentile([3.0], 99) == 3.0


def test_load_script_completes_every_lifecycle(app: FastAPI) -> None:
    async def run() -> LoadReport:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://dispatch.test") as client:
            return await run_load(client, orders=6, concurrency=3)

    report = asyncio.run(run())
    assert report.completed == 6 and report.errors == 0
    assert report.requests == 6 * len(STEPS)
    assert len(report.lifecycle_latencies) == 6
    lines = report.lines()
    assert lines[0].startswith("dispatch-service load run")
    assert any(line.strip().startswith("lifecycle") for line in lines)
