"""Health and metrics endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import EngineDep, MetricsDep
from app.db import read_connection
from app.errors import error_response
from app.metrics import CONTENT_TYPE
from app.schemas import HealthOut

router = APIRouter(tags=["system"])


@router.get("/health", response_model=HealthOut)
def health(engine: EngineDep) -> HealthOut | JSONResponse:
    """200 when the database answers a trivial query, 503 otherwise."""
    try:
        with read_connection(engine) as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return error_response(503, "service_unavailable", "database is unreachable")
    return HealthOut(status="ok", database="ok")


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(app_metrics: MetricsDep) -> PlainTextResponse:
    """Prometheus text exposition, rendered by the in-process registry."""
    return PlainTextResponse(app_metrics.registry.render(), media_type=CONTENT_TYPE)
