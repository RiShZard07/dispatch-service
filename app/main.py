"""Uvicorn entrypoint: `uvicorn app.main:app`. Settings come from DISPATCH_* environment variables."""

from app.config import Settings
from app.factory import create_app

app = create_app(Settings.from_env())
