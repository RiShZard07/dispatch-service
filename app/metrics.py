"""A small in-process metrics registry that renders the Prometheus text format.

Written by hand instead of depending on prometheus_client so the mechanics are
visible: counters and histograms are dictionaries keyed by label values,
guarded by a lock, and rendered on scrape. It covers exactly what this service
needs and nothing more (no multiprocess mode, no summaries, no push gateway).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass

Labels = tuple[str, ...]

DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def escape_label(value: str) -> str:
    """Escape a label value the way the exposition format requires."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_value(value: float) -> str:
    """Render whole numbers without a trailing .0 and everything else at full precision."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _render_labels(names: Sequence[str], values: Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = [f'{name}="{escape_label(value)}"' for name, value in zip(names, values, strict=True)]
    pairs += [f'{name}="{value}"' for name, value in extra]
    return "{" + ",".join(pairs) + "}" if pairs else ""


class Metric:
    """Name, help text and label handling shared by every metric kind."""

    kind = "untyped"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        self.name, self.help_text, self.labelnames = name, help_text, tuple(labelnames)
        self._lock = threading.Lock()

    def _check(self, values: Labels) -> Labels:
        if len(values) != len(self.labelnames):
            raise ValueError(f"{self.name} expects labels {self.labelnames}, got {values}")
        return values

    def samples(self) -> Iterator[str]:
        """Yield the exposition lines for this metric."""
        raise NotImplementedError


class Counter(Metric):
    """A monotonically increasing value per label set."""

    kind = "counter"

    def __init__(self, name: str, help_text: str, labelnames: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labelnames)
        # An unlabeled counter is a single series that exists from the start, so it renders as 0
        # before the first increment; labeled series appear when first used.
        self._values: dict[Labels, float] = {} if labelnames else {(): 0.0}

    def inc(self, *labels: str, amount: float = 1.0) -> None:
        key = self._check(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *labels: str) -> float:
        with self._lock:
            return self._values.get(self._check(labels), 0.0)

    def samples(self) -> Iterator[str]:
        with self._lock:
            items = sorted(self._values.items())
        for key, value in items:
            yield f"{self.name}{_render_labels(self.labelnames, key)} {format_value(value)}"


class Histogram(Metric):
    """Cumulative buckets, a sum and a count per label set."""

    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[Labels, list[int]] = {}
        self._sums: dict[Labels, float] = {}

    def observe(self, value: float, *labels: str) -> None:
        key = self._check(labels)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * (len(self.buckets) + 1))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            counts[-1] += 1  # the +Inf bucket doubles as the total count
            self._sums[key] = self._sums.get(key, 0.0) + value

    def samples(self) -> Iterator[str]:
        with self._lock:
            snapshot = [(key, list(counts), self._sums[key]) for key, counts in sorted(self._counts.items())]
        for key, counts, total in snapshot:
            for bound, count in zip(self.buckets, counts[:-1], strict=True):
                yield f"{self.name}_bucket{_render_labels(self.labelnames, key, (('le', repr(bound)),))} {count}"
            yield f"{self.name}_bucket{_render_labels(self.labelnames, key, (('le', '+Inf'),))} {counts[-1]}"
            yield f"{self.name}_sum{_render_labels(self.labelnames, key)} {format_value(total)}"
            yield f"{self.name}_count{_render_labels(self.labelnames, key)} {counts[-1]}"


class GaugeFunc(Metric):
    """A gauge whose values come from a callback at scrape time."""

    kind = "gauge"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str],
        collect: Callable[[], Iterable[tuple[Labels, float]]],
    ) -> None:
        super().__init__(name, help_text, labelnames)
        self._collect = collect

    def samples(self) -> Iterator[str]:
        for key, value in self._collect():
            yield f"{self.name}{_render_labels(self.labelnames, self._check(key))} {format_value(value)}"


class Registry:
    """Holds metrics and renders them in registration order."""

    def __init__(self) -> None:
        self._metrics: list[Metric] = []

    def register[M: Metric](self, metric: M) -> M:
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: list[str] = []
        for metric in self._metrics:
            lines.append(f"# HELP {metric.name} {metric.help_text}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            lines.extend(metric.samples())
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class AppMetrics:
    """The metrics this service exposes, created once per application instance."""

    registry: Registry
    http_requests: Counter
    http_duration: Histogram
    assignments: Counter
    assignment_conflicts: Counter
    webhook_deliveries: Counter
    webhook_failures: Counter


OUTBOX_STATUSES = ("pending", "delivered", "exhausted")


def build_metrics(
    orders_by_state: Callable[[], dict[str, int]],
    states: Sequence[str],
    outbox_by_status: Callable[[], dict[str, int]] = lambda: {},
) -> AppMetrics:
    """Create the registry. The two callbacks are queried on every scrape, one sample per known label."""
    registry = Registry()

    def collect_states() -> list[tuple[Labels, float]]:
        counts = orders_by_state()
        return [((state,), float(counts.get(state, 0))) for state in states]

    def collect_outbox() -> list[tuple[Labels, float]]:
        counts = outbox_by_status()
        return [((status,), float(counts.get(status, 0))) for status in OUTBOX_STATUSES]

    http_requests = registry.register(
        Counter(
            "http_requests_total",
            "HTTP requests by method, route template and status code.",
            ("method", "route", "status"),
        )
    )
    http_duration = registry.register(
        Histogram(
            "http_request_duration_seconds",
            "HTTP request latency in seconds by method and route template.",
            ("method", "route"),
        )
    )
    registry.register(
        GaugeFunc("orders_by_state", "Number of orders currently in each state.", ("state",), collect_states)
    )
    assignments = registry.register(Counter("assignments_total", "Orders successfully assigned to a courier."))
    conflicts = registry.register(
        Counter("assignment_conflicts_total", "Assignment attempts that found no available courier or lost a race.")
    )
    registry.register(
        GaugeFunc("outbox_rows", "Outbox rows by status: pending, delivered or exhausted.", ("status",), collect_outbox)
    )
    deliveries = registry.register(Counter("webhook_deliveries_total", "Outbox rows accepted by the webhook receiver."))
    failures = registry.register(
        Counter("webhook_failures_total", "Webhook delivery attempts that failed (each retry counts once).")
    )
    return AppMetrics(registry, http_requests, http_duration, assignments, conflicts, deliveries, failures)
