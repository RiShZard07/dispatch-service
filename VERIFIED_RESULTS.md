# Verified results

What was actually run, where, and what came out. Everything below is from one session on one
machine; all data involved is synthetic. If you reproduce this, replace the numbers with your
own rather than quoting these.

## Environment

- Linux x86_64, 2 CPU cores, Python 3.13.17 (CI and the Docker image use Python 3.12).
- fastapi 0.162.0, starlette 1.7.0, sqlalchemy 2.1.6, httpx 0.28.1, uvicorn 0.54.0,
  pytest 9.2.0, boto3 1.43.120, moto 5.2.5, SQLite library 3.53.1.
- Installed with `pip install -r requirements.txt`.

## Test suite

```
$ python3 -m pytest -q
125 passed in 18.94s
```

125 tests across 16 files (106 test functions, some parametrized); 86 of them predate the
feed, outbox, CORS and Render work and all still pass unchanged apart from the two service
signatures that gained a `feed` argument. Lint and formatting: `ruff check` and
`ruff format --check` with `ruff.toml` report no findings (ruff 0.17.3).

The earlier session ran the 86-test suite six times in a row and the concurrency file eight
more times while checking for flakiness. In this session, with the final logic (only a
docstring and an assignment style in `app/outbox.py` changed after the first 24 of these
runs), the full suite was run 66 times: 52 runs one after another, 12 runs as three
concurrent pytest processes on the two cores (four rounds of three, to disturb the timing),
and 2 runs as two concurrent processes; the four thread-heavy new files (`test_stream.py`,
`test_outbox.py`, `test_cors.py`, `test_scripts.py`, 35 tests) were run six more times on
their own. All of those passed. One run before them, the first of a batch of three launched
right after the last test was added, reported `1 failed, 124 passed`; its output was piped
through `tail -1`, so which test failed and why was not captured, and the 66 runs that
followed (30 of them with full tracebacks saved to disk in case it recurred) did not
reproduce it. It is recorded here because it happened. Nothing is marked flaky, skipped or
retried.
Timing-dependent assertions use deadlines (`tests/support.py::wait_until`, 10 s default,
polled every 10 ms) rather than fixed sleeps, except one 0.3 s sleep that checks nothing
arrives without a URL and one 0.2 s sleep that checks no second delivery arrives.

The three concurrency scenarios use real threads: 50 assigns against 50 couriers (50 distinct
bookings), 100 assigns against 10 couriers (exactly 10 bookings, 90 clean 409s, counters 10
and 90), and 40 assigns split across two independent app instances on one database file
against 10 couriers (10 bookings, 30 409s). They now also exercise the outbox insert and the
global event id inside every write transaction.

What the new tests actually do, because the mechanism matters for what they prove:

- Stream tests (`tests/test_stream.py`, 9 tests) start uvicorn on a free localhost port in a
  thread, with the app's lifespan on, and read `/orders/stream` with `httpx.Client.stream`.
  The TestClient cannot be used for this: it buffers a whole response before returning it, so
  an endless stream would never return. Checked over real HTTP: the `text/event-stream`
  headers, `retry:` preamble, `id:`/`event:`/`data:` framing, three live events in id order,
  resume with `Last-Event-ID` after a disconnect (exactly the three missed events, then a live
  one), `?after=` and the header taking precedence over it, three heartbeat comments with a
  50 ms heartbeat, an event written through a second app instance on the same database file
  arriving through the heartbeat's catch-up read, and the subscription count dropping to zero
  after the client closes. One test drives the generator directly under `asyncio.run` and
  publishes event 4 before event 3 on purpose; the stream emits 3 then 4 once each.
- Outbox tests (`tests/test_outbox.py`, 15 tests) start the repository's own
  `scripts/webhook_receiver.py` on a free localhost port inside the test and point the
  app's worker at it. Checked: one outbox row per transition with the payload reconciled
  against `/orders/{id}/events`; no row for a 409 (invalid transition, no courier, stale
  `If-Match`) or an idempotent replay; two rows delivered exactly once with the expected body
  and headers and `delivered_at` set; a receiver that fails the first two deliveries sees
  attempts 1, 2, 3 of the same delivery id and the row ends delivered with `attempts = 3`;
  with a fake clock and `run_once()` driven by hand, five failures schedule the next try at
  1, 2, 4, 8 and 16 seconds, the sixth pass does nothing and the metrics report one
  exhausted row; a row is not due before its backoff has elapsed; the claim is a
  compare-and-set that fails the second time; a connection-refused receiver is recorded in
  `last_error`; nothing is delivered and the worker does not exist when the URL is unset;
  the worker starts and stops with the app twice over; `stop()` interrupts a 60 s poll sleep;
  a commit wakes a worker configured to poll every 60 s so the delivery arrives within the
  test's 5 s deadline; and a backlog of 107 rows (two full batches and a bit) is cleared by
  one `drain()` call, in id order, once each.
