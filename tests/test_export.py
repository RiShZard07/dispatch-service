"""S3 export, exercised only against moto's in-memory mock. No live AWS account is involved."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import update

from app.config import Settings
from app.db import write_transaction
from app.export import ExportError, export_day, object_keys
from app.models import orders
from scripts.export_to_s3 import main as export_main
from tests.support import World, advance, make_courier, make_order

BUCKET = "dispatch-synthetic-exports"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3")
        client.create_bucket(Bucket=BUCKET)
        yield client


def deliver_orders(client: TestClient, world: World, count: int) -> list[dict[str, Any]]:
    for index in range(count):
        make_courier(client, f"c{index}", 0.0, 0.0)
    delivered = []
    for index in range(count):
        order = make_order(client, world, f"exp-{index}")
        delivered.append(advance(client, order["id"], "accept", "assign", "pickup", "deliver"))
    return delivered


def read_object(s3: Any, key: str) -> bytes:
    return s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()


def test_export_writes_ndjson_that_reconciles_with_the_database(
    client: TestClient, app: FastAPI, world: World, s3: Any
) -> None:
    delivered = deliver_orders(client, world, 3)
    pending = make_order(client, world, "still-open")
    canceled = make_order(client, world, "gone")
    advance(client, canceled["id"], "cancel")
    day = date.fromisoformat(delivered[0]["updated_at"][:10])

    result = export_day(app.state.engine, s3, BUCKET, day)

    data_key, manifest_key = object_keys("orders", day)
    assert (result.object_key, result.manifest_key, result.record_count) == (data_key, manifest_key, 3)
    lines = read_object(s3, data_key).decode().splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["id"] for r in records] == [o["id"] for o in delivered]
    assert all(r["state"] == "DELIVERED" and r["synthetic"] is True for r in records)
    assert all(r["delivered_at"] == o["updated_at"] for r, o in zip(records, delivered, strict=True))
    assert pending["id"] not in {r["id"] for r in records}
    manifest = json.loads(read_object(s3, manifest_key))
    assert manifest["record_count"] == 3 and manifest["sha256"] == result.sha256
    assert manifest["byte_size"] == len(read_object(s3, data_key))


def test_export_selects_only_the_requested_utc_day(client: TestClient, app: FastAPI, world: World, s3: Any) -> None:
    delivered = deliver_orders(client, world, 2)
    day = date.fromisoformat(delivered[0]["updated_at"][:10])
    moved = delivered[1]["id"]
    with write_transaction(app.state.engine) as conn:  # move one delivery to the previous day, in the database
        row = conn.execute(orders.select().where(orders.c.id == moved)).one()
        conn.execute(update(orders).where(orders.c.id == moved).values(updated_at=row.updated_at - timedelta(days=1)))

    today = export_day(app.state.engine, s3, BUCKET, day)
    yesterday = export_day(app.state.engine, s3, BUCKET, day - timedelta(days=1))
    empty = export_day(app.state.engine, s3, BUCKET, day - timedelta(days=30))

    assert (today.record_count, yesterday.record_count, empty.record_count) == (1, 1, 0)
    assert json.loads(read_object(s3, yesterday.object_key))["id"] == moved
    assert read_object(s3, empty.object_key) == b""


def test_rerunning_an_export_overwrites_with_identical_content(
    client: TestClient, app: FastAPI, world: World, s3: Any
) -> None:
    delivered = deliver_orders(client, world, 2)
    day = date.fromisoformat(delivered[0]["updated_at"][:10])

    first = export_day(app.state.engine, s3, BUCKET, day)
    first_data, first_manifest = read_object(s3, first.object_key), read_object(s3, first.manifest_key)
    second = export_day(app.state.engine, s3, BUCKET, day)

    assert first == second
    assert read_object(s3, second.object_key) == first_data
    assert read_object(s3, second.manifest_key) == first_manifest
    listed = s3.list_objects_v2(Bucket=BUCKET)["Contents"]
    assert sorted(obj["Key"] for obj in listed) == sorted([first.object_key, first.manifest_key])


def test_missing_bucket_gives_a_clear_error(app: FastAPI, s3: Any) -> None:
    with pytest.raises(ExportError, match="bucket 'no-such-bucket' does not exist"):
        export_day(app.state.engine, s3, "no-such-bucket", date(2026, 1, 1))


def test_export_cli_reads_its_configuration_from_the_environment(
    client: TestClient,
    app: FastAPI,
    world: World,
    settings: Settings,
    s3: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    delivered = deliver_orders(client, world, 1)
    day = delivered[0]["updated_at"][:10]
    monkeypatch.setenv("DISPATCH_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("DISPATCH_S3_BUCKET", BUCKET)
    monkeypatch.setenv("DISPATCH_S3_PREFIX", "exports")

    assert export_main(["--day", day]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["record_count"] == 1 and summary["object_key"] == f"exports/{day}.jsonl"
    assert read_object(s3, summary["object_key"]).count(b"\n") == 1

    assert export_main(["--day", day, "--bucket", "no-such-bucket"]) == 1
    assert "does not exist" in capsys.readouterr().err
