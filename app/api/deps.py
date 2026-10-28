"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy import Engine

from app.config import Settings
from app.errors import ApiError
from app.feed import OrderFeed
from app.metrics import AppMetrics


@dataclass(frozen=True)
class Page:
    """Offset pagination parameters."""

    limit: int
    offset: int


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


def get_metrics(request: Request) -> AppMetrics:
    return request.app.state.metrics


def get_feed(request: Request) -> OrderFeed:
    return request.app.state.feed


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_stream_cursor(
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> int:
    """Where GET /orders/stream resumes: the Last-Event-ID header a reconnecting browser sends wins over ?after=."""
    if last_event_id is not None:
        raw = last_event_id.strip()
        if not raw.isdigit():
            raise ApiError(422, "validation_error", "Last-Event-ID must be an integer event id")
        return int(raw)
    return after or 0


def get_actor(x_actor: Annotated[str | None, Header(max_length=64)] = None) -> str:
    """Who performed the action, recorded on each order event. Defaults to "api"."""
    return x_actor or "api"


def get_expected_version(if_match: Annotated[str | None, Header()] = None) -> int | None:
    """Optional optimistic-concurrency check, e.g. If-Match: 3 (quotes allowed, as with ETags)."""
    if if_match is None:
        return None
    raw = if_match.strip().strip('"')
    if not raw.isdigit():
        raise ApiError(422, "validation_error", "If-Match must be an integer version, optionally quoted")
    return int(raw)


def get_page(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page:
    return Page(limit=limit, offset=offset)


EngineDep = Annotated[Engine, Depends(get_engine)]
MetricsDep = Annotated[AppMetrics, Depends(get_metrics)]
FeedDep = Annotated[OrderFeed, Depends(get_feed)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
StreamCursorDep = Annotated[int, Depends(get_stream_cursor)]
ActorDep = Annotated[str, Depends(get_actor)]
ExpectedVersionDep = Annotated[int | None, Depends(get_expected_version)]
PageDep = Annotated[Page, Depends(get_page)]
