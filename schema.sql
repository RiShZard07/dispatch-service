-- dispatch-service schema. All rows are synthetic test data.
-- Portable DDL: it runs unchanged on SQLite (the default) and PostgreSQL.
-- app/models.py mirrors these tables and a test fails if the two drift apart.
-- Text primary keys say NOT NULL explicitly because SQLite, unlike PostgreSQL,
-- lets a non-integer primary key hold NULL unless told otherwise.

CREATE TABLE IF NOT EXISTS merchants (
    id   TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    lat  DOUBLE PRECISION NOT NULL,
    lng  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id         TEXT NOT NULL PRIMARY KEY,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS couriers (
    id        TEXT NOT NULL PRIMARY KEY,
    name      TEXT NOT NULL,
    lat       DOUBLE PRECISION NOT NULL,
    lng       DOUBLE PRECISION NOT NULL,
    available BOOLEAN NOT NULL DEFAULT TRUE,
    version   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_couriers_available ON couriers (available);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT NOT NULL PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash    TEXT NOT NULL,
    merchant_id     TEXT NOT NULL REFERENCES merchants (id),
    customer_id     TEXT NOT NULL REFERENCES customers (id),
    items           TEXT NOT NULL,
    subtotal_cents  INTEGER NOT NULL,
    state           TEXT NOT NULL,
    courier_id      TEXT REFERENCES couriers (id),
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL,
    CHECK (state IN ('CREATED', 'ACCEPTED', 'ASSIGNED', 'PICKED_UP', 'DELIVERED', 'CANCELED'))
);

-- items holds a JSON document as text so the column works on both engines.
-- On PostgreSQL alone, JSONB would be the natural choice.

CREATE INDEX IF NOT EXISTS idx_orders_state_created ON orders (state, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_merchant_created ON orders (merchant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_courier_created ON orders (courier_id, created_at);

-- Append-only audit trail. seq is the order version that the transition produced,
-- so (order_id, seq) is unique and the rows sort in the order they happened.
-- id is a single sequence across every order, assigned by the service inside the
-- write transaction (MAX(id) + 1 under the write lock), so the whole trail has one
-- total order. The real-time feed uses it as the SSE event id and resume cursor.
-- On PostgreSQL an identity column would take over that job. Comments in this file
-- must not contain semicolons: app/db.py splits statements on them.
CREATE TABLE IF NOT EXISTS order_events (
    id         INTEGER NOT NULL UNIQUE,
    order_id   TEXT NOT NULL REFERENCES orders (id),
    seq        INTEGER NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (order_id, seq)
);

-- Transactional outbox. One row per order event, inserted in the same transaction
-- as the event, delivered later by the background worker as a webhook POST.
-- attempts counts tries that were started, not tries that finished, so a crash in
-- the middle of a POST still counts. next_attempt_at holds the backoff schedule
-- and acts as a short lease while a try is in flight. delivered_at is set once a
-- 2xx came back. A row with attempts at the cap and no delivered_at is exhausted.
CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER NOT NULL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMP NOT NULL,
    delivered_at    TIMESTAMP,
    last_error      TEXT,
    created_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox (delivered_at, next_attempt_at);
