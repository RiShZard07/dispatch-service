"""Seed synthetic merchants, customers and couriers into a running dispatch-service.

Every name and coordinate is generated from a seeded random generator; nothing
here refers to a real person, business or place.

    python3 scripts/seed.py --base-url http://127.0.0.1:8000 --merchants 5 --customers 10 --couriers 20
"""

from __future__ import annotations

import argparse
import json
import random
import sys

import httpx

# A synthetic city centre; couriers and merchants are scattered within about 5 km of it.
CENTER_LAT, CENTER_LNG, SPREAD_DEGREES = 40.0, -75.0, 0.05


def seed(client: httpx.Client, merchants: int, customers: int, couriers: int, rng_seed: int) -> dict[str, list[str]]:
    """Create the requested entities through the HTTP API and return their ids by kind."""
    rng = random.Random(rng_seed)
    ids: dict[str, list[str]] = {"merchants": [], "customers": [], "couriers": []}

    def point() -> tuple[float, float]:
        return (
            round(CENTER_LAT + rng.uniform(-SPREAD_DEGREES, SPREAD_DEGREES), 6),
            round(CENTER_LNG + rng.uniform(-SPREAD_DEGREES, SPREAD_DEGREES), 6),
        )

    for index in range(merchants):
        lat, lng = point()
        response = client.post("/merchants", json={"name": f"Synthetic Merchant {index + 1}", "lat": lat, "lng": lng})
        response.raise_for_status()
        ids["merchants"].append(response.json()["id"])
    for _ in range(customers):
        response = client.post("/customers")
        response.raise_for_status()
        ids["customers"].append(response.json()["id"])
    for index in range(couriers):
        lat, lng = point()
        response = client.post("/couriers", json={"name": f"Synthetic Courier {index + 1}", "lat": lat, "lng": lng})
        response.raise_for_status()
        ids["couriers"].append(response.json()["id"])
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed synthetic merchants, customers and couriers.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--merchants", type=int, default=5)
    parser.add_argument("--customers", type=int, default=10)
    parser.add_argument("--couriers", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42, help="random seed, so runs are reproducible")
    args = parser.parse_args(argv)
    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        ids = seed(client, args.merchants, args.customers, args.couriers, args.seed)
    print(json.dumps(ids, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
