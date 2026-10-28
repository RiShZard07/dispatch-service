"""Transactional outbox: order events are queued in the database and delivered as webhooks.

The dual-write problem: a request that updates the order and then calls a
notification endpoint has two systems and no shared transaction. If the
process dies between the two, the order changed and nobody was told; if the
call runs inside the transaction, the database lock is held for a network
round trip and a slow receiver stalls every writer. The outbox fixes both.
The service inserts an outbox row in the same transaction as the order event,
so the two commit or roll back together, and a background worker delivers
rows afterwards, with retries, at its own pace.

What this buys is at-least-once delivery, not exactly-once. The worker can
crash after the receiver accepted a POST but before delivered_at was written,
in which case the row is tried again. Two workers on one database (several
processes) can also overlap. Receivers must therefore treat the delivery id
as an idempotency key and ignore ids they have already processed.

Delivery schedule: a row is tried as soon as the worker notices it, then
after backoff_seconds, twice that, four times, eight times (attempts 2 to 5).
After MAX_ATTEMPTS failed tries it is left in the table as exhausted, with
last_error saying why, for a person to look at.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Connection, Engine, Row, func, insert, select, update

from app import __version__
from app.db import read_connection, write_transaction
from app.metrics import AppMetrics
from app.models import outbox
from app.request_logging import get_logger
from app.services.common import next_id, utcnow

log = get_logger("outbox")

MAX_ATTEMPTS = 5
BATCH_SIZE = 50
ERROR_TEXT_LIMIT = 200


def enqueue(conn: Connection, event_type: str, payload: dict[str, Any], now: datetime) -> int:
    """Insert one outbox row inside the caller's write transaction and return its id."""
    row_id = next_id(conn, outbox.c.id)
    conn.execute(
        insert(outbox).values(
            id=row_id,
            event_type=event_type,
            payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            attempts=0,
            next_attempt_at=now,
            delivered_at=None,
            last_error=None,
            created_at=now,
        )
    )
    return row_id


def backoff_seconds(attempts: int, base: float) -> float:
    """Delay before the next try after `attempts` failed ones: base, 2*base, 4*base, ..."""
    return base * 2 ** (attempts - 1)


def due_rows(engine: Engine, now: datetime, limit: int = BATCH_SIZE) -> list[Row[Any]]:
    """Undelivered rows whose next attempt is due and whose attempts are not exhausted, oldest first."""
    stmt = (
        select(outbox)
        .where(outbox.c.delivered_at.is_(None), outbox.c.attempts < MAX_ATTEMPTS, outbox.c.next_attempt_at <= now)
        .order_by(outbox.c.id)
        .limit(limit)
    )
    with read_connection(engine) as conn:
        return conn.execute(stmt).all()


def claim(engine: Engine, row: Row[Any], now: datetime, lease: timedelta) -> bool:
    """Count the attempt before it starts and push next_attempt_at out by a lease.

    The compare-and-set on attempts means that of two workers looking at the
    same row, exactly one wins the try; the other sees zero rows updated. A
    worker that dies mid-POST leaves attempts incremented and the row due again
    once the lease expires, so a crash costs a retry, never a lost event.
    """
    with write_transaction(engine) as conn:
        result = conn.execute(
            update(outbox)
            .where(outbox.c.id == row.id, outbox.c.attempts == row.attempts, outbox.c.delivered_at.is_(None))
            .values(attempts=row.attempts + 1, next_attempt_at=now + lease)
        )
    return result.rowcount == 1


def mark_delivered(engine: Engine, row_id: int, now: datetime) -> None:
    with write_transaction(engine) as conn:
        conn.execute(update(outbox).where(outbox.c.id == row_id).values(delivered_at=now, last_error=None))


def mark_failed(engine: Engine, row_id: int, attempts: int, error: str, now: datetime, base: float) -> None:
    """Record the failure and schedule the next try; the due query stops once attempts reach the cap."""
    retry_at = now + timedelta(seconds=backoff_seconds(attempts, base))
    with write_transaction(engine) as conn:
        conn.execute(
            update(outbox)
            .where(outbox.c.id == row_id)
            .values(next_attempt_at=retry_at, last_error=error[:ERROR_TEXT_LIMIT])
        )


def count_by_status(engine: Engine) -> dict[str, int]:
    """Feeds the outbox_rows gauge: pending (still to deliver), delivered, exhausted (gave up)."""
    delivered = outbox.c.delivered_at.is_not(None)
    exhausted = outbox.c.delivered_at.is_(None) & (outbox.c.attempts >= MAX_ATTEMPTS)
    pending = outbox.c.delivered_at.is_(None) & (outbox.c.attempts < MAX_ATTEMPTS)
    with read_connection(engine) as conn:
        return {
            name: conn.execute(select(func.count()).select_from(outbox).where(condition)).scalar_one()
            for name, condition in (("pending", pending), ("delivered", delivered), ("exhausted", exhausted))
        }


