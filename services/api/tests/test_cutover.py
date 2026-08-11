"""The cutover (spec 024).

Every effect is injected, so this exercises the sequence without a cutover
ever happening. That is the point: the real thing runs once, and it has to
be right the first time.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

import harrier.cutover as cutover_module
from harrier.cutover import (
    OLD_LABELS,
    CutoverError,
    CutoverResult,
    env_check,
    old_jobs_check,
    preflight,
    quiesce,
    run_cutover,
    snapshot,
    tracker_check,
    utc_stamp,
)
from harrier.db import connect
from harrier.tracker import add_job


def loaded(_args: list[str]) -> tuple[int, str, str]:
    return (0, "", "")


def not_loaded(_args: list[str]) -> tuple[int, str, str]:
    return (3, "", "No such process")


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    add_job(
        conn,
        {
            "company": "Example Co",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/exampleco/1",
            "source": "greenhouse",
        },
    )
    return conn


@pytest.fixture()
def old_repo(tmp_path: Path) -> Path:
    root = tmp_path / "old"
    (root / "tracker").mkdir(parents=True)
    (root / "tracker" / "jobs.csv").write_text("company,title\n", encoding="utf-8")
    (root / "state").mkdir()
    (root / "state" / "seen.json").write_text("{}", encoding="utf-8")
    (root / "gmail_handler.log").write_text("{}\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=abc\n", encoding="utf-8")
    return root


@pytest.fixture()
def ready_checklist(tmp_path: Path) -> Path:
    """A checklist with every real matrix item decided."""
    from harrier.parity import parse_matrix, render_checklist

    rows = parse_matrix()
    text = render_checklist(rows)
    for row in rows:
        text = text.replace(f"- [ ] `{row.slug}`", f"- [x] `{row.slug}`")
    path = tmp_path / "checklist.md"
    path.write_text(text, encoding="utf-8")
    return path


# --- preflight ---------------------------------------------------------------


def test_an_undecided_checklist_blocks(db: sqlite3.Connection, old_repo: Path) -> None:
    checks = preflight(db, old_root=old_repo, launchctl=loaded)
    names = {check.name for check in checks.blocked}
    assert "parity checklist" in names
    assert not checks.ready


def test_a_decided_checklist_clears_every_mechanical_check(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path
) -> None:
    checks = preflight(db, old_root=old_repo, checklist_path=ready_checklist, launchctl=loaded)
    assert checks.ready, [check.line() for check in checks.blocked]
    # What a machine cannot check is stated rather than assumed.
    assert len(checks.attestations) == 3


def test_an_empty_tracker_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Going live against an empty tracker would have the first scheduled run
    # rediscover a year of postings as new.
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    assert not tracker_check(connect()).passed


def test_jobs_already_gone_blocks(db: sqlite3.Connection) -> None:
    check = old_jobs_check(OLD_LABELS, not_loaded)
    assert not check.passed
    assert "none of the old plists" in check.detail


def test_a_malformed_env_line_blocks_and_names_the_line(tmp_path: Path) -> None:
    """The defect that killed the old digest: a value wrapped onto a second
    line, so `set -a; . .env` tries to execute it and exits 127."""
    env = tmp_path / ".env"
    env.write_text("TOKEN=sk-line-one\nand-this-is-the-rest-of-the-value\nOTHER=2\n", "utf-8")
    check = env_check(env)
    assert not check.passed
    assert "line(s) 2" in check.detail
    assert "127" in check.detail


def test_a_clean_env_passes(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# a comment\n\nTOKEN=abc\nOTHER=2\n", encoding="utf-8")
    assert env_check(env).passed


def test_a_missing_env_is_not_a_blocker(tmp_path: Path) -> None:
    assert env_check(tmp_path / "absent").passed


# --- the sequence ------------------------------------------------------------


def test_a_dry_run_touches_nothing(db: sqlite3.Connection, old_repo: Path, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def recording(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        return (0, "", "")

    result = run_cutover(db, old_root=old_repo, stamp="2026-08-10-1200", launchctl=recording)
    assert not result.executed
    assert all(line.startswith(("would", "verify")) for line in result.lines), result.lines
    # Only the preflight probe, never a bootout.
    assert all(args[0] == "list" for args in calls)


def test_executing_without_a_clear_preflight_is_refused(
    db: sqlite3.Connection, old_repo: Path
) -> None:
    with pytest.raises(CutoverError, match="preflight is blocked"):
        run_cutover(
            db,
            old_root=old_repo,
            stamp="s",
            execute=True,
            attested=True,
            launchctl=loaded,
        )


def test_executing_without_attestation_is_refused(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path
) -> None:
    """Everything a machine can check can pass while the week of dual-running
    never happened. The operator has to say so."""
    with pytest.raises(CutoverError, match="attestations have not been made"):
        run_cutover(
            db,
            old_root=old_repo,
            stamp="s",
            execute=True,
            attested=False,
            checklist_path=ready_checklist,
            launchctl=loaded,
        )


def test_a_full_execution_quiesces_snapshots_verifies_and_installs(
    db: sqlite3.Connection,
    old_repo: Path,
    ready_checklist: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    booted: list[str] = []

    def runner(args: list[str]) -> tuple[int, str, str]:
        if args[0] == "bootout":
            booted.append(args[1])
        return (0, "", "")

    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=runner,
        install=lambda: ["installed 3 plists"],
    )
    assert result.ok
    assert len(booted) == len(OLD_LABELS)
    assert result.snapshot is not None
    assert (result.snapshot / "tracker" / "jobs.csv").is_file()
    assert (result.snapshot / "gmail_handler.log").is_file()
    assert "installed 3 plists" in result.lines
    # The record goes to data/, never docs/: it is dated operational state
    # about a real job search (ADR-008).
    log = tmp_path / "data" / "cutover" / "2026-08-10-1200.md"
    assert log.is_file()
    assert "unloaded" in log.read_text(encoding="utf-8")


def test_a_blocked_dry_run_reports_the_blockers_and_is_not_ok(
    db: sqlite3.Connection, old_repo: Path
) -> None:
    """A rehearsal that reports success while the real run would refuse is
    worse than no rehearsal at all (review finding on PR #22)."""
    result = run_cutover(db, old_root=old_repo, stamp="s", launchctl=loaded)
    assert not result.executed
    assert not result.ok
    assert any("parity checklist" in line for line in result.blocked)


def test_a_clear_dry_run_is_ok(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path
) -> None:
    result = run_cutover(
        db, old_root=old_repo, stamp="s", checklist_path=ready_checklist, launchctl=loaded
    )
    assert result.ok
    assert result.blocked == []


def agents_dir_with_plists(tmp_path: Path) -> Path:
    """An agents directory as a machine running the old jobs would have it.

    The rollback reloads a job by pointing launchctl at its plist, so a
    directory without them is a machine where rollback cannot work. The
    tests used to pass an empty directory and assert a successful rollback,
    which only held because nothing checked (spec 037).
    """
    directory = tmp_path / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    for label in OLD_LABELS:
        (directory / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    return directory


def test_a_failed_unload_rolls_back_what_was_already_stopped(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """Stopping at the first failure still leaves the old system half down.
    The only state worth ending in is the one we started from."""
    calls: list[list[str]] = []

    def fails_on_the_second(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        if args[0] == "bootout" and OLD_LABELS[1] in args[1]:
            return (1, "", "Operation not permitted")
        return (0, "", "")

    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="s",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=fails_on_the_second,
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert not result.ok
    # The third was never attempted, and the first was put back.
    booted = [args[1] for args in calls if args[0] == "bootout"]
    assert OLD_LABELS[2] not in " ".join(booted)
    assert any("rolled back: reloaded" in line for line in result.lines)
    assert result.snapshot is None


def test_a_rollback_that_itself_fails_is_reported(tmp_path: Path) -> None:
    """The operator has to learn which jobs are still down here, not at the
    next scheduled run that silently does not happen."""

    def everything_fails(args: list[str]) -> tuple[int, str, str]:
        if args[0] == "bootout" and OLD_LABELS[0] in args[1]:
            return (0, "", "")
        return (1, "", "Operation not permitted")

    result = CutoverResult(executed=True)
    quiesce(
        OLD_LABELS,
        everything_fails,
        execute=True,
        result=result,
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert any("rollback failed" in failure for failure in result.failures)
    assert any(OLD_LABELS[0] in failure for failure in result.failures)


def test_a_failing_install_still_writes_the_record(
    db: sqlite3.Connection,
    old_repo: Path,
    ready_checklist: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By this point the old jobs are down and the snapshot is taken. Losing
    the record of that is losing the only account of an irreversible step
    (review finding on PR #22)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def explode() -> list[str]:
        raise RuntimeError("launchctl refused the new plists")

    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=loaded,
        install=explode,
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert not result.ok
    assert any("launchctl refused" in failure for failure in result.failures)
    log = tmp_path / "data" / "cutover" / "2026-08-10-1200.md"
    assert log.is_file()
    body = log.read_text(encoding="utf-8")
    assert "unloaded" in body
    assert "schedule install FAILED" in body


def test_a_refused_unload_stops_before_the_data_is_touched(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """A half-quiesced system snapshotted mid-write is worse than one that
    never started, so the sequence stops rather than pressing on."""
    monkeypatch_home = tmp_path / "home"
    monkeypatch_home.mkdir()

    def stubborn(args: list[str]) -> tuple[int, str, str]:
        if args[0] == "bootout":
            return (1, "", "Operation not permitted")
        return (0, "", "")

    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=stubborn,
    )
    assert not result.ok
    assert result.snapshot is None
    assert any("stopped after quiesce" in line for line in result.lines)


def test_an_already_unloaded_job_is_not_a_failure(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    monkeypatch_calls: list[str] = []

    def mixed(args: list[str]) -> tuple[int, str, str]:
        if args[0] == "bootout":
            monkeypatch_calls.append(args[1])
            return (3, "", "No such process")
        return (0, "", "")

    result = CutoverResult(executed=True)
    quiesce(OLD_LABELS, mixed, execute=True, result=result)
    assert result.ok
    assert all("was not loaded" in line for line in result.lines)


def test_snapshotting_an_empty_old_repo_fails_rather_than_succeeding_quietly(
    tmp_path: Path,
) -> None:
    result = CutoverResult(executed=True)
    empty = tmp_path / "nothing"
    empty.mkdir()
    snapshot(empty, tmp_path / "dest", execute=True, result=result)
    assert not result.ok
    assert "nothing to snapshot" in result.failures[0]


def test_the_stamp_is_sortable() -> None:
    from datetime import UTC, datetime

    assert utc_stamp(datetime(2026, 8, 10, 9, 5, tzinfo=UTC)) == "2026-08-10-0905"


# --- a failure at every step leaves a known state (spec 037) -----------------


def loaded_ok(args: list[str]) -> tuple[int, str, str]:
    return (0, "", "")


def test_a_filesystem_error_during_snapshot_leaves_a_log_and_a_rollback(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """The defect this spec fixes. copytree raising OSError escaped
    run_cutover entirely: the log was never written, the rollback never ran,
    and the operator was left with the old scheduler stopped, nothing
    installed, no record, and a traceback."""
    agents = agents_dir_with_plists(tmp_path)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("No space left on device")

    with patch.object(cutover_module.shutil, "copytree", explode):
        result = run_cutover(
            db,
            old_root=old_repo,
            stamp="2026-08-10-1200",
            execute=True,
            attested=True,
            checklist_path=ready_checklist,
            launchctl=loaded_ok,
            agents_dir=agents,
        )

    assert not result.ok
    assert any("OSError after quiesce" in failure for failure in result.failures)
    assert any("rolled back: reloaded" in line for line in result.lines)
    log = tmp_path / "data" / "cutover" / "2026-08-10-1200.md"
    assert log.is_file(), "the log must be written on the failing path too"
    assert "FAILED after quiesce" in log.read_text(encoding="utf-8")


def test_an_install_failure_rolls_the_old_jobs_back(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """Old down and new not installed is the one end state that is never
    acceptable, so the old jobs go back up."""

    def explode() -> list[str]:
        raise RuntimeError("launchctl refused")

    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=loaded_ok,
        install=explode,
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert not result.ok
    assert any("rolled back: reloaded" in line for line in result.lines)


def test_a_rollback_with_no_plist_names_the_manual_step(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """The old arrangement is the one the README calls the defect being
    fixed, so assuming the standard directory is exactly the assumption most
    likely to be wrong. A rollback that cannot happen says so."""
    empty = tmp_path / "agents-without-plists"
    empty.mkdir()

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("No space left on device")

    with patch.object(cutover_module.shutil, "copytree", explode):
        result = run_cutover(
            db,
            old_root=old_repo,
            stamp="2026-08-10-1200",
            execute=True,
            attested=True,
            checklist_path=ready_checklist,
            launchctl=loaded_ok,
            agents_dir=empty,
        )

    assert any("cannot roll back" in failure for failure in result.failures)
    assert any("launchctl bootstrap" in failure for failure in result.failures)


def test_a_second_invocation_resumes_rather_than_repeating(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """Cutover is neither idempotent nor repeatable, so a partial failure has
    to be continuable rather than restarted."""
    agents = agents_dir_with_plists(tmp_path)
    attempts: list[str] = []

    def failing_install() -> list[str]:
        attempts.append("install")
        raise RuntimeError("launchctl refused")

    first = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=loaded_ok,
        install=failing_install,
        agents_dir=agents,
    )
    assert not first.ok
    assert cutover_module.progress_path("2026-08-10-1200").is_file()

    snapshots: list[str] = []

    def counting_install() -> list[str]:
        attempts.append("install")
        return ["installed the harrier schedule"]

    def record_copy(*_args: object, **_kwargs: object) -> None:
        snapshots.append("copied")

    with patch.object(cutover_module.shutil, "copytree", record_copy):
        second = run_cutover(
            db,
            old_root=old_repo,
            stamp="2026-08-10-1200",
            execute=True,
            attested=True,
            checklist_path=ready_checklist,
            launchctl=loaded_ok,
            install=counting_install,
            agents_dir=agents,
        )

    assert second.ok, second.failures
    assert snapshots == [], "the completed snapshot step was repeated"
    assert attempts == ["install", "install"]


def test_a_successful_run_clears_the_progress_record(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """Otherwise the next cutover with the same stamp would skip everything."""
    result = run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=loaded_ok,
        install=lambda: ["installed"],
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert result.ok, result.failures
    assert not cutover_module.progress_path("2026-08-10-1200").is_file()


def test_a_quiesce_failure_still_writes_a_log(
    db: sqlite3.Connection, old_repo: Path, ready_checklist: Path, tmp_path: Path
) -> None:
    """Every path out leaves a record, including the earliest one."""

    def refuses(args: list[str]) -> tuple[int, str, str]:
        if args[0] == "bootout":
            return (1, "", "Operation not permitted")
        return (0, "", "")

    run_cutover(
        db,
        old_root=old_repo,
        stamp="2026-08-10-1200",
        execute=True,
        attested=True,
        checklist_path=ready_checklist,
        launchctl=refuses,
        agents_dir=agents_dir_with_plists(tmp_path),
    )
    assert (tmp_path / "data" / "cutover" / "2026-08-10-1200.md").is_file()
