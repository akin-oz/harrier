"""A backup that can be restored (spec 030).

The defect was an archive that exited zero and might not open: tar over a
live WAL-mode database with the write-ahead log excluded, so committed
transactions since the last checkpoint were simply absent. After cutover this
is the only copy of a real person's job search, which is why every test here
is about the archive being *usable* rather than about it existing.
"""

from __future__ import annotations

import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from harrier.backup import (
    ARCHIVE_PREFIX,
    ARCHIVE_SUFFIX,
    PAYLOAD_DIR,
    SNAPSHOT_NAME,
    BackupError,
    create_backup,
    prune,
    restore_backup,
    verify_archive,
    verify_database,
)
from harrier.db import DB_FILENAME, connect
from harrier.tracker.store import add_job, list_jobs


def a_job(index: int) -> dict[str, str]:
    return {
        "company": f"Company {index}",
        "title": "Senior Frontend Engineer",
        "url": f"https://boards.example.com/example/{index}",
        "source": "greenhouse",
        "location": "Remote, Europe",
    }


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    for index in range(3):
        add_job(conn, a_job(index))
    conn.close()
    return directory


# --- the snapshot is consistent ---------------------------------------------


def test_a_backup_holds_every_committed_row(data: Path, tmp_path: Path) -> None:
    result = create_backup(tmp_path / "backups")
    assert result.jobs == 3
    assert verify_archive(result.archive) == 3


def test_a_backup_taken_during_an_open_write_holds_the_committed_rows(
    data: Path, tmp_path: Path
) -> None:
    """The defect that motivated this. tar excluded the write-ahead log, so
    everything committed since the last checkpoint was missing from the
    archive while the run still exited zero.

    VACUUM INTO reads through SQLite, so the snapshot is the last committed
    state: the fourth row is present, the uncommitted fifth is not.
    """
    writer = connect()
    add_job(writer, a_job(4))  # committed by add_job
    writer.execute("BEGIN")
    writer.execute(
        "INSERT INTO jobs (company, title, url, source, location) VALUES (?,?,?,?,?)",
        ("Uncommitted", "Senior Frontend Engineer", "https://x/9", "greenhouse", "Remote"),
    )

    result = create_backup(tmp_path / "backups")
    writer.rollback()
    writer.close()

    assert result.jobs == 4
    assert verify_archive(result.archive) == 4


