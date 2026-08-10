"""Database connection and schema versioning.

One SQLite file at data/tracker.db (ADR-003), holding tracker and profile data
(ADR-008). WAL mode serves the multi-process reality: API, CLI, scheduler.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from harrier.demo import demo_data_dir, is_demo_mode

DB_FILENAME = "tracker.db"


def data_dir() -> Path:
    override = os.environ.get("HARRIER_DATA_DIR", "").strip()
    if override:
        return Path(override)
    # A demo run writes to a temp directory, never into the clone (spec 021).
    return demo_data_dir() if is_demo_mode() else Path("data")


def default_db_path() -> Path:
    return data_dir() / DB_FILENAME


def connect(db_path: Path | None = None, *, same_thread: bool = True) -> sqlite3.Connection:
    """Open the database, creating the directory and schema if needed.

    same_thread=False relaxes sqlite3's own check that a connection is used
    from the thread that made it. Only the API needs it, and only because
    FastAPI runs a sync dependency and the sync endpoint it feeds on
    different threadpool threads: the connection is handed between them, but
    never used by two at once, since each request opens and closes its own
    (harrier_api/deps.py, proven by test_api_jobs.py::
    test_concurrent_requests_do_not_trip_the_sqlite_thread_check).
    """
    path = db_path if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    version = row["v"] if row is not None else None
    return int(version) if version is not None else 0


def _apply_schema(conn: sqlite3.Connection) -> None:
    from harrier.tracker.schema import MIGRATIONS

    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    current = schema_version(conn)
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        with conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
