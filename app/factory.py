"""Application factory: wires settings, database, metrics, feed, outbox worker, middleware, routers and errors."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import couriers, orders, parties, system
from app.config import Settings
from app.db import init_schema, make_engine
from app.errors import install_error_handlers
from app.feed import OrderFeed
from app.metrics import build_metrics
from app.middleware import BodyLimitMiddleware, ObservabilityMiddleware
from app.outbox import OutboxWorker, count_by_status
from app.request_logging import configure_logging
from app.services.orders import count_by_state
from app.state import OrderState


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the outbox worker for as long as the server is up. No webhook URL means no worker."""
    worker: OutboxWorker | None = app.state.outbox_worker
    if worker is not None:
        worker.start()
    try:
        yield
    finally:
        if worker is not None:
            worker.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an app bound to its own engine, feed, worker and metrics registry; tests build one per test."""
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_schema(engine)
    metrics = build_metrics(
        lambda: count_by_state(engine), [state.value for state in OrderState], lambda: count_by_status(engine)
    )
    feed = OrderFeed()
    worker = None
    if settings.webhook_url:
        worker = OutboxWorker(
            engine,
            metrics,
            settings.webhook_url,
            timeout_seconds=settings.webhook_timeout_seconds,
            backoff_base_seconds=settings.webhook_backoff_seconds,
            poll_seconds=settings.outbox_poll_seconds,
        )
        feed.add_listener(worker.wake)  # a commit wakes the worker instead of waiting for the next poll

    app = FastAPI(
        title="dispatch-service",
        version=__version__,
        description="Order dispatch backend for a synthetic local-delivery marketplace. All data is synthetic.",
        lifespan=lifespan,
    )
    app.state.settings, app.state.engine, app.state.metrics = settings, engine, metrics
    app.state.feed, app.state.outbox_worker = feed, worker
    install_error_handlers(app)
    # The last middleware added is the outermost. Observability wraps everything so preflights
    # and 413s are logged, counted and carry a request id; CORS sits inside it and outside the
    # body limit so a 413 still gets its CORS headers and a browser can read it.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )
    app.add_middleware(ObservabilityMiddleware, metrics=metrics)
    for router in (orders.router, couriers.router, parties.router, system.router):
        app.include_router(router)
    return app
