# dispatch-service

A small order-dispatch backend for a synthetic local-delivery marketplace. Merchants create
orders, a dispatcher assigns the nearest available courier, couriers pick up and deliver, and
every step is recorded. The point of the project is service design fundamentals, not the
domain: a REST API with idempotent order creation, a SQL-backed order state machine with
transactional courier assignment that stays correct under concurrent requests, optimistic
concurrency with version numbers, a real-time order feed over server-sent events, a
transactional outbox that delivers every order event as a webhook with retries, CORS for a
browser client, Prometheus-style metrics from a hand-written registry, structured JSON request
logging with request ids, a load script that reports p50/p95/p99 latency, a Docker image, CI,
a Render blueprint, and a small S3 export.

All data is synthetic. There are no real merchants, couriers, customers, orders or locations
anywhere in this repository, its tests or its scripts.

## Status

This is an AI-assisted educational prototype built with Claude Code. It runs as a free-tier
demo on Render at https://dispatch-service-d7gk.onrender.com (synthetic data only, no users;
the instance sleeps after 15 idle minutes, so the first request after a pause takes up to a
minute). The SQLite path is what the test suite and the load numbers cover.
The PostgreSQL path exists in the DDL and the locking code but has not been run. The S3
export has only been exercised against moto's in-memory mock, never a live bucket. The Docker
image is built by CI; it was not built on the machine where VERIFIED_RESULTS.md was written.
The Render blueprint (`render.yaml`) was applied on 2026-10-28; the first build and health
check passed without changes to the file. The webhook worker has only ever posted to the
receiver script in this repository, on the same machine. Treat every number in this repository
as "measured once on one laptop-class machine".

## What it demonstrates

- Idempotent `POST /orders` keyed by an `Idempotency-Key` header: a retry with the same body
  returns the original order with 200; the same key with a different body is a 409.
- An explicit order state machine (`app/state.py`) that is the single source of truth for
  which transitions are legal. Illegal ones are 409s with a machine-readable code.
- Courier assignment that picks the nearest available courier by haversine distance and books
  it in the same transaction as the order update, with three layers of protection against
  double booking (see "How assignment stays correct under concurrency").
- An append-only audit trail (`order_events`) written in the same transaction as every change,
  numbered by one global sequence so the whole trail has a single order.
- A real-time feed, `GET /orders/stream`, that pushes every audit event as a server-sent
  event. A client that reconnects with `Last-Event-ID` gets exactly what it missed, because the
  stream combines an in-process publish/subscribe with a catch-up read of the table.
- A transactional outbox: every order transition inserts an `outbox` row in the same
  transaction, and a background worker delivers each row as a webhook POST with exponential
  backoff, at most five attempts, and at-least-once semantics (see "Outbox and webhooks").
- Optimistic concurrency: every order and courier carries a `version`; clients may send
  `If-Match: <version>` and get a 409 if the row moved on since they read it.
- One consistent JSON error envelope, `{"error": {"code": ..., "message": ...}}`, for every
  failure including framework-level 404s, 405s, 413s and unexpected 500s.
- CORS for a browser client, from a configured list of origins, including preflights.
- A Prometheus text exposition produced by a small in-process registry written for this
  project (no `prometheus_client`), with a request counter, a latency histogram, gauges of
  orders by state and outbox rows by status, and counters for assignments and webhooks.
- Structured JSON logs, one line per request, carrying an `X-Request-ID` that is echoed back
  to the client and reaches service-layer log lines through a context variable.
- 125 tests, including two concurrency scenarios run with real threads against one app
  instance and a file-backed SQLite database, a third with two independent app instances
  sharing one database file, stream tests read over HTTP from a uvicorn server started inside
  the test, and webhook tests delivered to a receiver started inside the test.

## Running it

Python 3.12 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# API server (the JSON request log replaces uvicorn's access log)
uvicorn app.main:app --port 8000 --no-access-log

# tests
python3 -m pytest -q

# synthetic merchants, customers and couriers against a running server
python3 scripts/seed.py --base-url http://127.0.0.1:8000 --merchants 5 --customers 10 --couriers 20

# load: N orders through the full lifecycle, with a bounded number in flight
python3 scripts/load.py --base-url http://127.0.0.1:8000 --orders 200 --concurrency 10

# watch the live feed from a second terminal (Ctrl-C to stop)
curl -N http://127.0.0.1:8000/orders/stream

