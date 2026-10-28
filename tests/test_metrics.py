"""Metrics: registry unit tests and the exposition after a known sequence of requests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.metrics import CONTENT_TYPE, Counter, GaugeFunc, Histogram, Registry, escape_label
from tests.support import World, make_order, metric_value


def test_registry_renders_help_type_and_samples_in_registration_order() -> None:
    registry = Registry()
    hits = registry.register(Counter("hits_total", "Hits.", ("route",)))
    registry.register(GaugeFunc("queue_depth", "Depth.", (), lambda: [((), 3.0)]))
    hits.inc("/a")
    hits.inc("/a", amount=2)
    hits.inc("/b")
    assert registry.render() == (
        "# HELP hits_total Hits.\n# TYPE hits_total counter\n"
        'hits_total{route="/a"} 3\nhits_total{route="/b"} 1\n'
        "# HELP queue_depth Depth.\n# TYPE queue_depth gauge\nqueue_depth 3\n"
    )
    assert hits.value("/a") == 3 and hits.value("/missing") == 0


def test_histogram_buckets_are_cumulative_and_sum_and_count_are_right() -> None:
    histogram = Histogram("latency_seconds", "Latency.", (), buckets=(0.1, 1.0))
    for value in (0.0625, 0.5, 2.0):  # exactly representable, so the sum renders without rounding noise
        histogram.observe(value)
    assert list(histogram.samples()) == [
        'latency_seconds_bucket{le="0.1"} 1',
        'latency_seconds_bucket{le="1.0"} 2',
        'latency_seconds_bucket{le="+Inf"} 3',
        "latency_seconds_sum 2.5625",
        "latency_seconds_count 3",
    ]


def test_label_values_are_escaped() -> None:
    assert escape_label('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    counter = Counter("c_total", "C.", ("path",))
    counter.inc('/x"y')
    assert list(counter.samples()) == ['c_total{path="/x\\"y"} 1']


def test_exposition_after_a_known_sequence(client: TestClient, world: World) -> None:
    order = make_order(client, world, "k1")
    client.get(f"/orders/{order['id']}")
    client.get("/orders/ord_missing")
    client.get("/nothing-here")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE
    text = response.text
    assert "# TYPE http_requests_total counter" in text
    assert "# TYPE http_request_duration_seconds histogram" in text
    assert "# TYPE orders_by_state gauge" in text
    assert metric_value(text, 'http_requests_total{method="POST",route="/orders",status="201"}') == 1
    assert metric_value(text, 'http_requests_total{method="GET",route="/orders/{order_id}",status="200"}') == 1
    assert metric_value(text, 'http_requests_total{method="GET",route="/orders/{order_id}",status="404"}') == 1
    assert metric_value(text, 'http_requests_total{method="GET",route="unmatched",status="404"}') == 1
    assert metric_value(text, 'http_request_duration_seconds_count{method="POST",route="/orders"}') == 1
    assert metric_value(text, 'http_request_duration_seconds_bucket{method="POST",route="/orders",le="+Inf"}') == 1
    assert metric_value(text, 'http_request_duration_seconds_bucket{method="POST",route="/orders",le="10.0"}') == 1
    assert metric_value(text, 'orders_by_state{state="CREATED"}') == 1
    assert metric_value(text, 'orders_by_state{state="DELIVERED"}') == 0
    assert metric_value(text, "assignments_total") == 0


def test_histogram_bucket_counts_never_decrease_along_le(client: TestClient) -> None:
    for _ in range(5):
        client.get("/health")
    lines = [
        line
        for line in client.get("/metrics").text.splitlines()
        if line.startswith('http_request_duration_seconds_bucket{method="GET",route="/health"')
    ]
    counts = [int(line.rsplit(" ", 1)[1]) for line in lines]
    assert len(counts) == 12 and counts == sorted(counts) and counts[-1] == 5
