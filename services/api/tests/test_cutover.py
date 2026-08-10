"""The cutover (spec 024).

Every effect is injected, so this exercises the sequence without a cutover
ever happening. That is the point: the real thing runs once, and it has to
be right the first time.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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
    assert result.ok
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
