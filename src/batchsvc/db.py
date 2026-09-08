"""SQLite engine/session setup.

Uses SQLAlchemy with SQLite in WAL mode. WAL lets readers (status polling,
listing) proceed concurrently with writers, and gives us crash-safe durable
commits without a separate database server -- the role HSQLDB would have
played in this deployment.

A single process is assumed to own writes (the API process today; the
dispatcher joins it in M3). Budget-affecting operations additionally take
an in-process lock (see ledger.py) so business-level invariants hold even
though SQLite's own locking is coarser than a single row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from batchsvc.config import Settings
from batchsvc.models import Base


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        settings.blob_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
        _install_pragmas(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.SessionLocal()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.session()
        try:
            yield session
        finally:
            session.close()


def _install_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # SQLite's default busy_timeout is 0: a writer that can't
        # immediately acquire the write lock fails instantly with
        # "database is locked" rather than waiting. WAL allows concurrent
        # readers, but writers (admin API calls, budget grants, batch
        # submission, the dispatcher settling tasks) still serialize --
        # under real concurrent load that surfaces as sporadic 500s
        # without this (found via scripts/load_test.py). 20s comfortably
        # covers a queue of writers backing up under a burst without
        # masking a genuinely stuck one; this can be generous because
        # every recurring background write (dispatcher, retention job)
        # runs via asyncio.to_thread rather than directly on the event
        # loop, so a long wait here costs one worker thread, never stalls
        # the whole server the way it would if it blocked the loop.
        cursor.execute("PRAGMA busy_timeout=20000")
        cursor.close()


def build_database(settings: Settings) -> Database:
    db = Database(settings)
    db.create_all()
    return db


def in_memory_database(path: Path) -> Database:
    """Helper for tests: a file-backed (not :memory:, WAL needs a real file) db."""
    settings = Settings(
        database_path=path / "test.db",
        blob_dir=path / "blobs",
        admin_token="test-admin-token",
    )
    return build_database(settings)
