"""Database connection and schema versioning.

One SQLite file at data/tracker.db (ADR-003), holding tracker and profile data
(ADR-008). WAL mode serves the multi-process reality: API, CLI, scheduler.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_FILENAME = "tracker.db"


def data_dir() -> Path:
    return Path(os.environ.get("HARRIER_DATA_DIR", "data"))


def default_db_path() -> Path:
    return data_dir() / DB_FILENAME


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the database, creating the directory and schema if needed."""
    path = db_path if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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
