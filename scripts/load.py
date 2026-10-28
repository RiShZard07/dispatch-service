"""Local load generator for dispatch-service.

Drives N orders through create -> accept -> assign -> pickup -> deliver against
a running server, with a bounded number of lifecycles in flight, and prints
throughput plus p50/p95/p99 latencies per step. The numbers are single-process,
same-machine measurements of a prototype in front of SQLite. They describe the
machine they ran on, not a production capacity.

    python3 scripts/load.py --base-url http://127.0.0.1:8000 --orders 200 --concurrency 10
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import httpx

STEPS = ("create", "accept", "assign", "pickup", "deliver")
ASSIGN_ATTEMPTS = 50
RETRY_DELAY_SECONDS = 0.02
MERCHANT = {"name": "Synthetic Load Merchant", "lat": 40.0, "lng": -75.0}
ITEMS = [{"sku": "SYN-LOAD", "quantity": 1, "unit_price_cents": 500}]


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (pct in 0..100); 0.0 for an empty sequence."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


@dataclass
class LoadReport:
    """Everything the run measured. Latencies are in seconds."""

    orders: int
    concurrency: int
    completed: int = 0
    elapsed_seconds: float = 0.0
    step_latencies: dict[str, list[float]] = field(default_factory=lambda: {step: [] for step in STEPS})
    lifecycle_latencies: list[float] = field(default_factory=list)

    @property
    def errors(self) -> int:
        return self.orders - self.completed

    @property
    def requests(self) -> int:
        return sum(len(samples) for samples in self.step_latencies.values())

    def lines(self) -> list[str]:
        """Human-readable summary."""
        elapsed = max(self.elapsed_seconds, 1e-9)
        out = [
            "dispatch-service load run (synthetic data; local single-process numbers, not a capacity claim)",
            f"orders: {self.orders} requested, {self.completed} completed, {self.errors} failed; "
            f"concurrency {self.concurrency}; elapsed {self.elapsed_seconds:.2f} s",
            f"throughput: {self.completed / elapsed:.1f} orders/s, {self.requests / elapsed:.1f} requests/s",
            f"{'latency (ms)':<14}{'p50':>9}{'p95':>9}{'p99':>9}{'max':>9}{'n':>7}",
        ]
        rows = [*self.step_latencies.items(), ("lifecycle", self.lifecycle_latencies)]
        for name, samples in rows:
            p50, p95, p99 = (percentile(samples, p) * 1000 for p in (50, 95, 99))
            peak = max(samples, default=0.0) * 1000
            out.append(f"  {name:<12}{p50:>9.2f}{p95:>9.2f}{p99:>9.2f}{peak:>9.2f}{len(samples):>7}")
        return out


async def _call(
    client: httpx.AsyncClient,
    report: LoadReport,
    step: str,
    url: str,
    json: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST and record the wall-clock latency under the given step name."""
    started = time.perf_counter()
    response = await client.post(url, json=json, headers=headers)
    report.step_latencies[step].append(time.perf_counter() - started)
    return response


async def _lifecycle(
    client: httpx.AsyncClient, report: LoadReport, merchant_id: str, customer_id: str, key: str
) -> bool:
    """Run one order from creation to delivery. Returns False at the first unexpected response."""
    started = time.perf_counter()
    body = {"merchant_id": merchant_id, "customer_id": customer_id, "items": ITEMS}
    response = await _call(client, report, "create", "/orders", json=body, headers={"Idempotency-Key": key})
    if response.status_code != 201:
        return False
    order_id = response.json()["id"]
    if (await _call(client, report, "accept", f"/orders/{order_id}/accept")).status_code != 200:
        return False
    for _ in range(ASSIGN_ATTEMPTS):
        response = await _call(client, report, "assign", f"/orders/{order_id}/assign")
        if response.status_code == 200:
            break
        if response.status_code != 409 or response.json()["error"]["code"] != "no_courier_available":
            return False
        await asyncio.sleep(RETRY_DELAY_SECONDS)
    else:
        return False
    for step in ("pickup", "deliver"):
        if (await _call(client, report, step, f"/orders/{order_id}/{step}")).status_code != 200:
            return False
    report.lifecycle_latencies.append(time.perf_counter() - started)
    return True


async def run_load(client: httpx.AsyncClient, orders: int, concurrency: int) -> LoadReport:
    """Create a merchant, a customer and one courier per concurrent lifecycle, then run the orders."""
    merchant = (await client.post("/merchants", json=MERCHANT)).raise_for_status().json()
    customer = (await client.post("/customers")).raise_for_status().json()
    for index in range(concurrency):
        courier = {"name": f"Synthetic Load Courier {index + 1}", "lat": 40.0 + index * 0.001, "lng": -75.0}
        (await client.post("/couriers", json=courier)).raise_for_status()

    report = LoadReport(orders=orders, concurrency=concurrency)
    semaphore = asyncio.Semaphore(concurrency)
    run_id = uuid4().hex[:8]

    async def worker(index: int) -> bool:
        async with semaphore:
            return await _lifecycle(client, report, merchant["id"], customer["id"], f"load-{run_id}-{index}")

    started = time.perf_counter()
    outcomes = await asyncio.gather(*(worker(index) for index in range(orders)))
    report.elapsed_seconds = time.perf_counter() - started
    report.completed = sum(outcomes)
    return report


async def _run(base_url: str, orders: int, concurrency: int) -> LoadReport:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        return await run_load(client, orders, concurrency)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive synthetic orders through their full lifecycle.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args(argv)
    report = asyncio.run(_run(args.base_url, args.orders, args.concurrency))
    print("\n".join(report.lines()))
    return 0 if report.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