- CORS tests (`tests/test_cors.py`, 5 tests) use the TestClient: an allowed-origin preflight
  gets 200 with the origin, methods, requested headers and max-age echoed plus a request id;
  an unlisted origin gets 400 with no CORS headers; simple requests (201 and 404) carry the
  origin and expose `X-Request-ID`; a 413 from the body limit still carries the origin; with
  no configuration there are no CORS headers and `OPTIONS /orders` is a plain 405.
- `tests/test_config.py` (4), `tests/test_deploy.py` (2, static checks of the Dockerfile and
  `render.yaml`), two list/event tests in `tests/test_orders.py`, a stale-database test in
  `tests/test_schema.py` and the receiver test in `tests/test_scripts.py` make up the rest.

## Server, seed and load

```
$ uvicorn app.main:app --port 8000 --no-access-log
$ curl -s http://127.0.0.1:8000/health
{"status":"ok","database":"ok"}

$ python3 scripts/seed.py --base-url http://127.0.0.1:8000 --merchants 5 --customers 10 --couriers 20
(prints the created ids: 5 merchants, 10 customers, 20 couriers)

$ python3 scripts/load.py --base-url http://127.0.0.1:8000 --orders 200 --concurrency 1
dispatch-service load run (synthetic data; local single-process numbers, not a capacity claim)
orders: 200 requested, 200 completed, 0 failed; concurrency 1; elapsed 5.15 s
throughput: 38.8 orders/s, 194.1 requests/s
latency (ms)        p50      p95      p99      max      n
  create           4.80     6.90     8.68    20.71    200
  accept           4.55     7.03    14.79    44.08    200
  assign           5.48     9.04    13.81    17.01    200
  pickup           4.47     6.88     8.93    11.03    200
  deliver          4.84     7.23     8.26    10.00    200
  lifecycle       24.61    36.08    42.65    68.89    200

$ python3 scripts/load.py --base-url http://127.0.0.1:8000 --orders 200 --concurrency 10
dispatch-service load run (synthetic data; local single-process numbers, not a capacity claim)
orders: 200 requested, 200 completed, 0 failed; concurrency 10; elapsed 3.66 s
throughput: 54.6 orders/s, 272.9 requests/s
latency (ms)        p50      p95      p99      max      n
  create          32.76    45.17    60.95    67.74    200
  accept          34.97    49.66    61.95    67.63    200
  assign          40.24    53.96    59.91    68.78    200
  pickup          34.87    51.04    55.73    57.79    200
  deliver         34.30    48.57    60.02    68.21    200
  lifecycle      181.48   214.64   231.74   241.62    200

$ curl -s http://127.0.0.1:8000/metrics | grep -E '^(assignments_total|assignment_conflicts_total|orders_by_state\{state="DELIVERED"\})'
orders_by_state{state="DELIVERED"} 400
assignments_total 400
assignment_conflicts_total 0
```

These are local, single-process numbers: the load generator and the server shared two cores,
the server ran one uvicorn process, and the database was a SQLite file on local disk. They are
reproducible evidence that the lifecycle works under concurrent load, not a capacity claim.
Throughput plateaus at a few hundred requests per second because writes are serialized by
design; latency at concurrency 10 is roughly ten times the latency at concurrency 1, which is
what a single-writer store should show.

### The same run before the in-process write queue

Earlier in the session the service relied on SQLite's busy handler alone to queue writers. The
load script at concurrency 10 then produced (same machine, same parameters):

```
orders: 200 requested, 200 completed, 0 failed; concurrency 10; elapsed 4.29 s
throughput: 46.6 orders/s, 233.2 requests/s
latency (ms)        p50      p95      p99      max      n
  create          13.84   240.33   672.07   941.57    200
  lifecycle      133.76   699.47   968.51  1342.57    200
```

and a second run with a p99 of 655 ms and a maximum of 1960 ms for `create`. The busy
handler's sleep-and-retry backoff is the cause (see `app/db.py` and LEARNING_GUIDE.md item
10). Adding the per-engine write queue brought the concurrency-10 p99 to about 60 ms; the
correctness results were identical before and after, which is the point: the queue is a
latency measure, `BEGIN IMMEDIATE` is the correctness measure.

### The same run after the feed and the outbox were added

