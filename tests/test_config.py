"""Settings parsed from DISPATCH_* environment variables."""

from __future__ import annotations

import pytest

from app.config import Settings


def test_defaults_disable_cors_and_webhooks() -> None:
    settings = Settings()
    assert settings.cors_origins == () and settings.webhook_url is None
    assert settings.stream_heartbeat_seconds == 15.0 and settings.outbox_poll_seconds == 1.0


def test_cors_origins_are_split_on_commas_and_blanks_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_CORS_ORIGINS", " http://localhost:5173, https://dashboard.example ,, ")
    assert Settings.from_env().cors_origins == ("http://localhost:5173", "https://dashboard.example")
    monkeypatch.setenv("DISPATCH_CORS_ORIGINS", "")
    assert Settings.from_env().cors_origins == ()


def test_blank_webhook_url_means_no_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_WEBHOOK_URL", "   ")  # render.yaml leaves the value blank
    assert Settings.from_env().webhook_url is None
    monkeypatch.setenv("DISPATCH_WEBHOOK_URL", "http://127.0.0.1:9000/webhook")
    assert Settings.from_env().webhook_url == "http://127.0.0.1:9000/webhook"


def test_numeric_settings_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_WEBHOOK_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("DISPATCH_WEBHOOK_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("DISPATCH_OUTBOX_POLL_SECONDS", "3")
    monkeypatch.setenv("DISPATCH_STREAM_HEARTBEAT_SECONDS", "20")
    settings = Settings.from_env()
    assert (settings.webhook_timeout_seconds, settings.webhook_backoff_seconds) == (2.5, 0.5)
    assert (settings.outbox_poll_seconds, settings.stream_heartbeat_seconds) == (3.0, 20.0)
