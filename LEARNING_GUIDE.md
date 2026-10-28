# Learning guide

Sixteen things an interviewer could ask about this project, with the answer as it applies to
this code and a pointer to where to look. The aim is to be able to answer each one aloud,
without notes, and to know what the honest follow-up is. All data in the project is
synthetic; the project is an educational prototype and has never been deployed.

## 1. What does an idempotency key do that a plain retry does not?

A client that times out on `POST /orders` does not know whether the order was created. Without
a key, retrying risks a second order and not retrying risks none. With `Idempotency-Key`, the
server stores the key with the order (`orders.idempotency_key`, unique) and a hash of the
canonical request body (`orders.request_hash`). A retry with the same key and body returns the
original order with 200; the same key with a different body is a 409 `idempotency_conflict`,
because the client is confused and silently returning either order would hide that.

Where: `app/services/orders.py::create_order`, `tests/test_idempotency.py`,
`tests/test_concurrency.py::test_concurrent_creates_with_one_idempotency_key_make_one_order`.

Follow-up: the key is global here. In a multi-tenant API it must be scoped to the caller, and
it should expire. The unique index is the backstop on PostgreSQL if two creates with the same
key race past the initial lookup; on SQLite the write queue and `BEGIN IMMEDIATE` make that
race impossible.

## 2. Why 409 for some failures and 422 for others?

422 means "the request itself is wrong": a missing field, a negative quantity, an unknown
merchant id in the body. Fixing the request fixes the problem. 409 means "the request is fine
but it conflicts with the current state": cancelling a delivered order, reusing a key with a
different body, acting on a stale version, asking for a courier when none is free. Retrying
the same request later might succeed (no courier available) or never will (invalid
transition), and the `code` in the envelope says which. 404 is reserved for ids in the URL.

Where: `app/errors.py`, the table of codes in README.md.

Follow-up: why not 400 for everything? Because clients branch on status codes, and a
dashboard that treats every 4xx as "show the message" loses the distinction between "fix your
input" and "refresh and look again".

## 3. Why does assignment need a transaction, and which isolation does it rely on?

Assignment reads the free couriers, picks one, marks it busy and moves the order, and writes
an audit row. If any of those happen without the others, the data lies: a courier booked for
no order, an order assigned to a courier who is still "free", an audit trail with a gap. So
they run in one transaction and commit together.

Isolation is the harder part. Two assigns running at the same time must not both read
"courier X is free". On SQLite a plain `BEGIN` is deferred: the transaction only takes the
write lock when it first writes, so both can finish their reads and then collide. `BEGIN
IMMEDIATE` takes the write lock before the first read, so the second transaction waits and then
observes the first one's commit. On PostgreSQL the default isolation is READ COMMITTED, where
two transactions can also both read "free"; `SELECT ... FOR UPDATE` on the candidate couriers
makes the second one block, and when it resumes it re-checks the `WHERE available = true`
predicate against the committed row, so the taken courier drops out of its result.

Where: `app/db.py` (the module docstring and the `begin` event), `app/services/orders.py::assign_order`.

Follow-up: SERIALIZABLE isolation would also work on PostgreSQL by aborting one of the two
transactions with a serialization failure, which the application would have to retry. Explicit
row locks are the cheaper, more predictable tool here.

## 4. Optimistic or pessimistic locking, and why does this project have both?

Pessimistic locking takes the lock first (`BEGIN IMMEDIATE`, `FOR UPDATE`) so conflicts wait
instead of failing. Optimistic locking does the work and then checks nothing moved
(`UPDATE ... WHERE version = ?`, and 409 if zero rows changed). Pessimistic suits short,
contended, in-database work like booking a courier. Optimistic suits long gaps that no lock
should span: a person reads an order at version 3 on a screen, thinks for a minute, then
clicks "cancel". `If-Match: 3` lets the server refuse if the order moved in the meantime.