# receive webhooks: start the receiver, then start the server pointed at it
python3 scripts/webhook_receiver.py --port 9000
DISPATCH_WEBHOOK_URL=http://127.0.0.1:9000/webhook uvicorn app.main:app --port 8000 --no-access-log
```

The load script prints throughput and p50/p95/p99 latency per step. Those are local,
single-process, same-machine numbers for a prototype in front of SQLite. They are not a
capacity claim and should not be quoted as one. The interactive API docs are at `/docs`.

The schema changed when the feed and the outbox were added (`order_events.id`, the `outbox`
table). There is no migration tool: a `dispatch.sqlite` file created by an earlier version is
refused at startup with a message saying so. Delete it, or point `DISPATCH_DATABASE_URL` at a
new file.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISPATCH_DATABASE_URL` | `sqlite:///./dispatch.sqlite` | SQLAlchemy URL. A file path is required for the concurrency guarantees; `:memory:` gives every thread its own database. |
| `DISPATCH_MAX_BODY_BYTES` | `65536` | Request bodies above this size are rejected with 413. |
| `DISPATCH_LOG_LEVEL` | `INFO` | Level for the `dispatch.*` loggers. |
| `DISPATCH_CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API, for example `http://localhost:5173,https://dashboard.example`. Empty means no CORS headers at all. |
| `DISPATCH_WEBHOOK_URL` | empty | Where the outbox worker POSTs order events. Empty means the worker does not start and outbox rows stay pending. |
| `DISPATCH_WEBHOOK_TIMEOUT_SECONDS` | `5` | Timeout for one webhook POST. |
| `DISPATCH_WEBHOOK_BACKOFF_SECONDS` | `1` | Delay before the first retry; each later retry doubles it (1, 2, 4, 8 s with the default). |
| `DISPATCH_OUTBOX_POLL_SECONDS` | `1` | How long the worker sleeps between looks at the outbox when no commit has woken it. |
| `DISPATCH_STREAM_HEARTBEAT_SECONDS` | `15` | Seconds of silence on `/orders/stream` before a heartbeat comment is sent. |
| `DISPATCH_S3_BUCKET`, `DISPATCH_S3_ENDPOINT_URL`, `DISPATCH_S3_PREFIX` | none, none, `orders` | Used only by `scripts/export_to_s3.py`. |

### Docker

```bash
docker build -t dispatch-service .
docker run --rm -p 8000:8000 dispatch-service
# or
docker compose up --build
```

The image is `python:3.12-slim`, runs as an unprivileged user (`dispatch`, uid 10001), and
keeps its SQLite file in `/srv/data`. Mount a volume there if you want the data to outlive the
container. The server listens on the port named by the `PORT` environment variable, which
hosting platforms set for the container; it defaults to 8000, which is what the compose file
and the `docker run` line above expect. The image installs everything in `requirements.txt`,
including the test-only `moto` package, which is a simplification a production image would
not make.

### PostgreSQL (optional, untested)

The DDL in `schema.sql` is written to run unchanged on PostgreSQL, and the service layer uses
`SELECT ... FOR UPDATE` where SQLite uses `BEGIN IMMEDIATE`. To try it, install a driver
(`pip install "psycopg[binary]"`), add a `postgres` service to `docker-compose.yml`, and set
`DISPATCH_DATABASE_URL=postgresql+psycopg://dispatch:dispatch@postgres:5432/dispatch`. Nothing
in this repository has been run against PostgreSQL, so expect to debug.

## API

Request bodies are validated with pydantic; unknown fields are rejected. Ids are opaque
strings generated by the service (`ord_...`, `cur_...`, `mer_...`, `cus_...`).

