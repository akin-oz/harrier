"""A backup that can be restored, and a restore that is exercised (spec 030).

The previous script archived `data/` with tar while excluding `*.db-wal` and
`*.db-shm`, against a database opened in WAL mode. Committed transactions
since the last checkpoint live in exactly the file it omitted, and tar reads
a file being written page by page, so the main database could also be
captured torn. The archive exited zero and might not open.

It also resolved the data directory by its own rule while the application
honours `HARRIER_DATA_DIR`, so with the override set it archived an empty
directory and reported success. That is the worst failure available here: a
backup that runs nightly and contains nothing.

After cutover this is the only copy of a real person's job search, so three
things are non-negotiable and each is a function below.

**Snapshot through SQLite.** `VACUUM INTO` takes a consistent copy of a live
database including everything in the write-ahead log. No exclusion list, no
torn pages, no coordination with running writers.

**Verify what was written.** Every archive is opened afterwards and asked a
question only a working database can answer. An archive that fails is a
failed run and does not replace the previous one.

**A restore that is a command.** The recovery path was a paragraph in an ADR
and had never been executed. A path nobody runs is a path that does not work.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from harrier.db import DB_FILENAME, data_dir

BACKUP_DIR_ENV = "HARRIER_BACKUP_DIR"
ARCHIVE_PREFIX = "harrier-data-"
ARCHIVE_SUFFIX = ".tar.gz"

# Bounded so a repeating failure cannot fill the disk. Keeping several means
# a corruption noticed late still has a good copy behind it.
DEFAULT_KEEP = 14

# Names inside the archive. Fixed, so a restore does not have to guess.
SNAPSHOT_NAME = "tracker.db"
PAYLOAD_DIR = "data"


class BackupError(RuntimeError):
    """The backup could not be taken, or could not be trusted once taken."""


@dataclass(frozen=True)
class BackupResult:
    archive: Path
    jobs: int
    bytes_written: int
    pruned: tuple[Path, ...]


def backup_dir() -> Path:
    override = os.environ.get(BACKUP_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return Path.home() / "Backups" / "harrier"


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")


def snapshot_database(source: Path, destination: Path) -> None:
    """A consistent copy of a live database, WAL contents included.

    `VACUUM INTO` is the whole reason this module exists rather than a tar
    flag: it reads through SQLite, so a writer mid-transaction produces a
    snapshot of the last committed state instead of a torn file.
    """
    if not source.is_file():
        raise BackupError(f"no database at {source}")
    if destination.exists():
        raise BackupError(f"refusing to overwrite {destination}")
    conn = sqlite3.connect(source)
    try:
        conn.execute("VACUUM INTO ?", (str(destination),))
    except sqlite3.Error as error:
        raise BackupError(f"could not snapshot {source}: {error}") from error
    finally:
        conn.close()


def verify_database(path: Path) -> int:
    """Ask a question only a working database can answer. Returns the row count.

    Deliberately more than `PRAGMA integrity_check`: a real query over a real
    table is what proves the schema and the data survived, and a verification
    step that asks nothing is the guard-that-cannot-fail this project keeps
    producing.
    """
    if not path.is_file():
        raise BackupError(f"no database to verify at {path}")
    conn = sqlite3.connect(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise BackupError(f"{path} failed integrity_check: {integrity}")
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error as error:
        raise BackupError(f"{path} is not a usable harrier database: {error}") from error
    finally:
        conn.close()


def prune(destination: Path, keep: int) -> tuple[Path, ...]:
    """Drop the oldest archives beyond `keep`, newest always retained."""
    archives = sorted(
        (path for path in destination.glob(f"{ARCHIVE_PREFIX}*{ARCHIVE_SUFFIX}") if path.is_file()),
        key=lambda path: path.name,
        reverse=True,
    )
    doomed = archives[max(1, keep) :]
    for path in doomed:
        path.unlink()
    return tuple(doomed)


def create_backup(
    destination: Path | None = None, *, keep: int = DEFAULT_KEEP, source_dir: Path | None = None
) -> BackupResult:
    """Snapshot, archive, verify, prune. Any failure raises rather than exits 0.

    The data directory is resolved exactly as the application resolves it,
    `HARRIER_DATA_DIR` included. Backing up a different directory from the one
    in use is a silent total failure and must be impossible rather than
    documented.
    """
    source = source_dir if source_dir is not None else data_dir()
    if not source.is_dir():
        raise BackupError(f"no data directory at {source}")
    target_dir = destination if destination is not None else backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / f"{ARCHIVE_PREFIX}{_timestamp()}{ARCHIVE_SUFFIX}"
    if archive.exists():
        raise BackupError(f"an archive already exists at {archive}")

    with tempfile.TemporaryDirectory() as workspace:
        staging = Path(workspace) / PAYLOAD_DIR
        staging.mkdir()
        snapshot = staging / SNAPSHOT_NAME
        snapshot_database(source / DB_FILENAME, snapshot)
        expected = verify_database(snapshot)

        # Everything else in the data directory, minus the live database and
        # its sidecars: those are represented by the snapshot, and copying
        # them too would put a torn database in the archive next to a good one.
        for item in sorted(source.iterdir()):
            if item.name.startswith(DB_FILENAME):
                continue
            if item.is_dir():
                shutil.copytree(item, staging / item.name)
            else:
                shutil.copy2(item, staging / item.name)

        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname=PAYLOAD_DIR)

        # Verified from the archive, not from the staging copy. Verifying the
        # thing you did not write is how a check ends up proving nothing.
        found = verify_archive(archive)
        if found != expected:
            archive.unlink(missing_ok=True)
            raise BackupError(
                "archive verification disagreed with the snapshot: "
                f"{found} rows, expected {expected}"
            )

    return BackupResult(
        archive=archive,
        jobs=expected,
        bytes_written=archive.stat().st_size,
        pruned=prune(target_dir, keep),
    )


def _safe_extract(tar: tarfile.TarFile, into: Path) -> None:
    """Extract, refusing any member that would escape the target directory.

    An archive is data. This one is written by us, but a restore is exactly
    the moment somebody points the command at a file they were sent.
    """
    root = into.resolve()
    for member in tar.getmembers():
        resolved = (root / member.name).resolve()
        if not resolved.is_relative_to(root):
            raise BackupError(f"archive member escapes the target directory: {member.name}")
    tar.extractall(into, filter="data")


def verify_archive(archive: Path) -> int:
    """Open the database inside the archive and query it. Returns the row count."""
    if not archive.is_file():
        raise BackupError(f"no archive at {archive}")
    with tempfile.TemporaryDirectory() as workspace:
        target = Path(workspace)
        try:
            with tarfile.open(archive, "r:gz") as tar:
                _safe_extract(tar, target)
        except tarfile.TarError as error:
            raise BackupError(f"{archive} is not a readable archive: {error}") from error
        return verify_database(target / PAYLOAD_DIR / SNAPSHOT_NAME)


def restore_backup(archive: Path, target: Path | None = None, *, force: bool = False) -> int:
    """Restore an archive into a data directory. Returns the row count restored.

    Refuses a non-empty target without `force`. Restoring over an existing
    tracker is how two half-populated databases are created, and the operator
    running this has usually just lost one and cannot afford to lose the
    other.
    """
    into = target if target is not None else data_dir()
    if into.exists() and any(into.iterdir()) and not force:
        raise BackupError(
            f"{into} is not empty; restoring over it would merge two datasets. "
            "Move it aside, or pass force to overwrite."
        )
    restored = verify_archive(archive)

    with tempfile.TemporaryDirectory() as workspace:
        staging = Path(workspace)
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, staging)
        payload = staging / PAYLOAD_DIR
        if into.exists():
            shutil.rmtree(into)
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(payload), str(into))
    return restored
