"""Shared SQLite connections and the exclusive restore access gate."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator


class _DatabaseAccessGate:
    """Allow concurrent requests while giving restore a truly exclusive window."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._writer_thread: int | None = None
        self._writer_depth = 0
        self._waiting_writers = 0

    @contextmanager
    def read(self) -> Generator[None, None, None]:
        thread_id = threading.get_ident()
        with self._condition:
            while (
                self._writer_thread not in (None, thread_id)
                or (self._waiting_writers > 0 and self._writer_thread != thread_id)
            ):
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self) -> Generator[None, None, None]:
        thread_id = threading.get_ident()
        with self._condition:
            if self._writer_thread == thread_id:
                self._writer_depth += 1
            else:
                self._waiting_writers += 1
                try:
                    while self._writer_thread is not None or self._readers > 0:
                        self._condition.wait()
                    self._writer_thread = thread_id
                    self._writer_depth = 1
                finally:
                    self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    self._writer_thread = None
                    self._condition.notify_all()


_database_access_gate = _DatabaseAccessGate()


@contextmanager
def exclusive_database_access() -> Generator[None, None, None]:
    """Block new DB users and wait for live request connections to close."""
    with _database_access_gate.write():
        yield


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    columns = [col[0] for col in cursor.description]
    return dict(zip(columns, row))


@contextmanager
def read_existing_databases(paths):
    """One restore lease for a passive multi-store view; no nested read leases."""
    with _database_access_gate.read():
        connections=[]
        try:
            for path in paths:
                require_storage_ready(path)
                path=Path(path)
                connection=None
                if path.is_file():
                    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                    connections.append(connection)
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('BEGIN')
                else:
                    connections.append(None)
            yield tuple(connections)
        finally:
            for connection in connections:
                if connection is not None:connection.close()


@contextmanager
def connect_database(path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    with _database_access_gate.read():
        require_storage_ready(path)
        conn = None
        try:
            conn = sqlite3.connect(path, check_same_thread=False)
            conn.row_factory = _row_factory
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        except sqlite3.Error as error:
            from services.operation_diagnostics import failure
            failure("storage", "database.failed", reason="Database operation failed",
                    details={"error_type": type(error).__name__,
                             "sqlite_errorcode": getattr(error, "sqlite_errorcode", None)}, exception=error)
            raise
        finally:
            if conn is not None:
                conn.close()


def require_storage_ready(path):
    import sys
    config=sys.modules.get('config')
    # Explicit pure index/cache callers must not load an application profile.
    # The running app has already selected config before it opens active stores.
    if config is None:return
    active={Path(value).absolute() for value in (config.USER_DB_PATH,config.INDEX_DB_PATH,config.JOBS_DB_PATH,config.FILES_DB_PATH)}
    if Path(path).absolute() in active and any((config.SUITE_HOME/'state'/name).exists()
            for name in ('storage-install-pending.json','storage-restart-required.json')):
        raise RuntimeError('Storage recovery requires a restart before continuing.')
