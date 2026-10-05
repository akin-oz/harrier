"""A CLI command closes the tracker connection it opened (spec 076).

On CPython 3.12 a `sqlite3.Connection` sits in a reference cycle with its
statement cache, so one that is never closed outlives the command and closes
at the next gc pass or at exit, checkpointing the WAL whenever that is. Most
command handlers opened with `conn = connect()` and never closed.

Every test disables gc, so a connection is closed only if the command closed
it. With gc on, a collection between the command and the assertion could
pass a handler that leaks.
"""

from __future__ import annotations

import gc
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

import harrier_cli.main as cli
from harrier.db import connect
from harrier.tracker.store import add_job


@pytest.fixture
def opened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[sqlite3.Connection]]:
    """Every connection a command opens through `connect`, with gc off."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    recorded: list[sqlite3.Connection] = []

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = connect(*args, **kwargs)  # type: ignore[arg-type]
        recorded.append(conn)
        return conn

    monkeypatch.setattr(cli, "connect", recording_connect)
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield recorded
    finally:
        if was_enabled:
            gc.enable()


def assert_all_closed(recorded: list[sqlite3.Connection]) -> None:
    assert recorded, "the command opened no connection, so this proves nothing"
    for conn in recorded:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            conn.execute("SELECT 1")


def test_a_read_command_closes_its_connection(opened: list[sqlite3.Connection]) -> None:
    assert cli.main(["profile", "list"]) == 0
    assert_all_closed(opened)


def test_a_command_that_returns_early_closes_its_connection(
    opened: list[sqlite3.Connection],
) -> None:
    """`brief show` passed `connect()` inline, so nothing could close it."""
    assert cli.main(["brief", "show", "1"]) == 1
    assert_all_closed(opened)


def test_a_command_that_fails_closes_its_connection(opened: list[sqlite3.Connection]) -> None:
    """`migrate-legacy` into a tracker that already has rows aborts with
    MigrationError, which the handler turns into exit 1."""
    seed = connect()
    try:
        add_job(
            seed,
            {
                "company": "Northwind Labs",
                "title": "Senior Frontend Engineer",
                "url": "https://boards.example.com/northwind/1",
                "source": "greenhouse",
            },
        )
    finally:
        seed.close()
    assert cli.main(["migrate-legacy", "--jobs", "unused.csv"]) == 1
    assert_all_closed(opened)


def test_a_command_that_raises_closes_its_connection(
    opened: list[sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_conn: sqlite3.Connection) -> list[object]:
        raise RuntimeError("store failed")

    monkeypatch.setattr(cli, "list_documents", broken)
    with pytest.raises(RuntimeError, match="store failed"):
        cli.main(["profile", "list"])
    assert_all_closed(opened)