Every write transaction now also assigns a global event id (`MAX(id) + 1`), inserts an
`outbox` row (another `MAX(id) + 1` plus an insert) and publishes to the in-process feed after
commit. Same machine class (2 cores), a later session, fresh database file, no stream clients
connected:

```
$ python3 scripts/load.py --base-url http://127.0.0.1:8765 --orders 200 --concurrency 1
orders: 200 requested, 200 completed, 0 failed; concurrency 1; elapsed 4.86 s
throughput: 41.1 orders/s, 205.7 requests/s
latency (ms)        p50      p95      p99      max      n
  create           4.37     9.47    16.03    19.29    200
  assign           4.68     7.95    14.37    18.59    200
  lifecycle       21.82    37.90    51.17    56.81    200

$ python3 scripts/load.py --base-url http://127.0.0.1:8765 --orders 200 --concurrency 10
orders: 200 requested, 200 completed, 0 failed; concurrency 10; elapsed 4.65 s
throughput: 43.0 orders/s, 215.1 requests/s
latency (ms)        p50      p95      p99      max      n
  create          43.21    71.16    77.31    85.28    200
  assign          50.81    72.77    81.74   108.50    200
  lifecycle      229.08   284.24   306.70   328.51    200

$ curl -s http://127.0.0.1:8765/metrics | grep -E '^(outbox_rows|webhook_)'
outbox_rows{status="pending"} 2000
outbox_rows{status="delivered"} 0
outbox_rows{status="exhausted"} 0
webhook_deliveries_total 0
webhook_failures_total 0
```

Per-step p50 at concurrency 1 is unchanged within noise (about 4 to 5 ms); at concurrency 10
the p50 moved from about 35 ms to about 45 ms and throughput from 272.9 to 215.1 requests per
second. Two extra inserts and two `MAX()` lookups per transaction, all under the single
writer, are a plausible explanation, but the two runs are from different sessions and the
difference is within what this machine shows run to run, so read it as "the outbox costs
something, not nothing" rather than as a measured percentage. With no `DISPATCH_WEBHOOK_URL`
the 2000 rows (five transitions for each of 400 orders across both runs) stay pending, which
is the documented behaviour.

### Load with the webhook worker delivering to the receiver script

```
$ python3 scripts/webhook_receiver.py --port 9113 > receiver.log
$ DISPATCH_WEBHOOK_URL=http://127.0.0.1:9113/webhook uvicorn app.main:app --port 8769 --no-access-log
$ python3 scripts/seed.py --base-url http://127.0.0.1:8769
$ python3 scripts/load.py --base-url http://127.0.0.1:8769 --orders 200 --concurrency 10
orders: 200 requested, 200 completed, 0 failed; concurrency 10; elapsed 5.60 s
throughput: 35.7 orders/s, 178.7 requests/s
latency (ms)        p50      p95      p99      max      n
  create          52.64    64.05    71.01    79.60    200
  assign          59.97    80.32    85.45    90.58    200
  lifecycle      283.86   319.83   325.84   340.61    200
```

`outbox_rows{status="pending"}` reached 0 about 2.7 seconds after the load run finished
(polled every 0.25 s); then `outbox_rows{status="delivered"} 1000`,
`webhook_deliveries_total 1000`, `webhook_failures_total 0`, 1000 lines in the receiver's log,
none flagged as a duplicate, and no error lines in the server log. The worker delivers one
row at a time (claim, POST, mark: two short write transactions and one HTTP round trip each)
and competes with the API for the single SQLite writer while both are busy, which is the
likely reason throughput at concurrency 10 came out around 15 percent lower than the run
without a webhook URL (178.7 against 215.1 requests per second). That is a one-run
comparison on a shared two-core machine with the receiver on the same host; the direction is
plausible, the size is not a measurement to quote.

An earlier version of the worker in this same session handled at most one batch of 50 due
rows per poll interval and then slept, so the same backlog took about 18 seconds to clear
after the load stopped (roughly 50 deliveries per second, which is the batch size per
second). The loop now keeps running passes while a pass comes back with a full batch; the
number above is after that change.

### The backoff schedule, observed

```
$ python3 scripts/webhook_receiver.py --port 9112 --fail-first 5   # timestamps added by the shell
$ DISPATCH_WEBHOOK_URL=http://127.0.0.1:9112/webhook uvicorn app.main:app --port 8767 --no-access-log
$ curl -X POST localhost:8767/orders ...   # one order, so one outbox row
1793205192.171 delivery id=1 type=order.created attempt=1 ... -> answering 500 on purpose
1793205193.174 delivery id=1 type=order.created attempt=2 ... -> answering 500 on purpose
1793205195.183 delivery id=1 type=order.created attempt=3 ... -> answering 500 on purpose
1793205199.190 delivery id=1 type=order.created attempt=4 ... -> answering 500 on purpose
1793205207.208 delivery id=1 type=order.created attempt=5 ... -> answering 500 on purpose
$ curl -s localhost:8767/metrics | grep -E '^(outbox_rows|webhook_)'
outbox_rows{status="pending"} 0
outbox_rows{status="delivered"} 0
outbox_rows{status="exhausted"} 1
webhook_deliveries_total 0
webhook_failures_total 5
```

