"""A migration applies whole or not at all (spec 090).

The runner used to apply each migration under `with conn:`, which the
`sqlite3` module opens only before a data statement. A `CREATE TABLE` or
`ALTER TABLE` was on disk the moment it returned, so a migration that failed
on its third statement left its first two behind, with no `schema_version`
row to say so, and the next open failed on "table already exists". Every
database here is built under `tmp_path`; none is the operator's (spec 060).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from harrier import db
from harrier.db import BUSY_TIMEOUT_MS, connect
from harrier.tracker import schema

PROBE_TABLE = "CREATE TABLE probe (id INTEGER PRIMARY KEY)"
NOT_SQL = "THIS IS NOT SQL"
# What a connection hands out before anything touches its transaction mode.
MODULE_DEFAULT_ISOLATION = sqlite3.connect(":memory:").isolation_level


def versions(path: Path) -> list[int]:
    raw = sqlite3.connect(path)
    try:
        rows = raw.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
        return [int(row[0]) for row in rows]
    finally:
        raw.close()


def tables(path: Path) -> set[str]:
    raw = sqlite3.connect(path)
    try:
        rows = raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {str(row[0]) for row in rows}
    finally:
        raw.close()


def schema_text(path: Path) -> list[str]:
    raw = sqlite3.connect(path)
    try:
        rows = raw.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        raw.close()


REAL_VERSIONS = [version for version, _ in schema.MIGRATIONS]
REAL_MIGRATIONS = list(schema.MIGRATIONS)


def with_a_broken_next_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real migrations, then one whose second statement cannot run."""
    broken = [*REAL_MIGRATIONS, (REAL_VERSIONS[-1] + 1, [PROBE_TABLE, NOT_SQL])]
    monkeypatch.setattr(schema, "MIGRATIONS", broken)


def test_a_failed_migration_leaves_no_partial_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fails on the runner at cd5665d: the probe table survived the error."""
    with_a_broken_next_migration(monkeypatch)
    path = tmp_path / "tracker.db"
    with pytest.raises(sqlite3.OperationalError):
        connect(path)
    assert "probe" not in tables(path)
    assert versions(path) == REAL_VERSIONS


def test_a_failed_migration_closes_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_a_broken_next_migration(monkeypatch)
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(database: Path, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(database, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", recording_connect)
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "tracker.db")
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


CHILD = textwrap.dedent(
    """
    import sqlite3, sys, time
    from pathlib import Path
    from harrier import db

    class Slow(sqlite3.Connection):
        # Slow every CREATE TABLE so the other process arrives while this
        # one holds the lock, and pause after every COMMIT so the other
        # process gets the lock between migrations. Each then reads a
        # version the other has moved on from, and has to find that work
        # under the lock rather than redo it.
        def execute(self, sql, *args):
            if sql.lstrip().upper().startswith("CREATE TABLE "):
                time.sleep(0.25)
            cursor = super().execute(sql, *args)
            if sql.strip().upper() == "COMMIT":
                time.sleep(0.3)
            return cursor

    real = sqlite3.connect
    sqlite3.connect = lambda path, *a, **k: real(path, *a, factory=Slow, **k)
    db.connect(Path(sys.argv[1])).close()
    print("ok")
    """
)


def test_two_first_opens_apply_each_migration_once(tmp_path: Path) -> None:
    """Fails on the runner at cd5665d: the second process hit a table the
    first had already created, because nothing was locked between reading
    the version and writing the schema."""
    path = tmp_path / "shared" / "tracker.db"
    path.parent.mkdir()
    children = [
        subprocess.Popen(
            [sys.executable, "-c", CHILD, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outcomes = [child.communicate(timeout=60) for child in children]
    for child, (out, err) in zip(children, outcomes, strict=True):
        assert child.returncode == 0, err
        assert out.strip() == "ok"

    assert versions(path) == REAL_VERSIONS

    alone = tmp_path / "alone" / "tracker.db"
    alone.parent.mkdir()
    connect(alone).close()
    assert schema_text(path) == schema_text(alone)


def test_connect_returns_a_connection_in_its_usual_transaction_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tracker.db"
    fresh = connect(path)
    try:
        assert fresh.isolation_level == MODULE_DEFAULT_ISOLATION
        with fresh:
            fresh.execute("INSERT INTO schema_version (version) VALUES (999)")
    finally:
        fresh.close()
    assert 999 in versions(path)

    # After a failed migration too: the next open, with the broken
    # migration gone again, hands out an ordinary connection.
    with_a_broken_next_migration(monkeypatch)
    with pytest.raises(sqlite3.OperationalError):
        connect(tmp_path / "broken.db")
    monkeypatch.setattr(schema, "MIGRATIONS", REAL_MIGRATIONS)
    again = connect(tmp_path / "broken.db")
    try:
        assert again.isolation_level == MODULE_DEFAULT_ISOLATION
        assert not again.in_transaction
    finally:
        again.close()


def test_an_open_with_nothing_pending_takes_no_lock(tmp_path: Path) -> None:
    path = tmp_path / "tracker.db"
    connect(path).close()
    holder = sqlite3.connect(path)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        again = connect(path)
        again.close()
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    # A runner that took the write lock would wait out the busy timeout
    # behind the holder and then fail; this one never asks for it.
    assert elapsed < BUSY_TIMEOUT_MS / 1000 / 2