def test_the_backup_follows_the_data_directory_override(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old script resolved `data/` by its own rule while the application
    honours HARRIER_DATA_DIR, so with the override set it archived an empty
    directory and reported success: a nightly backup containing nothing."""
    elsewhere = tmp_path / "somewhere-else"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(elsewhere))
    conn = connect()
    add_job(conn, a_job(99))
    conn.close()

    result = create_backup(tmp_path / "backups")
    assert result.jobs == 1, "the backup archived a different directory from the one in use"


def test_other_files_in_the_data_directory_are_carried(data: Path, tmp_path: Path) -> None:
    (data / "discovery").mkdir(parents=True, exist_ok=True)
    (data / "discovery" / "greenhouse_seen.json").write_text('{"decisions": {}}', encoding="utf-8")
    result = create_backup(tmp_path / "backups")
    with tarfile.open(result.archive, "r:gz") as tar:
        names = tar.getnames()
    assert f"{PAYLOAD_DIR}/discovery/greenhouse_seen.json" in names


def test_the_live_database_and_its_sidecars_are_not_archived(data: Path, tmp_path: Path) -> None:
    """The snapshot represents them. Copying the live files too would put a
    torn database in the archive next to a good one.

    Exercised with a stray file rather than a real `-wal`, and that detail is
    the point. The first version of this test wrote `tracker.db-wal` by hand
    and asserted it was absent, which passed whatever the code did: SQLite
    removes the write-ahead log when the snapshot connection closes, so the
    file was gone before the copy loop ran. The test could not fail, which is
    the exact defect class this spec exists to close.
    """
    stray = data / f"{DB_FILENAME}.old"
    stray.write_text("a copy somebody left behind", encoding="utf-8")

    result = create_backup(tmp_path / "backups")
    with tarfile.open(result.archive, "r:gz") as tar:
        names = tar.getnames()

    assert f"{PAYLOAD_DIR}/{DB_FILENAME}.old" not in names
    assert f"{PAYLOAD_DIR}/{SNAPSHOT_NAME}" in names


def test_the_archived_database_is_the_snapshot_not_the_live_file(
    data: Path, tmp_path: Path
) -> None:
    """The archive's tracker.db must be the one VACUUM INTO produced. If the
    copy loop overwrote it with the live file, the archive would carry
    whatever tar happened to read mid-write."""
    result = create_backup(tmp_path / "backups")
    with tempfile.TemporaryDirectory() as workspace:
        with tarfile.open(result.archive, "r:gz") as tar:
            tar.extractall(workspace, filter="data")
        archived = Path(workspace) / PAYLOAD_DIR / SNAPSHOT_NAME
        # A VACUUM INTO output has no journal mode set to WAL: it is a fresh
        # database written page by page, which is what makes it consistent.
        conn = sqlite3.connect(archived)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
    assert str(mode).lower() != "wal", "the archived database looks like the live file"


# --- verification can fail ---------------------------------------------------


def test_a_corrupted_archive_is_rejected(data: Path, tmp_path: Path) -> None:
    """The check has to be able to fail, or it is the guard-that-does-nothing
    this project keeps producing."""
    result = create_backup(tmp_path / "backups")
    result.archive.write_bytes(b"not a tarball at all")
    with pytest.raises(BackupError):
        verify_archive(result.archive)


def test_a_truncated_database_inside_an_archive_is_rejected(tmp_path: Path) -> None:
    payload = tmp_path / PAYLOAD_DIR
    payload.mkdir()
    (payload / SNAPSHOT_NAME).write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    archive = tmp_path / f"{ARCHIVE_PREFIX}broken{ARCHIVE_SUFFIX}"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname=PAYLOAD_DIR)
    with pytest.raises(BackupError):
        verify_archive(archive)


def test_verification_asks_a_question_about_content(tmp_path: Path) -> None:
    """A database that opens but has no tracker table is not a harrier
    backup, and PRAGMA integrity_check alone would pass it."""
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(BackupError):
        verify_database(path)


def test_an_archive_escaping_its_target_is_refused(tmp_path: Path) -> None:
    """A restore is exactly the moment somebody points the command at a file
    they were sent."""
    evil = tmp_path / f"{ARCHIVE_PREFIX}evil{ARCHIVE_SUFFIX}"
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with tarfile.open(evil, "w:gz") as tar:
        tar.add(outside, arcname="../escaped.txt")
    with pytest.raises(BackupError, match="escapes"):
        verify_archive(evil)


# --- restore ----------------------------------------------------------------


def test_a_verified_archive_restores_to_a_readable_tracker(data: Path, tmp_path: Path) -> None:
    """The path that had never been executed. A recovery path nobody runs is
    a recovery path that does not work."""
    result = create_backup(tmp_path / "backups")
    target = tmp_path / "restored"
    assert restore_backup(result.archive, target) == 3

    conn = sqlite3.connect(target / DB_FILENAME)
    conn.row_factory = sqlite3.Row
    rows = list_jobs(conn)
    conn.close()
    assert len(rows) == 3


def test_restore_refuses_a_non_empty_directory(data: Path, tmp_path: Path) -> None:
    """Restoring over an existing tracker is how two half-populated databases
    are created, and the operator running this has usually just lost one."""
    result = create_backup(tmp_path / "backups")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "something.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(result.archive, occupied)
    assert (occupied / "something.txt").is_file()


def test_restore_overwrites_when_forced(data: Path, tmp_path: Path) -> None:
    result = create_backup(tmp_path / "backups")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "something.txt").write_text("x", encoding="utf-8")
    assert restore_backup(result.archive, occupied, force=True) == 3
    assert not (occupied / "something.txt").exists()


def test_restore_of_a_broken_archive_leaves_the_target_alone(data: Path, tmp_path: Path) -> None:
    """Verification happens before anything is moved, so a bad archive cannot
    destroy the directory it was going to replace."""
    broken = tmp_path / f"{ARCHIVE_PREFIX}broken{ARCHIVE_SUFFIX}"
    broken.write_bytes(b"nonsense")
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(BackupError):
        restore_backup(broken, target, force=True)
    assert target.is_dir()


# --- retention --------------------------------------------------------------


def test_retention_keeps_the_configured_number(tmp_path: Path) -> None:
    for day in range(5):
        (tmp_path / f"{ARCHIVE_PREFIX}2026-08-0{day}-000000{ARCHIVE_SUFFIX}").write_text("x")
    pruned = prune(tmp_path, keep=3)
    remaining = sorted(path.name for path in tmp_path.glob(f"{ARCHIVE_PREFIX}*"))
    assert len(remaining) == 3
    assert len(pruned) == 2


def test_retention_never_deletes_the_newest(tmp_path: Path) -> None:
    """Even a keep of zero. A repeating failure must not be able to leave the
    operator with nothing."""
    for day in range(3):
        (tmp_path / f"{ARCHIVE_PREFIX}2026-08-0{day}-000000{ARCHIVE_SUFFIX}").write_text("x")
    prune(tmp_path, keep=0)
    remaining = sorted(path.name for path in tmp_path.glob(f"{ARCHIVE_PREFIX}*"))
    assert remaining == [f"{ARCHIVE_PREFIX}2026-08-02-000000{ARCHIVE_SUFFIX}"]


def test_retention_drops_the_oldest_first(tmp_path: Path) -> None:
    for day in range(4):
        (tmp_path / f"{ARCHIVE_PREFIX}2026-08-0{day}-000000{ARCHIVE_SUFFIX}").write_text("x")
    pruned = prune(tmp_path, keep=2)
    assert sorted(path.name for path in pruned) == [
        f"{ARCHIVE_PREFIX}2026-08-00-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-08-01-000000{ARCHIVE_SUFFIX}",
    ]


def test_a_backup_run_prunes(data: Path, tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    for day in range(3):
        (destination / f"{ARCHIVE_PREFIX}2020-01-0{day}-000000{ARCHIVE_SUFFIX}").write_text("x")
    result = create_backup(destination, keep=2)
    assert len(result.pruned) == 2
    assert result.archive.is_file()


# --- failures are failures ---------------------------------------------------


def test_a_missing_data_directory_fails_rather_than_exiting_zero(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no data directory"):
        create_backup(tmp_path / "backups", source_dir=tmp_path / "absent")


def test_a_missing_database_fails(tmp_path: Path) -> None:
    empty = tmp_path / "data"
    empty.mkdir()
    with pytest.raises(BackupError, match="no database"):
        create_backup(tmp_path / "backups", source_dir=empty)
