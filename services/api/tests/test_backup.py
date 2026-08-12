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
from typing import cast
from unittest import mock

import pytest

import harrier.backup as backup_module
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
    destroy the directory it was going to replace.

    The sentinel is the point. Asserting only that the target is a directory
    passes just as well when the restore deleted everything in it and left an
    empty one behind, which is the failure worth catching (review finding on
    PR #34).
    """
    broken = tmp_path / f"{ARCHIVE_PREFIX}broken{ARCHIVE_SUFFIX}"
    broken.write_bytes(b"nonsense")
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep-me.txt"
    sentinel.write_text("the operator's data")
    with pytest.raises(BackupError):
        restore_backup(broken, target, force=True)
    assert target.is_dir()
    assert sentinel.read_text() == "the operator's data"


def test_restore_into_an_existing_file_is_refused(tmp_path: Path) -> None:
    """A mistyped path used to reach iterdir() on a file and raise
    NotADirectoryError, which the CLI does not catch, so the operator got a
    traceback instead of an answer (review finding on PR #34)."""
    archive = tmp_path / f"{ARCHIVE_PREFIX}x{ARCHIVE_SUFFIX}"
    archive.write_bytes(b"nonsense")
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("a file")
    with pytest.raises(BackupError, match="not a directory"):
        restore_backup(archive, occupied, force=True)
    assert occupied.read_text() == "a file"


def test_a_failed_replacement_puts_the_old_data_directory_back(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore removed the target before the replacement was in place, so
    a failure partway through left the operator with nothing. That is the loss
    this whole spec exists to prevent (review finding on PR #34)."""
    archive = create_backup(tmp_path / "backups").archive
    target = tmp_path / "target"
    target.mkdir()
    sentinel = target / "keep-me.txt"
    sentinel.write_text("the operator's data")

    real_rename = Path.rename

    def fail_installing_the_payload(self: Path, other: object) -> Path:
        # Only the install, not the rollback that follows it: keying on the
        # destination would fail both and prove nothing about the recovery.
        if self.name == PAYLOAD_DIR:
            raise OSError("no space left on device")
        return real_rename(self, other)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Path, "rename", fail_installing_the_payload)
    with pytest.raises(OSError):
        restore_backup(archive, target, force=True)
    assert sentinel.read_text() == "the operator's data"


def test_a_failed_rollback_says_where_the_data_directory_went(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case where the directory really is not where it was. Failing
    silently would leave the operator hunting the filesystem for it."""
    archive = create_backup(tmp_path / "backups").archive
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep-me.txt").write_text("the operator's data")

    moved_aside = None
    real_rename = Path.rename

    def fail_after_moving_aside(self: Path, other: object) -> Path:
        nonlocal moved_aside
        if self == target:
            moved_aside = Path(str(other))
            return real_rename(self, other)  # pyright: ignore[reportArgumentType]
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "rename", fail_after_moving_aside)
    with pytest.raises(BackupError, match="could not be put back") as raised:
        restore_backup(archive, target, force=True)
    assert moved_aside is not None
    assert str(moved_aside) in str(raised.value)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert (moved_aside / "keep-me.txt").read_text() == "the operator's data"


def test_the_restored_payload_is_the_payload_that_was_verified(data: Path, tmp_path: Path) -> None:
    """One extraction, one verification, one install. Opening the archive to
    verify it and then opening it again to install it read the file twice and
    checked only the first read (review finding on PR #34)."""
    archive = create_backup(tmp_path / "backups").archive
    opened: list[str] = []
    real_open = tarfile.open

    def counting_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        opened.append(str(args[0]) if args else "")
        return cast("tarfile.TarFile", real_open(*args, **kwargs))  # pyright: ignore[reportCallIssue, reportArgumentType]

    with mock.patch.object(tarfile, "open", counting_open):
        restore_backup(archive, tmp_path / "target")
    assert [name for name in opened if name == str(archive)] == [str(archive)]


# --- retention --------------------------------------------------------------


# All within ISO week 32 of 2026, so these exercise the count half of the
# policy without the weekly half sparing anything.
ONE_WEEK = ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")


def _archives(directory: Path, days: tuple[str, ...]) -> None:
    for day in days:
        (directory / f"{ARCHIVE_PREFIX}{day}-000000{ARCHIVE_SUFFIX}").write_text("x")


def test_retention_keeps_the_configured_number(tmp_path: Path) -> None:
    _archives(tmp_path, ONE_WEEK)
    pruned = prune(tmp_path, keep=3)
    remaining = sorted(path.name for path in tmp_path.glob(f"{ARCHIVE_PREFIX}*"))
    assert len(remaining) == 3
    assert len(pruned) == 2


def test_pruning_refuses_to_keep_nothing(tmp_path: Path) -> None:
    """A repeating failure must not be able to leave the operator with nothing.

    The floor used to be silent: `max(1, keep)` turned a request to keep zero
    into a request to keep one, so a caller asking for something impossible
    got something else without being told (review finding on PR #34). The CLI
    already refuses it, and now so does the function it calls.
    """
    _archives(tmp_path, ONE_WEEK[:3])
    with pytest.raises(BackupError, match="1 or more"):
        prune(tmp_path, keep=0)
    assert len(list(tmp_path.glob(f"{ARCHIVE_PREFIX}*"))) == 3


def test_retention_drops_the_oldest_first(tmp_path: Path) -> None:
    _archives(tmp_path, ONE_WEEK[:4])
    pruned = prune(tmp_path, keep=2)
    assert sorted(path.name for path in pruned) == [
        f"{ARCHIVE_PREFIX}2026-08-03-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-08-04-000000{ARCHIVE_SUFFIX}",
    ]


def test_the_newest_of_each_week_survives_pruning(tmp_path: Path) -> None:
    """The spec's retention is "the most recent N and the most recent weekly",
    and only the count half was implemented (review finding on PR #34).

    With fourteen nightly archives and no weekly rule, a corruption noticed a
    fortnight late has nothing behind it. Here `keep=2` would have deleted
    every archive from the three earlier weeks.
    """
    _archives(
        tmp_path,
        (
            "2026-07-17",  # week 29
            "2026-07-24",  # week 30
            "2026-07-30",  # week 31
            "2026-07-31",  # week 31, newer
            "2026-08-06",  # week 32
            "2026-08-07",  # week 32, newer
        ),
    )
    prune(tmp_path, keep=2)
    remaining = sorted(path.name for path in tmp_path.glob(f"{ARCHIVE_PREFIX}*"))
    assert remaining == [
        f"{ARCHIVE_PREFIX}2026-07-17-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-07-24-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-07-31-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-08-06-000000{ARCHIVE_SUFFIX}",
        f"{ARCHIVE_PREFIX}2026-08-07-000000{ARCHIVE_SUFFIX}",
    ]
    # The older of week 31 goes; its week is already represented.
    assert not (tmp_path / f"{ARCHIVE_PREFIX}2026-07-30-000000{ARCHIVE_SUFFIX}").exists()


def test_an_unparseable_archive_name_is_never_deleted(tmp_path: Path) -> None:
    """A file this module cannot date is a file it cannot judge, and deleting
    the operator's only good copy on a guess is not a trade worth making."""
    _archives(tmp_path, ONE_WEEK)
    # Sorts oldest, so only the cannot-date branch can spare it. Named
    # "handmade" it sorted newest and the count rule kept it, which made this
    # test pass whatever the branch did.
    stray = tmp_path / f"{ARCHIVE_PREFIX}0000-handmade{ARCHIVE_SUFFIX}"
    stray.write_text("x")
    prune(tmp_path, keep=1)
    assert stray.exists()


def test_a_failed_archive_is_not_left_behind(data: Path, tmp_path: Path) -> None:
    """A partial archive counted towards `keep`, so the next prune could
    delete a verified archive to make room for one that would not open: a
    failed backup destroying a good one (review finding on PR #34)."""
    destination = tmp_path / "backups"
    destination.mkdir()
    unreadable = mock.patch.object(
        backup_module, "verify_archive", side_effect=BackupError("unreadable")
    )
    with unreadable, pytest.raises(BackupError):
        create_backup(destination)
    assert list(destination.glob(f"{ARCHIVE_PREFIX}*")) == []


def test_a_backup_run_prunes(data: Path, tmp_path: Path) -> None:
    destination = tmp_path / "backups"
    destination.mkdir()
    # One week, so the weekly half of the policy spares nothing here.
    for day in ("2020-01-07", "2020-01-08", "2020-01-09"):
        (destination / f"{ARCHIVE_PREFIX}{day}-000000{ARCHIVE_SUFFIX}").write_text("x")
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