Attempts at 0, 1.00, 3.01, 7.02 and 15.04 seconds after the first: the 1, 2, 4, 8 second
schedule with the default one-second base, then nothing more. The poll interval added at
most a few milliseconds here because the first attempt was woken by the commit and each
retry fell due just before a poll tick; it can add up to one full `DISPATCH_OUTBOX_POLL_SECONDS`.

## S3 export

`tests/test_export.py` (five tests) runs the export against moto's in-memory S3 mock: it
checks that the NDJSON object and the manifest are written, that the records reconcile with
the DELIVERED orders in the database for the requested UTC day and nothing else, that a re-run
overwrites both objects with byte-identical content, that a missing bucket produces a clear
`ExportError`, and that the command-line entrypoint reads its configuration from the
environment and exits non-zero on failure. The export has not been run against a live AWS
account or any real S3-compatible endpoint.

## Real-time feed and CORS by hand

Beyond the tests, with the server started with `DISPATCH_STREAM_HEARTBEAT_SECONDS=2` and
`DISPATCH_CORS_ORIGINS=http://localhost:5173`:

- `curl -sN http://127.0.0.1:8768/orders/stream` was left open while
  `scripts/load.py --orders 4 --concurrency 2` ran. Its output held `retry: 3000`, then 20
  events (`id: 1` to `id: 20`, five per order) and four `: heartbeat` lines over about 12
  seconds, in that framing.
- `curl -sN -H 'Last-Event-ID: 18' .../orders/stream` returned the preamble, then exactly
  `id: 19` and `id: 20` (both `to_state: DELIVERED`), then went quiet. `?after=18` behaved
  the same.
- `curl -X OPTIONS .../orders` with `Origin: http://localhost:5173`,
  `Access-Control-Request-Method: POST` and `Access-Control-Request-Headers:
  idempotency-key,content-type` returned 200 with `access-control-allow-origin:
  http://localhost:5173`, the requested headers echoed, `access-control-max-age: 600` and an
  `x-request-id`. The same preflight from `Origin: http://evil.example` returned
  `400 Bad Request` with the body `Disallowed CORS origin` and no allow-origin header. A plain
  `GET /health` with the allowed origin carried `access-control-allow-origin` and
  `access-control-expose-headers: X-Request-ID`.
- The server log had no error-level lines after any of this, including the stream
  disconnects.

A browser `EventSource` client has not been run against the feed; the automatic
reconnect-and-resume attributed to browsers is the documented behaviour of the SSE standard,
verified here only with httpx and curl sending `Last-Event-ID` themselves. No actual browser
has made a cross-origin call to the API.

## Docker

The Docker image was not built on this machine; Docker is not available here. The Dockerfile
changed in this session (it now reads `PORT`, defaulting to 8000, and `exec`s uvicorn from
`sh -c` so it stays PID 1; it already ran as the unprivileged `dispatch` user). Those changes
are covered only by the static test in `tests/test_deploy.py` and by reading. CI
(`.github/workflows/ci.yml`) runs `docker build -t dispatch-service:ci .` after the test suite
on `ubuntu-latest` with Python 3.12, so the first push to a repository with Actions enabled is
where the image is verified. Until that has happened, treat the Dockerfile as unverified.

## Render

Not run. `render.yaml` was written against Render's published Blueprint reference (service
`type`, `runtime: docker`, `plan: free`, `healthCheckPath`, `envVars`) and the free-plan
facts in the README (no persistent disk, filesystem reset on redeploy or restart, spin-down
after 15 idle minutes, 750 free hours a month, `PORT` set by the platform) come from Render's
documentation as read on the day this was written. No Render account was used, nothing was
deployed, and the blueprint has not been parsed by Render. The static test checks the file
says what the README says it says, nothing more.

## PostgreSQL

Not run. The DDL and the `FOR UPDATE` path are written for it but nothing in this session
exercised a PostgreSQL server. The new `MAX(id) + 1` id assignment for `order_events` and
`outbox` is race-free only under SQLite's write lock; on PostgreSQL it would need an identity
column, which is noted in `schema.sql` and `app/services/common.py`.