| Method and path | Purpose | Notes |
| --- | --- | --- |
| `POST /merchants` | Create a synthetic merchant with a location | 201 |
| `GET /merchants/{id}` | Read a merchant | 404 if unknown |
| `POST /customers` | Create a synthetic customer id (no personal data) | 201 |
| `GET /customers/{id}` | Read a customer | 404 if unknown |
| `POST /orders` | Create an order; `Idempotency-Key` header required | 201 on first create, 200 on replay, 409 on key reuse with a different body, 422 for unknown merchant or customer |
| `GET /orders` | List orders, oldest first | Filters `state`, `merchant_id`, `courier_id` (a courier app's "my orders"); pagination `limit` (1..100, default 20) and `offset`; the response carries `next_offset` or `null` |
| `GET /orders/stream` | Real-time feed of audit events as server-sent events | `text/event-stream`; resumes after `Last-Event-ID` header or `?after=<event id>`; see "Real-time feed" |
| `GET /orders/{id}` | Read an order | 404 if unknown |
| `GET /orders/{id}/events` | The order's audit trail in sequence order | Each event carries its global `id`, the stream cursor |
| `POST /orders/{id}/accept` | CREATED to ACCEPTED | 409 `invalid_transition` otherwise |
| `POST /orders/{id}/assign` | ACCEPTED to ASSIGNED, booking the nearest available courier | 409 `no_courier_available` when nobody is free |
| `POST /orders/{id}/pickup` | ASSIGNED to PICKED_UP | |
| `POST /orders/{id}/deliver` | PICKED_UP to DELIVERED; frees the courier | |
| `POST /orders/{id}/cancel` | CREATED or ACCEPTED to CANCELED | 409 after assignment |
| `POST /couriers` | Register a courier | 201 |
| `GET /couriers` | List couriers | Filter `available=true|false`; same pagination as orders |
| `GET /couriers/{id}` | Read a courier | |
| `POST /couriers/{id}/location` | Update position | bumps `version` |
| `POST /couriers/{id}/availability` | Go on or off shift | bumps `version` |
| `GET /health` | Liveness plus a trivial database query | 503 with the error envelope if the database does not answer |
| `GET /metrics` | Prometheus text exposition | `text/plain; version=0.0.4` |

Headers:

- `Idempotency-Key` (required on `POST /orders`): any string up to 128 characters chosen by
  the client. The key is global in this prototype; a real system would scope it per client.
- `If-Match` (optional on every transition and courier update): the version the client last
  saw, optionally quoted. A stale value gets 409 `version_conflict` and nothing changes.
- `X-Actor` (optional on order creation and transitions): free text recorded in the audit
  event, for example `merchant:tablet`. Defaults to `api`.
- `X-Request-ID` (optional on anything): echoed back on the response and written into the
  request log line. Generated when absent or malformed.
- `Last-Event-ID` (optional on `GET /orders/stream`): the last event id the client received.
  Browsers send it automatically when an `EventSource` reconnects. It takes precedence over
  `?after=`, which exists for clients that cannot set headers. A non-integer value is a 422.

Error envelope and codes:

```json
{"error": {"code": "invalid_transition", "message": "cannot move an order from CREATED to PICKED_UP"}}
```

| Status | Codes |
| --- | --- |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `invalid_transition`, `idempotency_conflict`, `version_conflict`, `no_courier_available`, `courier_conflict` |
| 413 | `payload_too_large` |
| 422 | `validation_error`, `unknown_merchant`, `unknown_customer` |
| 500 | `internal_error` |
| 503 | `service_unavailable` |

Unknown merchant and customer ids in a request body are 422 rather than 404 because the URL
resource (the orders collection) exists; the body is well formed but refers to something that
does not. 404 is reserved for ids in the path.

## State machine

```
            accept            assign             pickup            deliver
 CREATED ----------> ACCEPTED ----------> ASSIGNED ----------> PICKED_UP ----------> DELIVERED
    |                   |
    | cancel            | cancel
    v                   v
 CANCELED            CANCELED

 DELIVERED and CANCELED are terminal. Every other move is rejected with 409.
```

The table in `app/state.py` is the only place that encodes this. Service code calls
`assert_transition(current, target)` inside the write transaction, after locking the row, so
the check is made against the committed state and not against something the client read
earlier. Every accepted transition appends one `order_events` row (`id`, `seq`, `from_state`,
`to_state`, `actor`, `created_at`) and one `outbox` row in the same transaction; `seq` equals
the order version the transition produced and `id` is the next value of one sequence shared
by every order. A failed transition appends nothing to either table.

## Real-time feed

`GET /orders/stream` keeps the connection open and writes one server-sent event per
`order_events` row, in id order:

```
retry: 3000

id: 42
event: order
data: {"id":42,"order_id":"ord_fd2700f1bd7843e3","seq":3,"from_state":"ACCEPTED","to_state":"ASSIGNED","actor":"dispatcher","created_at":"2026-10-28T15:42:08.754Z"}

: heartbeat
```

The `data` line is the same JSON as one element of `GET /orders/{id}/events`. A line starting
with `:` is a comment, which browsers ignore; one is sent after
`DISPATCH_STREAM_HEARTBEAT_SECONDS` of silence so that a client, a proxy and the server can all
tell an idle connection from a dead one. The `retry` line asks a browser `EventSource` to wait
three seconds before reconnecting.

Resuming. Every event carries `id`, and a browser sends the last one it saw back as
`Last-Event-ID` when it reconnects; a client without headers can pass `?after=<id>`. The
server then sends every event with a greater id, oldest first, before switching to live
delivery. So the recipe for a client is: remember the highest id you have processed, and
reconnect with it. From a browser:

```js
const feed = new EventSource("https://dispatch.example/orders/stream?after=0");
feed.addEventListener("order", (event) => render(JSON.parse(event.data)));
// on a dropped connection the browser reconnects by itself and sends Last-Event-ID
```

How nothing is missed. Each committed event is published to an in-process feed
(`app/feed.py`) after the transaction commits, and every open stream holds a queue on that
feed. The queue alone would not be enough: publishing happens after the write lock is released,
so two events can arrive out of order; a slow consumer's queue can fill and drop one; and a
second server process writing to the same database file never touches this process's feed at
all. So the stream treats the queue as a doorbell and the table as the truth. It subscribes
before it reads the table for catch-up (anything committed in between is in the result or in
the queue, and ids dedupe the overlap), it reads the table again whenever an id arrives that
is not exactly the next one, and it reads it once more at every heartbeat. The out-of-order
case and the second-process case both have a test (`tests/test_stream.py`).

The stream runs on one server instance; the heartbeat catch-up is what keeps several
instances consistent, at the cost of the heartbeat interval in latency for events written
elsewhere. A production system with many subscribers would move the feed to a broker and keep
the same resume contract.

## Outbox and webhooks

Every order transition inserts a row into `outbox` inside the same transaction as the order
update and the audit event. When `DISPATCH_WEBHOOK_URL` is set, a worker thread started with
the application delivers each row as `POST <url>` with a JSON body and marks it delivered:

```json
{
  "id": 42,
  "type": "order.assigned",
  "attempt": 1,
  "created_at": "2026-10-28T15:42:08.754Z",
  "data": {
    "event_id": 42, "order_id": "ord_fd2700f1bd7843e3", "seq": 3,
    "from_state": "ACCEPTED", "to_state": "ASSIGNED", "actor": "dispatcher",
    "occurred_at": "2026-10-28T15:42:08.754Z",
    "order": {"id": "ord_fd2700f1bd7843e3", "state": "ASSIGNED", "courier_id": "cur_...",
              "version": 3, "merchant_id": "mer_...", "customer_id": "cus_..."}
  },
  "synthetic": true
}
```

The same values travel as headers (`X-Dispatch-Delivery-ID`, `X-Dispatch-Event-Type`,
`X-Dispatch-Attempt`) for receivers that route before parsing. Any 2xx response counts as
delivered. Anything else, a timeout or a connection error is a failed attempt: the row's
`last_error` records why and `next_attempt_at` moves out by a delay that starts at
`DISPATCH_WEBHOOK_BACKOFF_SECONDS` and doubles with every failure (1, 2, 4 and 8 s by default
before the second, third, fourth and fifth attempts). After five attempts the row is left in
the table as exhausted and never tried again; `GET /metrics`
reports it under `outbox_rows{status="exhausted"}` so a person can notice. No URL means no
worker: rows still accumulate, which is deliberate, because the table is the record of what
has to be delivered, not a cache.

Why an outbox. Without it the service would have to call the receiver either inside the
database transaction (holding the write lock across a network round trip, so one slow
receiver stalls every writer) or after it (and a crash between the commit and the call loses
the notification with no trace). That is the dual-write problem: two systems and no
transaction spanning them. The outbox turns the notification into a row that commits or rolls
back with the change it describes; a failed transition writes no row, which
`tests/test_outbox.py` checks. Delivery then becomes a separate job with its own retries, and
the service's latency no longer depends on the receiver's.

Why delivery is at-least-once, never exactly-once. The worker counts an attempt before it
starts (a compare-and-set on `attempts`, which also stops two workers from trying the same row
at once), POSTs, and then writes `delivered_at`. If the process dies after the receiver
accepted the POST and before `delivered_at` is written, the row is still undelivered as far
as the table knows, and it is tried again. The receiver sees the same delivery twice. There is
no way to make the POST and the `delivered_at` update one atomic step, because they happen in
two different systems, so the duplicate is a property of the design, not a bug to fix later.
The only honest options are at-most-once (mark first, then send, and accept that a crash loses
the event) or at-least-once (send first, then mark, and accept duplicates). Lost notifications
are worse than repeated ones, so this project picks at-least-once.

What the receiver must do. Treat `id` as an idempotency key: keep the ids already processed
(in its own database, ideally in the same transaction as whatever the event caused), and
answer 2xx without doing anything for an id it has seen. It must also expect events to arrive
out of order across retries (event 41 may be retried after event 42 was delivered), so a
consumer that cares about order should sort by `data.event_id` or apply only events whose
`seq` is greater than the version it already holds. `scripts/webhook_receiver.py` shows the
minimum: it prints every delivery, keeps a set of ids, and flags a repeat instead of acting
on it; `--fail-first N` makes it answer the first N deliveries with 500 so the retries can be
watched.

To watch the mechanism work, in three terminals:

```bash
python3 scripts/webhook_receiver.py --port 9000 --fail-first 2
DISPATCH_WEBHOOK_URL=http://127.0.0.1:9000/webhook DISPATCH_WEBHOOK_BACKOFF_SECONDS=1 \
    uvicorn app.main:app --port 8000 --no-access-log
python3 scripts/seed.py && python3 scripts/load.py --orders 5 --concurrency 1
```

The first terminal prints the first two deliveries answered with 500, their retries about a
second later (accepted this time, `attempt=2`), and every later event exactly once. To watch
one row walk the whole schedule and give up, restart the receiver with `--fail-first 5` and
create a single order with `curl`: the five attempts land at roughly 0, 1, 3, 7 and 15 seconds
(each retry can slip by up to one `DISPATCH_OUTBOX_POLL_SECONDS`, because only a commit wakes
the worker early), and `curl -s localhost:8000/metrics | grep outbox_rows` then shows one
exhausted row.

## How assignment stays correct under concurrency

`POST /orders/{id}/assign` must never give two orders the same courier, and must not lose
assignments either: with 100 concurrent assigns and 10 free couriers, exactly 10 orders end
up ASSIGNED and 90 get a clean 409 `no_courier_available`. Three layers make that true, each
of which would catch a failure of the one above it.

1. An in-process write queue. On SQLite every write transaction first takes a `threading.Lock`
   that belongs to the engine. This is not the correctness guarantee; it is there because
   SQLite's busy handler makes a waiting writer sleep and retry with growing delays, which under
   ten concurrent writers produced p99 latencies of several hundred milliseconds. Queueing on
   a lock hands the turn over the moment the previous commit finishes. On this machine the
   load script at concurrency 10 went from a p99 of roughly 650 ms per request to roughly
   65 ms after the queue was added; VERIFIED_RESULTS.md has both runs.
2. `BEGIN IMMEDIATE` on SQLite, `SELECT ... FOR UPDATE` on PostgreSQL. A deferred SQLite
   transaction only takes the write lock at its first write, so two assigns could both read
   "courier X is available" and then one of them fails on the lock upgrade. `BEGIN IMMEDIATE`
   takes the write lock up front, before the courier query runs, so the second transaction
   waits and then sees the first one's commit. This works across processes, which the lock in
   layer 1 does not, and `tests/test_concurrency.py` proves it with two independent app
   instances (two engines, two pools, two locks) hammering one database file. On PostgreSQL the
   equivalent is locking the order row and the candidate courier rows with `FOR UPDATE`;
   SQLAlchemy drops that clause when compiling for SQLite, so the same code serves both.
3. A compare-and-set on the courier row. The booking statement is
   `UPDATE couriers SET available = false, version = version + 1 WHERE id = ? AND version = ?
   AND available = true`, and the service checks that exactly one row changed. The order row
   is updated the same way (`WHERE id = ? AND version = ?`). If a locking bug ever let two
   transactions past layers 1 and 2, the second update would match zero rows and the request
   would fail with 409 instead of double booking.

The nearest courier is chosen in Python by haversine distance over every available courier,
with ties broken by id so the choice is deterministic. There is no spatial index; with
thousands of couriers this scan would be the first thing to replace.

Delivery frees the courier in the delivery transaction, so the load script can recycle a
small fleet through hundreds of orders.

## Metrics

`GET /metrics` renders the registry in `app/metrics.py`, which implements counters,
histograms and callback gauges by hand so the mechanics are visible. No `prometheus_client`.

| Metric | Type | Labels | Meaning |
| --- | --- | --- | --- |
| `http_requests_total` | counter | `method`, `route`, `status` | Requests by route template (for example `/orders/{order_id}`), not raw path, so label cardinality stays bounded. Requests that match no route are labelled `unmatched`. |
| `http_request_duration_seconds` | histogram | `method`, `route` | Latency with the default buckets 5 ms to 10 s plus `+Inf`, with `_sum` and `_count`. |
| `orders_by_state` | gauge | `state` | Counted from the database on every scrape, one sample per state, including zeros. |
| `assignments_total` | counter | none | Successful courier bookings. |
| `assignment_conflicts_total` | counter | none | Assign requests that found no free courier or lost a race. |
| `outbox_rows` | gauge | `status` | Counted from the database on every scrape: `pending` (still to deliver), `delivered`, `exhausted` (five failures, needs a person). |
| `webhook_deliveries_total` | counter | none | Outbox rows accepted by the receiver with a 2xx. |
| `webhook_failures_total` | counter | none | Failed delivery attempts; each retry that fails counts again. |

A 413 rejected before routing is counted under route `unmatched`, because the router never
ran, and so is a CORS preflight, which the CORS middleware answers before the router. The
request log line for `GET /orders/stream` is written when the stream closes, so its
`duration_ms` is how long the client stayed connected.

## Logging

Every request produces one JSON line on stderr from the `dispatch.http` logger:

```json
{"ts": "2026-10-28T15:42:08.754+00:00", "level": "INFO", "logger": "dispatch.http", "msg": "request",
 "request_id": "b2308068638e482394560e124d37fbbf", "method": "POST",
 "path": "/orders/ord_fd2700f1bd7843e3/assign", "route": "/orders/{order_id}/assign",
 "status": 200, "duration_ms": 4.93}
```

The request id lives in a `contextvars.ContextVar` for the duration of the request. Starlette
copies the context into the worker thread that runs a synchronous endpoint, so a service-layer
line such as `"msg": "order assigned"` carries the same `request_id` without being passed
one. Unhandled exceptions are logged with their traceback at ERROR and answered with the
standard 500 envelope, still carrying the request id.

## S3 export

`scripts/export_to_s3.py` writes the DELIVERED orders of one UTC day as newline-delimited
JSON to `<prefix>/YYYY-MM-DD.jsonl` and a manifest (`record_count`, `byte_size`, `sha256`) to
`<prefix>/YYYY-MM-DD.manifest.json`. Keys are deterministic and the content is canonically
serialized, so re-running a day overwrites both objects with byte-identical content; there is
no way to produce duplicates by running it twice. The bucket and an optional endpoint URL come
from `DISPATCH_S3_BUCKET` and `DISPATCH_S3_ENDPOINT_URL`, so it also points at any
S3-compatible store. A missing bucket is reported as a clear error rather than a traceback.

```bash
AWS_DEFAULT_REGION=us-east-1 DISPATCH_S3_BUCKET=my-bucket python3 scripts/export_to_s3.py --day 2026-10-28
```

This export has been run only against moto's in-memory S3 mock (`tests/test_export.py`),
never against a real AWS account or any live endpoint. The exported records are synthetic.

## Design decisions and tradeoffs

**SQLite with `BEGIN IMMEDIATE` instead of PostgreSQL.** SQLite makes the project runnable
with nothing but `pip install`, including the concurrency tests, and it forces the locking
question into the open instead of hiding it behind a database that queues lock waiters for
you. The cost is a single writer: throughput plateaus at a few hundred requests per second on
this machine no matter how many clients there are, and tail latency grows linearly with the
number of concurrent writers. The in-process write queue and `PRAGMA synchronous=NORMAL`
(consistent after a crash, only the last commits at risk on power loss, roughly five times
cheaper per commit than `FULL`) are the two tunings applied; both are documented in
`app/db.py`.

**Optimistic versioning on top of locking.** The locks already serialize writers, so why keep
a `version` column? Two reasons. It gives clients `If-Match`, so a dashboard that read an
order at version 3 can refuse to act on it after someone else moved it, without the server
holding anything open between requests. And the compare-and-set `UPDATE ... WHERE version = ?`
is a cheap, database-enforced assertion that the locking did its job, which is exactly the
kind of invariant worth paying one predicate for.

**Server-sent events, not websockets or polling, for the feed.** The feed is one-directional:
the server has things to say and the client only needs to listen. SSE is plain HTTP, works
through ordinary proxies and load balancers, gives browsers automatic reconnection with
`Last-Event-ID` for free, and needs no extra library on either side. Websockets would add a
second protocol, an upgrade handshake and hand-written reconnection logic to get a channel
that can also talk back, which nothing here needs. Polling `GET /orders/{id}/events` would
work with zero server-side machinery but either wastes requests when nothing happens or adds
seconds of lag, and it scales with the number of clients times the polling rate rather than
with the number of events. The cost of SSE is one open connection per client, which on this
single-process server means one asyncio task and one queue each; it is not the right shape
for tens of thousands of subscribers without a broker in between.

**An outbox with a webhook worker, rather than calling the receiver from the request.** See
"Outbox and webhooks" for the full argument. The short version is that a notification sent
from inside the request is either inside the transaction (holding the write lock across the
network) or outside it (lost on a crash between commit and send), and the outbox is the
smallest thing that fixes both. The price is at-least-once delivery and a worker to run.
The worker is a thread in the API process so the project stays one process to start; a
separate process reading the same table would be the next step, and the compare-and-set
claim on `attempts` is already there to keep two workers from colliding.

**Application-assigned ids for `order_events` and `outbox`.** Both tables take
`MAX(id) + 1` inside the write transaction instead of an autoincrement column. On SQLite the
transaction already holds the database write lock, so the read-and-add is race-free, and
it keeps `schema.sql` free of engine-specific syntax (SQLite's `AUTOINCREMENT` and
PostgreSQL's identity columns differ). On PostgreSQL two transactions could compute the same
value under `READ COMMITTED`, and the `UNIQUE` constraint would fail one of them; the
honest fix there is an identity column, which is noted in the schema.

**CORS sits between the body limit and the observability layer.** With the last middleware
added being the outermost, the order is observability outside, CORS in the middle, body limit
inside. That way a preflight is still logged, counted and given a request id, a 413 from the
body limit still carries `Access-Control-Allow-Origin` so a browser can read it, and the
allowed-origin list is checked before any handler runs. The one gap is an unexpected 500,
which the observability layer writes itself, without CORS headers; a browser then sees a
network error rather than the error envelope.

**No queue, no cache, no migrations tool.** A message queue would decouple assignment from
the request (a client could POST and poll, and a worker could batch assignments or retry when
no courier is free). Redis would be the natural home for courier locations updated every few
seconds, and for rate limits and idempotency keys with a TTL. Alembic would manage schema
changes once there is data worth keeping; here `schema.sql` plus `CREATE ... IF NOT EXISTS`
is enough, and the one incompatible change so far (the event id column) is refused at
startup with a clear message rather than migrated. Each of these is a real component with
real failure modes, and none of them would make the core invariants of this service clearer,
so they are left out and named instead.

**Offset pagination.** `limit`/`offset` is simple and the response says where the next page
starts. Its known weaknesses are that deep pages cost the database a scan of everything
skipped, and that an insert between two page reads shifts items so one can be seen twice or
missed. Keyset pagination on `(created_at, id)` fixes both and would be the first change if
the list were used by anything other than a dashboard.

**Hand-written metrics registry.** About 150 lines replace a dependency and make histogram
buckets, label escaping and the exposition format concrete. It does not do multiprocess
aggregation; with several uvicorn workers each would report its own counters.

**Request hash for idempotency.** The hash covers the canonicalized, validated body, so
two requests that differ only in key order or whitespace replay as the same request. A key
is global rather than per client, which is fine for a prototype and wrong for a multi-tenant
API.

## Limitations

- Single process. Several uvicorn workers would still be correct (layer 2 above holds across
  processes, and the outbox claim is a compare-and-set), but metrics would be per worker, the
  write queue would stop helping, and a stream served by one worker learns about events
  written through another only at its next heartbeat.
- No authentication or authorization. Anyone can accept, assign or cancel anything, and
  anyone can open the feed or read every order through it.
- No idempotency on transitions (a retried `accept` after a network failure returns 409
  rather than 200 because the first attempt already moved the order).
- One webhook URL for every event type, no signature on the webhook body, and no pruning of
  the outbox: delivered and exhausted rows stay forever. A real system would sign deliveries,
  let receivers subscribe to event types, and archive old rows.
- The feed has no backpressure beyond a 256-event queue per subscriber; a client that stops
  reading is simply served from the table at the next heartbeat.
- The webhook worker delivers rows one at a time, in order, and shares the single SQLite
  writer with the API, so it costs the API some throughput while both are busy (about 15
  percent at concurrency 10 in VERIFIED_RESULTS.md, with the receiver on the same host). A
  slow receiver would stretch the backlog linearly, since nothing is delivered in parallel.
- No geo index. Nearest-courier selection scans every available courier.
- No real time: timestamps come from the server clock, and the S3 export's "day" is UTC.
- Load numbers are local single-process measurements, published only so the method is
  reproducible, not as a capacity claim.
- PostgreSQL and real S3 are supported in the code but have not been run. The Render
  deployment is a free instance with no persistent disk: its database is reset whenever it
  restarts or spins down.

## Deploying to Render

`render.yaml` is a Render Blueprint that declares one Docker web service named
`dispatch-service` on the free plan, with `/health` as the health check and the three
environment variables a deployment needs. To use it, push the repository to a Git provider
that the Render account is connected to, then in the Render dashboard:

1. Click **New +** and choose **Blueprint**.
2. Pick the repository (connect the account if Render does not list it yet). Render reads
   `render.yaml` from it and shows the one service it declares.
3. Click **Apply**. Render builds the Dockerfile, starts the container with `PORT` set, waits
   for `GET /health` to answer 200, and gives the service a public `onrender.com` URL.

After that, every push to the default branch redeploys. The environment variables live in
the service's **Environment** tab, where they can be changed without touching the file:

| Variable | Blueprint value | What to set |
| --- | --- | --- |
| `DISPATCH_DATABASE_URL` | `sqlite:////tmp/dispatch.sqlite` | Leave it. The container's user can write to `/tmp`. |
| `DISPATCH_CORS_ORIGINS` | blank | The origin of the browser client, for example `https://my-dashboard.example`. |
| `DISPATCH_WEBHOOK_URL` | blank | A receiver URL once there is one that is reachable from the internet. Blank keeps the worker off. |

What the free plan means for this demo, from Render's own documentation at the time of
writing: a free web service has no persistent disk, and any change to its filesystem is lost
whenever the service redeploys, restarts or spins down, so the SQLite database under `/tmp`
starts empty again each time; a free service spins down after 15 minutes without inbound
traffic and takes around a minute to come back on the next request, so the first call after
a pause is slow and an `EventSource` client should expect at least one reconnect; and each
workspace gets 750 free instance hours a month. None of this matters for synthetic demo data.
A paid instance with a persistent disk mounted at `/srv/data` (and `DISPATCH_DATABASE_URL`
pointed there) would keep the data; PostgreSQL on Render would be the real answer, and the
code is written for it but has not been run against it.

The blueprint was applied to a Render workspace on 2026-10-28 (New + -> Blueprint -> this
repository -> Deploy Blueprint). Render parsed the file as written, built the Dockerfile in
about 1 minute 10 seconds, passed the `/health` check, and published the service at
https://dispatch-service-d7gk.onrender.com; `GET /health` there returns
`{"status":"ok","database":"ok"}` and `/docs` serves the OpenAPI UI. Every push to `main`
redeploys it.

## Reproduce and explain before using on a resume

If this project is going on a resume, run it first and be able to explain it without notes.
A reasonable bar:

1. Run the test suite and the load script yourself, on your machine, and replace the numbers
   in VERIFIED_RESULTS.md with your own.
2. Read `app/db.py`, `app/services/orders.py` and `tests/test_concurrency.py` end to end and
   be able to say, in your own words, why two concurrent assigns cannot double book, what each
   of the three layers would catch, and what would break on PostgreSQL if `FOR UPDATE` were
   removed.
3. Break it on purpose: change `BEGIN IMMEDIATE` to `BEGIN DEFERRED` and remove the write
   lock, run the concurrency tests, and watch what fails and how. Then explain why.
4. Run the receiver with `--fail-first 5`, create one order, and watch the five attempts and
   the exhausted row. Then explain why the receiver, not the sender, has to deduplicate.
5. Open `/orders/stream` in one terminal, kill and restart the server, and reconnect with
   `Last-Event-ID`. Explain where the catch-up read happens and why it is ordered before the
   live queue is drained.
6. Work through LEARNING_GUIDE.md and answer each item aloud.
7. Decide which claim you are making. "Built a dispatch API with transactional courier
   assignment, a server-sent event feed with resumable delivery, and a transactional outbox
   with retried webhooks, verified with concurrency and integration tests" is defensible. Any
   throughput figure is not, unless you also say where and how it was measured, and "deployed
   on Render" means a free-tier demo instance with synthetic data, nothing more.

## Layout

```
app/
  main.py              uvicorn entrypoint
  factory.py           create_app(): wires settings, engine, metrics, feed, outbox worker, CORS, routers
  config.py            Settings from DISPATCH_* environment variables
  db.py                engine, SQLite BEGIN IMMEDIATE control, write queue, schema loading
  models.py            SQLAlchemy Core tables mirroring schema.sql
  state.py             order states, legal transitions, InvalidTransition
  schemas.py           pydantic request and response models
  errors.py            ApiError and the JSON error envelope handlers
  middleware.py        request id, JSON request log, HTTP metrics, body size limit
  request_logging.py   JSON formatter and the request id context variable
  metrics.py           in-process Prometheus-style registry
  feed.py              in-process publish/subscribe of committed order events
  sse.py               server-sent event framing and the resumable stream generator
  outbox.py            outbox rows, backoff schedule, compare-and-set claim, webhook worker
  geo.py               haversine distance
  export.py            S3 export of one day of delivered orders
  api/                 routers: orders (including /orders/stream), couriers, parties, system
  services/            use cases: orders, couriers, parties, common helpers
schema.sql             portable DDL (SQLite and PostgreSQL)
scripts/seed.py        synthetic merchants, customers and couriers
scripts/load.py        lifecycle load generator with p50/p95/p99 output
scripts/webhook_receiver.py   prints deliveries, flags repeats, can fail the first N on purpose
scripts/export_to_s3.py
tests/                 125 tests (pytest)
Dockerfile, docker-compose.yml, render.yaml, .github/workflows/ci.yml
LEARNING_GUIDE.md      interview walk-through of the design
VERIFIED_RESULTS.md    what was run, where, and what came out
```
