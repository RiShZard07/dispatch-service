"""Export one UTC day of DELIVERED orders as newline-delimited JSON to S3.

DELIVERED is a terminal state, so an order's updated_at is the moment it was
delivered; the export selects DELIVERED orders whose updated_at falls on the
given day. Object keys are deterministic (<prefix>/YYYY-MM-DD.jsonl plus a
manifest next to it) and the content is sorted and canonically serialized, so
re-running a day overwrites both objects with byte-identical content instead of
producing duplicates. The data is synthetic.

This module has only been exercised against moto's in-memory S3 mock, never a
live bucket. See LEARNING_GUIDE.md for what changes with real S3.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from botocore.exceptions import ClientError
from sqlalchemy import Engine, select

from app.db import read_connection
from app.models import orders
from app.services.orders import row_to_order
from app.state import OrderState

DEFAULT_PREFIX = "orders"
SCHEMA_VERSION = 1


class ObjectStore(Protocol):
    """The one boto3 S3 client method the export uses, so tests can pass any client."""

    def put_object(self, **kwargs: Any) -> Any: ...


class ExportError(Exception):
    """Raised when the export cannot be written to the target bucket."""


@dataclass(frozen=True)
class ExportResult:
    """What was written and where."""

    day: date
    bucket: str
    object_key: str
    manifest_key: str
    record_count: int
    byte_size: int
    sha256: str


def object_keys(prefix: str, day: date) -> tuple[str, str]:
    """Deterministic data and manifest keys for a day."""
    return f"{prefix}/{day.isoformat()}.jsonl", f"{prefix}/{day.isoformat()}.manifest.json"


def delivered_orders(engine: Engine, day: date) -> list[dict[str, Any]]:
    """DELIVERED orders delivered on the given UTC day, oldest first, as JSON-ready records."""
    start = datetime(day.year, day.month, day.day)
    stmt = (
        select(orders)
        .where(orders.c.state == OrderState.DELIVERED.value)
        .where(orders.c.updated_at >= start, orders.c.updated_at < start + timedelta(days=1))
        .order_by(orders.c.updated_at, orders.c.id)
    )
    with read_connection(engine) as conn:
        rows = conn.execute(stmt).all()
    records = []
    for row in rows:
        order = row_to_order(row).model_dump(mode="json")
        records.append({**order, "delivered_at": order["updated_at"], "synthetic": True})
    return records


def to_ndjson(records: list[dict[str, Any]]) -> bytes:
    """One canonical JSON object per line; identical input always gives identical bytes."""
    return "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records).encode()


def export_day(
    engine: Engine, store: ObjectStore, bucket: str, day: date, prefix: str = DEFAULT_PREFIX
) -> ExportResult:
    """Write the day's records and a manifest describing them. Safe to re-run."""
    body = to_ndjson(delivered_orders(engine, day))
    digest = hashlib.sha256(body).hexdigest()
    object_key, manifest_key = object_keys(prefix, day)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "day": day.isoformat(),
        "object_key": object_key,
        "record_count": body.count(b"\n"),
        "byte_size": len(body),
        "sha256": digest,
        "synthetic": True,
    }
    try:
        store.put_object(Bucket=bucket, Key=object_key, Body=body, ContentType="application/x-ndjson")
        store.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=json.dumps(manifest, sort_keys=True, indent=2).encode(),
            ContentType="application/json",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        if code == "NoSuchBucket":
            raise ExportError(f"bucket {bucket!r} does not exist; create it or fix DISPATCH_S3_BUCKET") from exc
        raise ExportError(f"S3 request failed with {code}: {exc}") from exc
    return ExportResult(day, bucket, object_key, manifest_key, manifest["record_count"], len(body), digest)