This project uses pessimistic locking inside the transaction and keeps the version compare-and-set
as a second, independent check that the locking actually worked. The update can only match
zero rows if something slipped past the lock, which is exactly when you want a loud failure.

Where: `app/services/orders.py::_record_transition`, `app/services/common.py::check_expected_version`,
`tests/test_orders.py::test_if_match_with_stale_version_is_409`.

## 5. What is a histogram bucket, and how does p95 come out of it?

The latency histogram keeps, per route, a counter for each upper bound (`le="0.005"`,
`le="0.01"`, ... `le="+Inf"`), each counting every observation less than or equal to the
bound, so the counts are cumulative, plus a `_sum` and a `_count`. The registry computes
nothing else. A Prometheus server estimates p95 with `histogram_quantile(0.95, ...)` by finding
the bucket where the cumulative count crosses 95 percent of the total and interpolating inside
it. That estimate is only as fine as the bucket boundaries: if every request lands between
5 ms and 10 ms, the server cannot tell 6 ms from 9 ms. Cumulative buckets have one big
advantage over exact percentiles: they can be added across instances and over time, while
percentiles cannot be averaged.

The load script computes exact nearest-rank percentiles from the raw samples it holds in
memory, which is why its p99 is precise and a Prometheus p99 of the same traffic would not be.

Where: `app/metrics.py::Histogram`, `scripts/load.py::percentile`,
`tests/test_metrics.py::test_histogram_buckets_are_cumulative_and_sum_and_count_are_right`.

Follow-up: why label by route template and not by path? `/orders/ord_abc` and `/orders/ord_def`
are the same route; labelling by raw path would create one series per order and overwhelm
the metrics store.

## 6. What is a request id for?

It ties together everything that happened because of one HTTP request: the request log line,
a service log line such as "order assigned", the error with its traceback if something broke,
and the response the client got (`X-Request-ID` is echoed back). When a client reports "my
assign failed at 15:42", the id is how you find the right log lines among thousands. The id is
accepted from the client when present and well formed so that an upstream gateway can
propagate its own, and generated otherwise. It lives in a context variable, which Starlette
copies into the worker thread that runs the endpoint, so nothing has to pass it by hand.

Where: `app/middleware.py::ObservabilityMiddleware`, `app/request_logging.py`,
`tests/test_middleware.py`.

Follow-up: a request id is not a trace. A trace has spans with parent and child
relationships across services; the id here is the single-service version of that idea.

## 7. How does the state machine prevent invalid orders?

`app/state.py` has one table: for each state, the set of states it may move to. Every
transition endpoint locks the order row, reads its committed state, and calls
`assert_transition(current, target)`. There is no other way to change `orders.state`. Because
the check happens inside the write transaction after the lock, it cannot be fooled by a client
that read the order a second ago; the test with twenty threads accepting the same order ends
with one 200 and nineteen 409s, and exactly one audit event. Terminal states have empty sets,
so nothing can revive a delivered or cancelled order.

Where: `app/state.py`, `tests/test_state.py`, `tests/test_orders.py::test_illegal_transitions_are_409_and_change_nothing`.

Follow-up: what about transitions that need extra conditions, such as "cancel after
assignment only if the courier has not picked up"? That is a guard, not a transition; the
table would grow a predicate per edge, or the service would check it after the table says
the edge exists.

## 8. What changes at 100 times the load?

Today the service does around 250 requests per second on one laptop core, limited by a single
SQLite writer and one Python process. At 100 times that, roughly 25,000 requests per second:

- SQLite is out. Writes need a database that handles concurrent writers with row locks and a
  lock queue, which the code already anticipates with `FOR UPDATE`. The schema stays.
- One process is out. Several instances behind a load balancer, each stateless, which is why
  nothing here keeps state in memory except metrics (which would then need per-instance
  scraping or a shared store).
- Nearest-courier selection by scanning every free courier is out. A geospatial index (a
  geohash prefix lookup, PostGIS, or an in-memory grid) narrows candidates to a few dozen.
