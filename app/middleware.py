"""ASGI middleware: request ids, one JSON log line per request, HTTP metrics and a body size limit."""

from __future__ import annotations

import re
import time
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import error_response
from app.metrics import AppMetrics
from app.request_logging import get_logger, request_id_var

log = get_logger("http")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def route_template(scope: Scope) -> str:
    """The matched route's path template (e.g. /orders/{order_id}), which keeps label cardinality bounded."""
    route = scope.get("route")
    return route.path if route is not None else "unmatched"


class ObservabilityMiddleware:
    """Tag each request with X-Request-ID, log it as JSON and record HTTP metrics."""

    def __init__(self, app: ASGIApp, metrics: AppMetrics) -> None:
        self.app, self.metrics = app, metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status, response_started = message["status"], True
                MutableHeaders(raw=message["headers"]).append("X-Request-ID", request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # Nothing below us handled it: log with the traceback, answer with the standard
            # envelope (still carrying the request id), then re-raise so the server sees it too.
            log.error("unhandled error", exc_info=exc, extra={"fields": {"request_id": request_id}})
            if not response_started:
                await error_response(500, "internal_error", "unexpected server error")(scope, receive, send_with_id)
            raise
        finally:
            elapsed = time.perf_counter() - started
            method, route = scope["method"], route_template(scope)
            self.metrics.http_requests.inc(method, route, str(status))
            self.metrics.http_duration.observe(elapsed, method, route)
            log.info(
                "request",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": method,
                        "path": scope["path"],
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                    }
                },
            )
            request_id_var.reset(token)


class BodyLimitMiddleware:
    """Reject request bodies above max_bytes with 413, whether or not Content-Length was declared."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            response = error_response(413, "payload_too_large", self._message())
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise HTTPException(status_code=413, detail=self._message())
            return message

        await self.app(scope, limited_receive, send)

    def _message(self) -> str:
        return f"request body exceeds the limit of {self.max_bytes} bytes"
