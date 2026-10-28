"""Runtime settings, read from DISPATCH_* environment variables with safe defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _origins(raw: str) -> tuple[str, ...]:
    """Split a comma-separated origin list, dropping blanks and surrounding whitespace."""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    """Configuration for one application instance."""

    database_url: str = "sqlite:///./dispatch.sqlite"
    max_body_bytes: int = 64 * 1024
    log_level: str = "INFO"
    # Browser origins allowed to call the API. Empty means the CORS middleware is not installed.
    cors_origins: tuple[str, ...] = ()
    # Where the outbox worker POSTs order events. None (or blank) means nothing is delivered.
    webhook_url: str | None = None
    webhook_timeout_seconds: float = 5.0
    # First retry waits this long; each later retry doubles it (see app/outbox.py).
    webhook_backoff_seconds: float = 1.0
    # How long the outbox worker sleeps between looks at the table when nothing wakes it.
    outbox_poll_seconds: float = 1.0
    # Seconds of silence on GET /orders/stream before a heartbeat comment is sent.
    stream_heartbeat_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the environment, falling back to the defaults above."""
        env = os.environ.get
        return cls(
            database_url=env("DISPATCH_DATABASE_URL", cls.database_url),
            max_body_bytes=int(env("DISPATCH_MAX_BODY_BYTES", cls.max_body_bytes)),
            log_level=env("DISPATCH_LOG_LEVEL", cls.log_level),
            cors_origins=_origins(env("DISPATCH_CORS_ORIGINS", "")),
            webhook_url=env("DISPATCH_WEBHOOK_URL", "").strip() or None,
            webhook_timeout_seconds=float(env("DISPATCH_WEBHOOK_TIMEOUT_SECONDS", cls.webhook_timeout_seconds)),
            webhook_backoff_seconds=float(env("DISPATCH_WEBHOOK_BACKOFF_SECONDS", cls.webhook_backoff_seconds)),
            outbox_poll_seconds=float(env("DISPATCH_OUTBOX_POLL_SECONDS", cls.outbox_poll_seconds)),
            stream_heartbeat_seconds=float(env("DISPATCH_STREAM_HEARTBEAT_SECONDS", cls.stream_heartbeat_seconds)),
        )