- Hot rows become the problem. Every assign in a city locks the same handful of free couriers.
  Sharding by region keeps contention local.
- Request-time work moves off the request. Assignment becomes a job: the client gets 202 and
  a status to poll, a worker batches assignments every few hundred milliseconds and can make
  better global choices than "nearest free courier right now".
- Idempotency keys and courier locations move to a store with TTLs, such as Redis.
- Offset pagination becomes keyset pagination.
- The in-process feed behind `/orders/stream` becomes a broker (an append-only log or a
  pub/sub channel) that every API instance publishes to and every stream subscribes to; the
  resume contract (`Last-Event-ID` is the global event id) stays the same, because the
  broker only replaces the doorbell, not the table the catch-up read uses.
- The outbox worker leaves the API process and becomes one or more workers of its own,
  which the compare-and-set claim on `attempts` already allows, with delivery to a queue or
  to many webhook URLs rather than one, and with the outbox pruned or partitioned by day.

What does not change: the state machine, the audit trail written in the same transaction, the
outbox row written next to it, the error envelope, the request id, and the version check.
Those are cheap at any scale.

## 9. What does the outbox pattern fix, and what does it cost?

The service notifies a webhook receiver on every order transition. Doing that inside the
request means either calling the receiver inside the database transaction (holding the write
lock while waiting on a network, so one slow receiver stalls every writer) or after the commit
(and losing the notification if the process dies in between, with nothing recording that it
was ever owed). Both are the dual-write problem: two systems, no shared transaction.

The outbox is a table in the same database. The transition transaction inserts an `outbox` row
next to the order update and the audit event, so the three commit or roll back together; a
409 writes none of them. A worker thread reads due rows, POSTs them, and marks them delivered.
The cost is that delivery becomes at-least-once (item 14), that there is now a worker to run
and monitor (`outbox_rows{status="exhausted"}` is the alarm), and that the outbox grows
forever unless something prunes it.

Where: `app/outbox.py`, `app/services/orders.py::_append_event`,
`tests/test_outbox.py::test_every_transition_writes_one_outbox_row_in_the_same_transaction`
and `::test_failed_transitions_and_replays_write_no_outbox_row`.

Follow-up: why is the outbox separate from `order_events`, which already records every change
transactionally? Because the audit trail is immutable history and the outbox is mutable
delivery bookkeeping (`attempts`, `next_attempt_at`, `delivered_at`, `last_error`). Mixing the
two would mean updating audit rows, and it would tie the trail to one consumer; a second
consumer (the S3 export, analytics) would want its own outbox rows or its own cursor.

## 10. Why does SQLite serialize writers, and what does the in-process write queue add?

SQLite has one write lock per database file. In WAL mode readers never block and are never
blocked, but writers go one at a time. When a writer finds the lock taken, SQLite's busy
handler sleeps and retries with growing delays (1, 2, 5, 10, 15, 20, 25 ... milliseconds).
With ten writers contending, the lock often sits free while every waiter is asleep, and
whoever wakes first wins, so latency is both high and unfair. Measured here, the load script at
concurrency 10 had a per-request p99 around 650 ms with p50 around 14 ms.

The fix inside one process is to queue writers on a `threading.Lock` before they touch the
database. A released lock wakes the next waiter immediately, so ten writers at roughly 3 ms
each cost the last one about 30 ms, not hundreds. After the change the same load run had a
p99 around 65 ms. The lock is per engine, so it does nothing across processes; `BEGIN
IMMEDIATE` underneath still guarantees correctness there, which the two-instance test checks.

Where: `app/db.py`, VERIFIED_RESULTS.md, `tests/test_concurrency.py::test_two_app_instances_on_one_database_file_never_double_book`.

Follow-up: `PRAGMA synchronous=NORMAL` is the other tuning. In WAL mode it keeps the database
consistent after a crash and only risks the most recent commits on power loss, at about a
fifth of the per-commit cost of `FULL`. A system that moves money would keep `FULL`.

