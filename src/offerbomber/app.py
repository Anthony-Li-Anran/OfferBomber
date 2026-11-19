"""Application context: config + database engine/session factory."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, load_settings
from .storage.base import make_engine, make_session_factory
from .storage.fts import ensure_fts5


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = make_engine(settings.storage.db_path)
        self.sessions: sessionmaker[Session] = make_session_factory(self.engine)
        self.fts_available = ensure_fts5(self.engine)

    def session(self) -> Session:
        return self.sessions()

    @classmethod
    def from_config(cls, path: str | Path) -> AppContext:
        return cls(load_settings(path))

    def close(self) -> None:
        self.engine.dispose()


def resolve_db_path(settings: Settings) -> str:
    return str(settings.storage.db_path)
