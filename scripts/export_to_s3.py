"""Export one UTC day of DELIVERED orders to S3, or to any S3-compatible endpoint.

    DISPATCH_S3_BUCKET=my-bucket python3 scripts/export_to_s3.py --day 2026-10-28

Environment variables (flags override them):
    DISPATCH_DATABASE_URL      database to read (default sqlite:///./dispatch.sqlite)
    DISPATCH_S3_BUCKET         target bucket
    DISPATCH_S3_ENDPOINT_URL   optional endpoint for S3-compatible object stores
    DISPATCH_S3_PREFIX         key prefix (default "orders")
Credentials and region come from the standard AWS variables (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION) or any other boto3 credential source.

Only exercised against moto's in-memory S3 mock so far, never a live account.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # allow `python3 scripts/export_to_s3.py`

import boto3  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import make_engine  # noqa: E402
from app.export import DEFAULT_PREFIX, ExportError, export_day  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export DELIVERED orders for one UTC day as NDJSON to S3.")
    parser.add_argument("--day", type=date.fromisoformat, default=datetime.now(UTC).date(), help="YYYY-MM-DD, UTC")
    parser.add_argument("--bucket", default=os.environ.get("DISPATCH_S3_BUCKET"))
    parser.add_argument("--endpoint-url", default=os.environ.get("DISPATCH_S3_ENDPOINT_URL"))
    parser.add_argument("--prefix", default=os.environ.get("DISPATCH_S3_PREFIX", DEFAULT_PREFIX))
    parser.add_argument("--database-url", default=Settings.from_env().database_url)
    args = parser.parse_args(argv)
    if not args.bucket:
        parser.error("a bucket is required: pass --bucket or set DISPATCH_S3_BUCKET")

    engine = make_engine(args.database_url)
    store = boto3.client("s3", endpoint_url=args.endpoint_url)
    try:
        result = export_day(engine, store, args.bucket, args.day, args.prefix)
    except ExportError as exc:
        print(f"export failed: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    print(json.dumps(asdict(result), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