## 11. Why is the audit event's sequence number the order's new version?

Every accepted transition does `UPDATE orders SET version = version + 1 ...` and inserts an
`order_events` row with `seq = that new version`, in the same transaction. That gives three
properties for free: `(order_id, seq)` is a natural primary key, events sort in the order they
happened without relying on timestamps that can tie, and anyone can check that an order with
`version = 5` has exactly five events with no gaps. A rejected transition inserts nothing and
bumps nothing, so a 409 leaves no trace in the trail, which is the right behaviour for an
audit log of what happened rather than what was attempted.

Where: `app/services/orders.py::_record_transition`, `schema.sql`,
`tests/test_orders.py::test_audit_trail_records_every_transition_and_nothing_else`.

## 12. The S3 export works against moto. What changes with real S3?

The code path is the same two `put_object` calls, but the environment is not.

- Credentials and permissions. moto accepts anything. Real S3 needs an identity with
  `s3:PutObject` on that bucket and prefix, ideally a role attached to the machine or job
  rather than long-lived keys in environment variables, and the first failure you meet is
  usually `AccessDenied`, not `NoSuchBucket`.
- Retries and throttling. boto3 retries transient errors (connection resets, 5xx, slow-down
  responses) a few times by default. A nightly export should set an explicit retry mode and
  treat a final failure as an alert, because nothing re-runs it otherwise.
- Consistency. S3 has been read-after-write consistent for new and overwritten objects since
  2020, so a consumer that reads the manifest after the export finishes sees the new content.
  Eventual consistency is no longer the concern it once was.
- Partial writes and multipart. Each `put_object` is atomic: an object is either entirely
  there or not. But the export writes two objects, and a crash between them leaves a new data
  file with an old or missing manifest. Writing the manifest last, and having consumers trust
  only what the manifest describes (including checking its `sha256`), is what makes the
  two-object export safe. Above roughly 100 MB a single `put_object` should become a
  multipart upload, which is resumable and parallel but can leave orphaned parts that need a
  lifecycle rule to clean up.
- Cost and layout. One object per day per prefix is fine for a nightly batch. A consumer that
  queries by merchant would want a partitioned layout (`merchant_id=.../day=...`) and a
  columnar format instead of NDJSON.

Where: `app/export.py`, `scripts/export_to_s3.py`, `tests/test_export.py`. Be clear that the
export was never run against a live bucket.

## 13. Server-sent events, websockets or polling for the order feed?

Polling is a client asking `GET /orders/{id}/events` every few seconds. It needs nothing on
the server, works through anything, and is the right answer when updates are rare or lag does
not matter. Its cost scales with clients times polling rate, not with events, and the lag is
half the interval on average.

Websockets open a bidirectional channel after an HTTP upgrade. They are the right tool when
both sides talk (chat, a game, a collaborative editor) or when messages are binary or very
frequent. The cost is a second protocol: proxies and load balancers need to support the
upgrade, browsers do not reconnect for you, and the client has to implement framing, resume
and heartbeats itself.

Server-sent events are plain HTTP: a response with `Content-Type: text/event-stream` that
never finishes, carrying `id:`, `event:` and `data:` lines separated by blank lines. The
browser's `EventSource` reconnects on its own and sends the last `id` it saw back as
`Last-Event-ID`, so resuming is part of the protocol rather than something to design. The
limits are that it is one-directional, text only, and (over HTTP/1.1) one of the browser's
small per-host connection budget. This feed is one-directional and low-volume, so SSE is the
fit; the resume contract was the deciding feature.

Where: `app/sse.py`, `app/api/orders.py::stream`, `tests/test_stream.py`.

Follow-up: why does the stream read the table at every heartbeat when it already has a
queue? Because the queue is per process. A second uvicorn worker, or the two-instance test,
writes events this process never hears about, and the heartbeat read is the cheapest way to
stay complete without a broker. The queue gives latency; the table gives correctness.