def webhook_request(row: Row[Any], attempt: int) -> tuple[dict[str, str], dict[str, Any]]:
    """Headers and JSON body for one delivery. The id is the receiver's idempotency key."""
    headers = {
        "User-Agent": f"dispatch-service/{__version__}",
        "X-Dispatch-Delivery-ID": str(row.id),
        "X-Dispatch-Event-Type": row.event_type,
        "X-Dispatch-Attempt": str(attempt),
    }
    body = {
        "id": row.id,
        "type": row.event_type,
        "attempt": attempt,
        "created_at": row.created_at.isoformat(timespec="milliseconds") + "Z",
        "data": json.loads(row.payload),
        "synthetic": True,
    }
    return headers, body


@dataclass(frozen=True)
class DeliveryOutcome:
    """What one try produced."""

    row_id: int
    attempt: int
    delivered: bool
    error: str | None = None


class OutboxWorker:
    """Background thread that POSTs due outbox rows to the webhook URL.

    Started by the application lifespan and stopped by it; start/stop may be
    called more than once. The thread runs drain() whenever it is woken or its
    poll interval elapses; drain() and run_once() can also be called directly,
    without the thread, which is how the backoff tests drive it with a fake clock.
    """

    def __init__(
        self,
        engine: Engine,
        metrics: AppMetrics,
        url: str,
        timeout_seconds: float = 5.0,
        backoff_base_seconds: float = 1.0,
        poll_seconds: float = 1.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine, self.metrics, self.url = engine, metrics, url
        self.timeout_seconds = timeout_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client: httpx.Client | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._client = httpx.Client(timeout=self.timeout_seconds)
        self._thread = threading.Thread(target=self._run, name="outbox-worker", daemon=True)
        self._thread.start()
        log.info("outbox worker started", extra={"fields": {"webhook_url": self.url}})

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the thread to finish the row it is on and wait for it.

        A POST in flight is given up to `timeout` to finish (the HTTP timeout is far
        shorter by default). If the thread is somehow still alive after that, the client is
        closed under it; the resulting error is caught by the run loop and the row is
        simply retried by the next worker, which is what at-least-once means.
        """
        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout)
        self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None
        log.info("outbox worker stopped")

    def wake(self, _event: object = None) -> None:
        """Cut the poll sleep short; registered as an OrderFeed listener so commits are delivered promptly."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain()
            except Exception as exc:  # keep the thread alive; the rows stay due and will be retried
                log.error("outbox pass failed", exc_info=exc)
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def drain(self) -> list[DeliveryOutcome]:
        """Run passes until one finds fewer than a full batch of due rows, so a backlog clears without waiting."""
        outcomes: list[DeliveryOutcome] = []
        while not self._stop.is_set():
            tried, fetched = self._pass()
            outcomes.extend(tried)
            if fetched < BATCH_SIZE:
                break
        return outcomes

    def run_once(self) -> list[DeliveryOutcome]:
        """One pass over up to BATCH_SIZE due rows. Returns one outcome per row that was actually tried."""
        return self._pass()[0]

    def _pass(self) -> tuple[list[DeliveryOutcome], int]:
        outcomes: list[DeliveryOutcome] = []
        now = self.clock()
        lease = timedelta(seconds=self.timeout_seconds + self.backoff_base_seconds)
        rows = due_rows(self.engine, now)
        for row in rows:
            if self._stop.is_set():
                break
            if not claim(self.engine, row, now, lease):
                continue  # another worker took this try
            outcomes.append(self._deliver(row, row.attempts + 1))
        return outcomes, len(rows)

    def _deliver(self, row: Row[Any], attempt: int) -> DeliveryOutcome:
        headers, body = webhook_request(row, attempt)
        client = self._client or httpx.Client(timeout=self.timeout_seconds)
        try:
            response = client.post(self.url, json=body, headers=headers)
            error = None if response.is_success else f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if client is not self._client:
                client.close()
        fields = {"delivery_id": row.id, "event_type": row.event_type, "attempt": attempt}
        if error is None:
            mark_delivered(self.engine, row.id, self.clock())
            self.metrics.webhook_deliveries.inc()
            log.info("webhook delivered", extra={"fields": fields})
            return DeliveryOutcome(row.id, attempt, True)
        mark_failed(self.engine, row.id, attempt, error, self.clock(), self.backoff_base_seconds)
        self.metrics.webhook_failures.inc()
        gave_up = attempt >= MAX_ATTEMPTS
        log.warning("webhook failed", extra={"fields": {**fields, "error": error, "exhausted": gave_up}})
        return DeliveryOutcome(row.id, attempt, False, error)
