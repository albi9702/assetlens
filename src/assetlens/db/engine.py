"""Engine creation and SQLite tuning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event

from assetlens.db.models import Base


def _ensure_sqlite_directory(url: str) -> None:
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return
    path = Path(url.removeprefix(prefix))
    if path.name in {"", ":memory:"}:
        return
    path.parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str, *, echo: bool = False) -> Engine:
    """Create an SQLAlchemy engine for SQLite or PostgreSQL.

    SQLite gets WAL mode, foreign keys and a busy timeout. PostgreSQL keeps a
    pre-pinged pool with a short recycle time so Neon can scale to zero without
    leaving stale connections behind.
    """

    _ensure_sqlite_directory(url)
    is_sqlite = url.startswith("sqlite")

    engine = create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=-1 if is_sqlite else 300,
        connect_args={"check_same_thread": False, "timeout": 10} if is_sqlite else {},
    )

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def create_all(engine: Engine) -> None:
    """Create every table. Used by tests; production uses Alembic."""

    Base.metadata.create_all(engine)