## 14. Why is webhook delivery at-least-once, and why not fix that?

The worker claims a row (increments `attempts` with a compare-and-set, which also stops two
workers from trying the same row), POSTs it, and then writes `delivered_at`. The POST and the
`delivered_at` update are in two different systems: the receiver's and ours. If the process
dies between them, the receiver has the event and our table says it does not, so the row is
sent again. There is no transaction that spans an HTTP response and a database row, so the
duplicate cannot be engineered away; it can only be moved. Mark before sending and a crash
loses the event instead (at-most-once). Send and then mark, as here, and a crash repeats it
(at-least-once). Exactly-once is not available between two independent systems; what people
call exactly-once is at-least-once delivery plus idempotent consumption on the receiving side.

Where: `app/outbox.py::OutboxWorker._deliver` and `::claim`, README.md "Outbox and webhooks",
`tests/test_outbox.py::test_claim_is_a_compare_and_set_so_two_workers_cannot_both_try_a_row`.

Follow-up: what does the backoff buy? A receiver that is down for a minute gets five tries
spread over about 15 seconds with the default base of one second and then the row is parked
as exhausted. Retrying immediately would hammer a struggling receiver and burn the attempts in
milliseconds; doubling spreads them out at no cost to the common case, where the first try
succeeds. A production schedule would stretch to minutes or hours and would add jitter so
many rows failing at once do not retry in lockstep.

## 15. What must an idempotent consumer do with a webhook?

Treat the delivery `id` as the key. Before acting, check whether that id has been processed;
if so, answer 2xx and do nothing. Record the id in the same transaction as the effect the
event has (the row it updates, the message it sends), so a crash between the effect and the
record cannot leave the two disagreeing, which would be the dual-write problem again on the
receiving side. Answer 2xx only after the record is durable; answering first and then
crashing means the sender marks the row delivered and the effect never happened.

Two more things follow from retries. Deliveries can arrive out of order (event 41 is retried
after event 42 succeeded), so a consumer that builds state should compare `data.seq` with the
version it already holds and ignore anything older, or apply events in `data.event_id` order
from its own buffer. And a consumer should not trust the body alone in a real deployment: a
signature over the body with a shared secret is what proves the sender, and this project does
not add one.

Where: `scripts/webhook_receiver.py` (the set of seen ids and the "duplicate" line),
`tests/test_scripts.py::test_webhook_receiver_prints_each_delivery_and_flags_repeats`. This
is the same discipline as item 1, seen from the other side of the wire.

## 16. What is CORS actually protecting, and what does the preflight do?

A browser lets a page make requests to any origin, but it does not let the page read the
response from a different origin unless that origin says so. CORS is the server saying so,
per origin, with response headers. It protects users of other sites from a malicious page
reading data from this API with the user's cookies; it does not protect the API itself (curl
ignores CORS entirely), which is why it is not an authorization mechanism.

A preflight is an `OPTIONS` request the browser sends on its own before any request that is
not "simple", which in practice means anything with a JSON body or a custom header such as
`Idempotency-Key` or `If-Match`. It asks "may origin X send method Y with headers Z?" and
the server answers with `Access-Control-Allow-Origin`, `-Methods` and `-Headers`; the browser
caches the answer for `Access-Control-Max-Age` seconds. Only then does the real request go
out. The middleware here answers preflights before the router sees them, echoes the requested
headers back, and refuses an origin not in `DISPATCH_CORS_ORIGINS` with a 400. Response
headers the page is allowed to read are listed separately in `Access-Control-Expose-Headers`,
which is how the browser client gets to see `X-Request-ID`.

Where: `app/factory.py` (the middleware order and its comment), `tests/test_cors.py`.

Follow-up: why not `allow_origins=["*"]`? With no credentials it would work for this
prototype, but the list is the point: it is the one place that says which sites are supposed
to use this API from a browser, and a wildcard cannot be combined with cookies or
`Authorization` headers once authentication arrives.
