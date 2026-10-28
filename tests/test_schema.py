"""schema.sql and app/models.py must describe the same tables; the uvicorn entrypoint must build."""

from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import MetaData

from app.config import Settings
from app.db import init_schema
from app.factory import create_app
from app.models import metadata


def test_models_mirror_schema_sql(app: FastAPI) -> None:
    live = MetaData()
    live.reflect(app.state.engine)
    assert set(live.tables) == set(metadata.tables)
    for name, declared in metadata.tables.items():
        reflected = live.tables[name]
        assert [c.name for c in reflected.columns] == [c.name for c in declared.columns], name
        assert {c.name for c in reflected.primary_key} == {c.name for c in declared.primary_key}, name
        for column in declared.columns:
            assert reflected.columns[column.name].nullable == column.nullable, f"{name}.{column.name}"


def test_init_schema_is_idempotent(app: FastAPI) -> None:
    init_schema(app.state.engine)
    init_schema(app.state.engine)
    live = MetaData()
    live.reflect(app.state.engine)
    assert set(live.tables) == set(metadata.tables)


LEGACY_ORDER_EVENTS = """
CREATE TABLE order_events (
    order_id   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (order_id, seq)
)
"""


def test_a_database_from_before_the_event_id_column_is_refused_clearly(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS keeps an old table as it is, so the stale shape must be caught on start."""
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as legacy:
        legacy.execute(LEGACY_ORDER_EVENTS)
    with pytest.raises(RuntimeError, match="predates the order_events.id column"):
        create_app(Settings(database_url=f"sqlite:///{path}"))


def test_uvicorn_entrypoint_builds_an_app_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPATCH_DATABASE_URL", f"sqlite:///{tmp_path}/entrypoint.sqlite")
    monkeypatch.setenv("DISPATCH_MAX_BODY_BYTES", "1024")
    main = importlib.import_module("app.main")
    main = importlib.reload(main)
    assert isinstance(main.app, FastAPI)
    assert main.app.state.settings.max_body_bytes == 1024
    assert (tmp_path / "entrypoint.sqlite").exists()
    main.app.state.engine.dispose()
