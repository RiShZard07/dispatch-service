"""Structured JSON logging plus the context variable that carries the request id."""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

ROOT_LOGGER = "dispatch"

# Set by the observability middleware for the duration of each request. Starlette
# copies the context into the worker thread that runs a sync endpoint, so service
# code logging during a request picks the id up automatically.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Render each log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    """Send every `dispatch.*` logger through one JSON handler on stderr. Safe to call twice."""
    logger = logging.getLogger(ROOT_LOGGER)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a child of the service logger, e.g. dispatch.http."""
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")
