"""Two processes can write the tracker at once (spec 051, ADR-010).

The container serving the API and the host CLI that launchd invokes are
separate processes reaching one SQLite file, and after spec 051 they reach it
across a Docker bind mount. ADR-003's single write path is still the only
path, but "one path" is not "one process", and concurrency was previously a
property nobody had exercised.

Spec 051 named three resolutions and refused to let an implementation pick
none. This is the third: WAL, which `connect` already set, plus a busy timeout,
which it did not. Without the timeout a writer that arrives while another holds
the write lock fails immediately with "database is locked", and on a scheduled
run that is a failure nobody is watching for.

Real subprocesses rather than threads, because threads in one interpreter share
a connection pool and a GIL and would prove something easier than the thing
that actually happens.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harrier.db import BUSY_TIMEOUT_MS, connect

WRITER = """
import sqlite3, sys
from harrier.db import connect

path, tag, rows = sys.argv[1], sys.argv[2], int(sys.argv[3])
conn = connect(__import__("pathlib").Path(path))
try:
    for index in range(rows):
        conn.execute("INSERT INTO concurrency_probe (writer, n) VALUES (?, ?)", (tag, index))
        conn.commit()
finally:
    conn.close()
"""


def probe_table(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS concurrency_probe "
            "(id INTEGER PRIMARY KEY, writer TEXT NOT NULL, n INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()


def test_the_busy_timeout_is_set_on_every_connection(tmp_path: Path) -> None:
    """The pragma, read back from the connection rather than from the source.

    A test asserting the string appears in `db.py` would pass for a line that
    had been commented out.
    """
    conn = connect(tmp_path / "t.db")
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        conn.close()


def test_two_processes_writing_at_once_both_commit(tmp_path: Path) -> None:
    """The failure this guards against is corruption or a lost writer.

    Fails without the busy timeout: whichever process arrives second while the
    first holds the write lock exits non-zero with "database is locked", and
    its rows are missing.
    """
    path = tmp_path / "tracker.db"
    probe_table(path)
    rows = 60

    writers = [
        subprocess.Popen(
            [sys.executable, "-c", WRITER, str(path), tag, str(rows)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for tag in ("container", "launchd")
    ]
    failures: list[str] = []
    for writer in writers:
        _, stderr = writer.communicate(timeout=120)
        if writer.returncode != 0:
            failures.append(stderr.strip()[-300:])
    assert failures == [], "a concurrent writer failed: " + " | ".join(failures)

    conn = connect(path)
    try:
        counts = dict(
            conn.execute(
                "SELECT writer, COUNT(*) FROM concurrency_probe GROUP BY writer"
            ).fetchall()
        )
        assert counts == {"container": rows, "launchd": rows}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_a_writer_that_never_gets_its_turn_still_reports(tmp_path: Path) -> None:
    """The timeout is a wait, not a promise, and it has to end in an error.

    A held exclusive lock outlasting the timeout must surface as
    `OperationalError` rather than hanging a scheduled run forever. This is why
    the timeout is finite: an infinite one would turn a stuck writer into a job
    that never returns and never reports.
    """
    path = tmp_path / "tracker.db"
    probe_table(path)

    holder = connect(path)
    blocked = connect(path)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        holder.execute("INSERT INTO concurrency_probe (writer, n) VALUES ('holder', 0)")
        # Shorter than BUSY_TIMEOUT_MS so the test does not sit for the real
        # wait; the behaviour at expiry is what is being pinned.
        blocked.execute("PRAGMA busy_timeout=200")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            blocked.execute("INSERT INTO concurrency_probe (writer, n) VALUES ('blocked', 0)")
            blocked.commit()
    finally:
        holder.rollback()
        holder.close()
        blocked.close()
