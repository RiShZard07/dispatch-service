"""Error types and the single JSON error shape used by every failure response.

Every error body looks like {"error": {"code": "...", "message": "..."}} so
clients can switch on the code without parsing prose.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.state import InvalidTransition

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}


class ApiError(Exception):
    """An error with a fixed HTTP status and a machine-readable code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class NotFound(ApiError):
    """404 for a missing resource named in the URL."""

    def __init__(self, kind: str, ident: str) -> None:
        super().__init__(404, "not_found", f"{kind} {ident!r} not found")


class Conflict(ApiError):
    """409 for state, idempotency and version conflicts."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(409, code, message)


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """Build the standard error body."""
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def install_error_handlers(app: FastAPI) -> None:
    """Map every exception type the app can raise onto the standard error body."""

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(InvalidTransition)
    async def on_invalid_transition(_: Request, exc: InvalidTransition) -> JSONResponse:
        return error_response(409, "invalid_transition", str(exc))

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors())
        return error_response(422, "validation_error", details)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    # Anything else is an unexpected server error; ObservabilityMiddleware turns it into a 500
    # with the same envelope, because it also owns the request id that the response must carry.
