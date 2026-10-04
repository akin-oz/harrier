"""Who owns the tracker database right now, and is it intact? (spec 061)

The guard in `harrier.db` refuses an open the moment it would be unsafe. This
is the question asked on purpose: before a raw `sqlite3` session the guard
cannot see, after a scare, or to check the rule is holding.

It never opens the live database with SQLite unless the ownership check
allows it. The journal mode comes from two header bytes read as plain bytes,
which needs no SQLite and touches no WAL index.

No line carries an absolute path. The mount source is reported as "this
repository" or "elsewhere", because the path contains a home directory and
this output gets pasted into issues and pull requests.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from harrier import container
from harrier.db import (
    EXIT_DATABASE_OWNED,
    DatabaseOwnedByContainer,
    DatabaseOwnershipUnknown,
    check_database_ownership,
    default_db_path,
    live_data_root,
)

ALLOWED = "allowed"
CONTAINER_OWNS = "container"
UNKNOWN = "unknown"

# Docker creates this file in every container it starts.
DOCKERENV = Path("/.dockerenv")

# The journal mode as SQLite records it in the header: bytes 18 and 19, the
# file format write and read versions. 1 is a rollback journal, 2 is WAL.
_JOURNAL_BYTES = {b"\x01\x01": "rollback", b"\x02\x02": "wal"}

_INTEGRITY_INSIDE = f"docker exec {container.CONTAINER_NAME} harrier doctor --integrity"


@dataclass
class DoctorResult:
    lines: list[str] = field(default_factory=list[str])
    exit_code: int = 0


def running_in() -> str:
    return "container" if DOCKERENV.exists() else "host"


def journal_mode(path: Path) -> str:
    if not path.is_file():
        return "no database"
    # An unreadable file is a finding, not a reason to lose the verdict that
    # follows it (review finding on PR #110).
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return "unreadable"
    if len(header) < 20 or not header.startswith(b"SQLite format 3\x00"):
        return "not a sqlite database"
    return _JOURNAL_BYTES.get(header[18:20], "unrecognised")


def verdict(path: Path) -> tuple[str, str | None]:
    """The guard's own answer for `path`, so the report cannot disagree with it."""
    try:
        check_database_ownership(path)
    except DatabaseOwnedByContainer:
        return CONTAINER_OWNS, None
    except DatabaseOwnershipUnknown as error:
        return UNKNOWN, str(error)
    return ALLOWED, None


def _engine_lines(state: container.ContainerState) -> list[str]:
    if state.engine == container.UNREACHABLE:
        return [
            "docker engine: not reachable",
            f"{container.CONTAINER_NAME} container: not running",
            "data mount: none",
        ]
    if state.engine == container.UNKNOWN:
        return [
            f"docker engine: unknown ({state.reason})",
            f"{container.CONTAINER_NAME} container: unknown",
            "data mount: unknown",
        ]
    if not state.running:
        return [
            "docker engine: reachable",
            f"{container.CONTAINER_NAME} container: not running",
            "data mount: none",
        ]
    if state.data_source is None:
        mount = "none"
    elif live_data_root().resolve().is_relative_to(state.data_source.resolve()):
        mount = "this repository"
    else:
        mount = "elsewhere"
    return [
        "docker engine: reachable",
        f"{container.CONTAINER_NAME} container: running",
        f"data mount: {mount}",
    ]


def run_doctor(*, require_host_access: bool = False, integrity: bool = False) -> DoctorResult:
    path = default_db_path()
    result = DoctorResult()
    result.lines.append(f"running in: {running_in()}")
    result.lines.extend(_engine_lines(container.detect()))
    result.lines.append(f"journal mode: {journal_mode(path)}")
    for suffix in ("wal", "shm"):
        present = path.with_name(f"{path.name}-{suffix}").exists()
        result.lines.append(f"{suffix} file: {'present' if present else 'absent'}")

    owner, reason = verdict(path)
    if owner == ALLOWED:
        result.lines.append("host access: allowed")
    elif owner == CONTAINER_OWNS:
        result.lines.append("host access: refused (container owns the database)")
    else:
        result.lines.append(f"host access: refused (ownership unknown: {reason})")
    # Consistent whichever side owns the file; only not knowing is a fault.
    result.exit_code = 1 if owner == UNKNOWN else 0

    if require_host_access and owner != ALLOWED:
        result.exit_code = EXIT_DATABASE_OWNED
    if integrity:
        _integrity(path, owner, result)
    return result


def _integrity(path: Path, owner: str, result: DoctorResult) -> None:
    if owner == CONTAINER_OWNS:
        result.lines.append(f"integrity: not checked from here; run: {_INTEGRITY_INSIDE}")
        result.exit_code = EXIT_DATABASE_OWNED
        return
    if owner == UNKNOWN:
        result.lines.append("integrity: not checked (ownership unknown)")
        result.exit_code = EXIT_DATABASE_OWNED
        return
    if not path.is_file():
        result.lines.append("integrity: no database")
        result.exit_code = 1
        return
    # Never a read-write connection. Closing the last one checkpoints a
    # leftover WAL into the database and deletes it, so the check would change
    # the file it reports on and destroy the evidence of a crash (review
    # finding on PR #110). With no WAL, `immutable=1` reads the file and
    # creates nothing; with one, `mode=ro` reads through it and leaves it in
    # place. Not `harrier.db.connect`, which would also migrate the schema.
    wal = path.with_name(f"{path.name}-wal")
    options = "mode=ro" if wal.exists() else "mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?{options}", uri=True)
        try:
            rows = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        finally:
            conn.close()
    except sqlite3.DatabaseError as error:
        result.lines.append(f"integrity: {error}")
        result.exit_code = 1
        return
    if rows == ["ok"]:
        result.lines.append("integrity: ok")
        return
    result.lines.extend(f"integrity: {row}" for row in rows)
    result.exit_code = 1
