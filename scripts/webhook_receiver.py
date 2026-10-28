"""A webhook receiver that prints every delivery from the outbox worker. Synthetic data only.

    python3 scripts/webhook_receiver.py --port 9000
    DISPATCH_WEBHOOK_URL=http://127.0.0.1:9000/webhook uvicorn app.main:app --port 8000 --no-access-log

It answers 200 to every POST and prints one line per delivery. Pass
--fail-first N to answer the first N deliveries with 500 and watch the worker
retry them with growing delays. Deliveries are at-least-once, so the receiver
keeps the ids it has seen and flags a repeat instead of acting on it twice,
which is the idempotent-consumer discipline any real receiver needs.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Receiver(ThreadingHTTPServer):
    """Keeps every delivery it has received, in order, and the ids it has already seen."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], fail_first: int = 0, quiet: bool = False) -> None:
        super().__init__(address, Handler)
        self.fail_first, self.quiet = fail_first, quiet
        self.deliveries: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []  # the request headers of each delivery, same order
        self.seen: set[int] = set()
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/webhook"

    def record(self, delivery: dict[str, Any], headers: dict[str, str]) -> tuple[int, str]:
        """Store the delivery and decide the response. Returns (status, one-line summary)."""
        with self.lock:
            self.deliveries.append(delivery)
            self.headers.append(headers)
            ordinal = len(self.deliveries)
            duplicate = delivery.get("id") in self.seen
            fail = ordinal <= self.fail_first
            if not fail and not duplicate:
                self.seen.add(delivery.get("id"))
        order = delivery.get("data", {}).get("order", {})
        summary = (
            f"delivery id={delivery.get('id')} type={delivery.get('type')} attempt={delivery.get('attempt')} "
            f"order={order.get('id')} state={order.get('state')}"
        )
        if fail:
            return 500, summary + " -> answering 500 on purpose"
        if duplicate:
            return 200, summary + " -> duplicate, already processed, ignoring"
        return 200, summary


class Handler(BaseHTTPRequestHandler):
    server: Receiver

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            delivery = json.loads(raw)
        except ValueError:
            self.send_response(400)
            self.end_headers()
            return
        status, summary = self.server.record(delivery, {key.lower(): value for key, value in self.headers.items()})
        if not self.server.quiet:
            print(summary, flush=True)
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format: str, *_args: Any) -> None:
        return  # the summary line above replaces the default access log


def serve(receiver: Receiver) -> threading.Thread:
    """Run the receiver on a background thread; the caller shuts it down with receiver.shutdown()."""
    thread = threading.Thread(target=receiver.serve_forever, name="webhook-receiver", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print webhook deliveries from the dispatch-service outbox worker.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--fail-first", type=int, default=0, help="answer the first N deliveries with 500")
    args = parser.parse_args(argv)
    receiver = Receiver((args.host, args.port), fail_first=args.fail_first)
    print(f"listening on {receiver.url} (set DISPATCH_WEBHOOK_URL to this); Ctrl-C to stop", flush=True)
    try:
        receiver.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        receiver.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
