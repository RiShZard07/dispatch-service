"""Engine construction, SQLite locking setup and the two transaction helpers.

SQLite allows one writer at a time. A transaction opened with a plain BEGIN
(DEFERRED) takes its write lock lazily, on the first write. Two assignment
requests can therefore both read "courier X is available" inside their own
transactions and then race to upgrade to the write lock; SQLite resolves that
by failing the loser with "database is locked" rather than waiting, because
waiting could deadlock. BEGIN IMMEDIATE takes the write lock up front, so
writers queue in order (the busy timeout makes them wait instead of fail) and
each one sees the committed result of the previous one. Readers stay on
DEFERRED transactions and, with WAL journaling, never block writers.

Waiting on SQLite's busy handler is correct but slow under contention: a
waiting writer sleeps and retries with growing delays, so the lock can sit idle
while everyone sleeps and tail latency balloons. Within one process, writers
therefore queue on a plain threading lock first, which hands the turn over the
moment the previous commit finishes. BEGIN IMMEDIATE still runs underneath and
remains the correctness guarantee across processes (see the two-instance test
in tests/test_concurrency.py).

PostgreSQL has no BEGIN IMMEDIATE and queues lock waiters itself. There the
service layer relies on SELECT ... FOR UPDATE (which SQLAlchemy drops when
compiling for SQLite) and on the version columns. That path is not exercised
by the test suite.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, inspect

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
SQLITE_BUSY_TIMEOUT_SECONDS = 30

# One in-process write queue per SQLite engine. Weak keys so a disposed engine is not kept alive.
_write_locks: weakref.WeakKeyDictionary[Engine, threading.Lock] = weakref.WeakKeyDictionary()


def make_engine(database_url: str) -> Engine:
    """Create an engine. SQLite engines get WAL, foreign keys, explicit BEGIN control and a write queue."""
    if not database_url.startswith("sqlite"):
        return create_engine(database_url, pool_pre_ping=True)

    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS},
    )

    @event.listens_for(engine, "connect")
    def configure_connection(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # stop pysqlite from emitting its own BEGIN
        dbapi_connection.execute("PRAGMA journal_mode=WAL")
        # In WAL mode NORMAL keeps the database consistent after a crash; only the most recent
        # commits can be lost on power failure. FULL would fsync every commit at roughly 5x the cost.
        dbapi_connection.execute("PRAGMA synchronous=NORMAL")
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    @event.listens_for(engine, "begin")
    def begin_transaction(conn: Connection) -> None:
        mode = "IMMEDIATE" if conn.get_execution_options().get("write") else "DEFERRED"
        conn.exec_driver_sql(f"BEGIN {mode}")

    _write_locks[engine] = threading.Lock()
    return engine


@contextmanager
def write_transaction(engine: Engine) -> Iterator[Connection]:
    """A transaction that may write: BEGIN IMMEDIATE on SQLite, a normal BEGIN elsewhere.

    The in-process lock is taken before a pooled connection is checked out, so queued
    writers do not hold connections that readers could use. Commits when the block exits
    normally and rolls back on any exception.
    """
    with _write_locks.get(engine) or nullcontext():
        with engine.connect().execution_options(write=True) as conn, conn.begin():
            yield conn


@contextmanager
def read_connection(engine: Engine) -> Iterator[Connection]:
    """A read-only connection. Its implicit DEFERRED transaction is discarded on exit."""
    with engine.connect() as conn:
        yield conn


def init_schema(engine: Engine) -> None:
    """Apply schema.sql. Every statement uses IF NOT EXISTS, so this is safe to repeat.

    There is no migration tool. CREATE TABLE IF NOT EXISTS leaves an existing table
    untouched, so a database file created before order_events gained its id column
    would fail later with a confusing "no such column". Refuse it up front instead.
    """
    statements = [part.strip() for part in SCHEMA_PATH.read_text().split(";") if part.strip()]
    with write_transaction(engine) as conn:
        for statement in statements:
            conn.exec_driver_sql(statement)
    columns = {column["name"] for column in inspect(engine).get_columns("order_events")}
    if "id" not in columns:
        raise RuntimeError(
            "the database predates the order_events.id column and there is no migration tool; "
            "delete the database file (or point DISPATCH_DATABASE_URL at a new one) and start again"
        )
