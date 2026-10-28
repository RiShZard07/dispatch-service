"""In-process publish/subscribe for order events, behind GET /orders/stream.

A service function publishes each event after its write transaction has
committed, from the worker thread that ran the request. A subscriber is an
asyncio queue owned by the event loop that serves one stream; delivery crosses
the thread boundary with loop.call_soon_threadsafe. Listeners (the outbox
worker's wake-up call) are plain callables invoked on the publishing thread.

The feed is a doorbell, not the source of truth. Publishing happens after the
write lock is released, so two events can arrive at a queue out of order, a
slow subscriber's queue can fill up and drop one, and another process writing
to the same database never rings this process's bell at all. The stream
endpoint (app/sse.py) therefore reads order_events to catch up on connect,
whenever it sees a gap in ids, and at every heartbeat. A missed or misordered
publish costs one indexed query, never an event.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import count

from app.schemas import EventOut

QUEUE_SIZE = 256


@dataclass
class Subscription:
    """One stream's queue. Use as a context manager so the queue is removed when the stream ends."""

    feed: OrderFeed
    key: int
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[EventOut] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_: object) -> None:
        self.feed.unsubscribe(self.key)

    def deliver(self, event: EventOut) -> None:
        """Hand an event to the owning loop. Called from any thread; a full queue drops the event."""
        self.loop.call_soon_threadsafe(self._put, event)

    def _put(self, event: EventOut) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # the stream will notice the gap (or hit a heartbeat) and read the table instead

    async def get(self, timeout: float) -> EventOut | None:
        """Next event, or None once timeout seconds pass without one."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None


class OrderFeed:
    """Fan-out of committed order events to stream subscribers and wake-up listeners."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[int, Subscription] = {}
        self._listeners: list[Callable[[EventOut], None]] = []
        self._keys = count()

    def subscribe(self) -> Subscription:
        """Register the calling event loop as a subscriber. Must be called from within a running loop."""
        subscription = Subscription(self, next(self._keys), asyncio.get_running_loop())
        with self._lock:
            self._subscriptions[subscription.key] = subscription
        return subscription

    def unsubscribe(self, key: int) -> None:
        with self._lock:
            self._subscriptions.pop(key, None)

    def add_listener(self, listener: Callable[[EventOut], None]) -> None:
        """Register a callable invoked synchronously on every publish, on the publishing thread."""
        with self._lock:
            self._listeners.append(listener)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)

    def publish(self, event: EventOut) -> None:
        """Deliver a committed event to every subscriber and listener. Safe from any thread."""
        with self._lock:
            subscriptions = list(self._subscriptions.values())
            listeners = list(self._listeners)
        for subscription in subscriptions:
            try:
                subscription.deliver(event)
            except RuntimeError:
                # The owning event loop is closed (its server shut down); the subscription is dead.
                self.unsubscribe(subscription.key)
        for listener in listeners:
            listener(event)
