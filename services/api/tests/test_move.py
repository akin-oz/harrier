"""A row moves between the industry and focused tracks (spec 138).

Every database is built under `tmp_path`. Every track, company and posting is
synthetic (spec 060, ADR-008); the focused track's screening document is the
committed example's.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import harrier.tracker.store as store
from harrier.connection import Connection
from harrier.db import connect, default_db_path
from harrier.screening.focused import focused_score, load_screening
from harrier.screening.normalized import make_normalized_job
from harrier.tracker import list_jobs
from harrier.tracker.reasons import MOVED
from harrier.tracker.store import list_events
from harrier.tracks import default_scope, resolve_scope
from harrier_cli.main import main

FOCUSED = "bounded"
EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "screening.focused.example.json"
POSTING = "Build interfaces from Figma designs with React and TypeScript."
REMOTE_EMEA = "Remote, Europe"


@pytest.fixture()
def tracks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[Path]:
    """The default industry track and a configured focused track."""
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    assert main(["tracks", "add", FOCUSED, "--kind", "focused", "--label", "Bounded"]) == 0
    assert main(["--track", FOCUSED, "profile", "put", "screening", "--file", str(EXAMPLE)]) == 0
    capsys.readouterr()
    yield directory


def add_industry(company: str, *, description: str = POSTING, location: str = REMOTE_EMEA) -> int:
    argv = ["add", "--company", company, "--title", "Junior Frontend Developer"]
    argv += ["--location", location, "--url", url_for(company)]
    if description:
        argv += ["--description", description]
    assert main(argv) == 0
    return int(row(company, None)["id"])


def url_for(company: str) -> str:
    return f"https://jobs.example.com/{company.lower().replace(' ', '-')}"


def rows(slug: str | None) -> list[dict[str, str]]:
    conn = connect()
    try:
        scope = default_scope(conn) if slug is None else resolve_scope(conn, slug)
        return list_jobs(conn, scope)
    finally:
        conn.close()


def row(company: str, slug: str | None) -> dict[str, str]:
    found = [job for job in rows(slug) if job["company"] == company]
    assert len(found) == 1, found
    return found[0]


def events(job_id: int, slug: str | None) -> list[dict[str, str]]:
    conn = connect()
    try:
        scope = default_scope(conn) if slug is None else resolve_scope(conn, slug)
        return list_events(conn, scope, job_id)
    finally:
        conn.close()


def move(job_id: int, to: str, *, source: str | None = None) -> int:
    argv = [] if source is None else ["--track", source]
    return main([*argv, "move", str(job_id), "--to", to])


# --- the move ------------------------------------------------------------------


def test_a_move_keeps_status_dates_and_history(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_industry("Example Studio")
    assert main(["applied", str(job_id), "--applied-date", "2026-10-01"]) == 0
    before = row("Example Studio", None)
    history = events(job_id, None)
    capsys.readouterr()

    assert move(job_id, FOCUSED) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"moved {job_id} from job to {FOCUSED}, score ")

    after = row("Example Studio", FOCUSED)
    for name in ("status", "applied_date", "last_contact", "next_action", "notes"):
        assert after[name] == before[name], name
    for name in ("outreach_status", "next_outreach_action", "outreach_priority"):
        assert after[name] == before[name], name
    moved = events(job_id, FOCUSED)
    assert moved[: len(history)] == history
    last = moved[-1]
    assert len(moved) == len(history) + 1
    assert (last["kind"], last["actor"], last["reason_code"]) == ("decision", "candidate", MOVED)
    assert last["from_status"] == last["to_status"] == "applied"
    assert last["reason_text"] == f"moved from job to {FOCUSED}"
    # `next` lists active rows; `review` only those still awaiting a decision.
    assert main(["--track", FOCUSED, "next"]) == 0
    assert "Example Studio" in capsys.readouterr().out


def test_a_move_rescores_from_the_cached_description(tracks: Path) -> None:
    job_id = add_industry("Example Studio")
    assert move(job_id, FOCUSED) == 0
    moved = row("Example Studio", FOCUSED)
    conn: Connection = connect()
    try:
        document = load_screening(conn, resolve_scope(conn, FOCUSED))
    finally:
        conn.close()
    assert document is not None
    expected = focused_score(
        make_normalized_job(
            source="manual",
            company="Example Studio",
            title="Junior Frontend Developer",
            location=REMOTE_EMEA,
            url=url_for("Example Studio"),
            description=POSTING,
        ),
        document,
    )
    assert moved["fit_score"] == str(expected.score)
    assert moved["scoring_version"] == expected.version
    assert moved["signals"].split("|")[0] == "scorer=focused"
    last = events(job_id, FOCUSED)[-1]
    assert (last["fit_score"], last["scoring_version"]) == (str(expected.score), expected.version)


def test_a_move_without_a_description_keeps_the_score(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_industry("Quiet Co", description="")
    before = row("Quiet Co", None)
    capsys.readouterr()
    assert move(job_id, FOCUSED) == 0
    out = capsys.readouterr().out
    assert "not rescored: no cached description" in out
    after = row("Quiet Co", FOCUSED)
    assert (after["fit_score"], after["scoring_version"], after["signals"]) == (
        before["fit_score"],
        before["scoring_version"],
        before["signals"],
    )
    last = events(job_id, FOCUSED)[-1]
    assert (
        last["reason_text"] == f"moved from job to {FOCUSED}; not rescored: no cached description"
    )
    assert last["fit_score"] == before["fit_score"]


def test_a_move_names_a_failed_destination_gate(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_industry("Office Co", location="Amsterdam (On-site)")
    capsys.readouterr()
    assert move(job_id, FOCUSED) == 0
    assert f"fails a {FOCUSED} gate: location says hybrid/on-site" in capsys.readouterr().err
    last = events(job_id, FOCUSED)[-1]
    assert last["reason_text"].endswith("failed gate: location says hybrid/on-site")
    assert row("Office Co", FOCUSED)["status"] == "prospect"


def test_a_move_back_is_its_own_move(tracks: Path) -> None:
    job_id = add_industry("Example Studio")
    assert move(job_id, FOCUSED) == 0
    assert move(job_id, "job", source=FOCUSED) == 0
    back = row("Example Studio", None)
    assert back["signals"].split("|")[0] != "scorer=focused"
    codes = [event["reason_code"] for event in events(job_id, None)]
    assert codes.count(MOVED) == 2


# --- refusals ------------------------------------------------------------------


def _unchanged(job_id: int, company: str) -> None:
    assert row(company, None)["id"] == str(job_id)
    assert all(event["reason_code"] != MOVED for event in events(job_id, None))


def test_a_move_involving_an_academic_track_is_refused(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "add", "lab", "--kind", "academic", "--label", "Lab"]) == 0
    job_id = add_industry("Example Studio")
    capsys.readouterr()
    assert move(job_id, "lab") == 2
    assert "between an industry and a focused track" in capsys.readouterr().err
    _unchanged(job_id, "Example Studio")


def test_a_move_to_the_same_track_is_refused(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_industry("Example Studio")
    capsys.readouterr()
    assert move(job_id, "job") == 2
    assert "already on track job" in capsys.readouterr().err
    _unchanged(job_id, "Example Studio")


def test_a_move_to_an_archived_track_is_refused(
    tracks: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = add_industry("Example Studio")
    assert main(["tracks", "archive", FOCUSED]) == 0
    capsys.readouterr()
    assert move(job_id, FOCUSED) == 2
    assert "archived" in capsys.readouterr().err
    _unchanged(job_id, "Example Studio")


def test_a_move_to_an_unconfigured_focused_track_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 138, amendment 1: the destination's scorer has nothing to score
    with, as a focused `add` has nothing (spec 137)."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    assert main(["tracks", "add", FOCUSED, "--kind", "focused", "--label", "Bounded"]) == 0
    job_id = add_industry("Example Studio")
    capsys.readouterr()
    assert move(job_id, FOCUSED) == 2
    assert f"track {FOCUSED} has no screening configuration" in capsys.readouterr().err
    _unchanged(job_id, "Example Studio")


def test_a_move_reads_only_the_scopes_row(tracks: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job_id = add_industry("Example Studio")
    capsys.readouterr()
    assert move(job_id, "job", source=FOCUSED) == 1
    assert f"no job {job_id} on track {FOCUSED}" in capsys.readouterr().err
    _unchanged(job_id, "Example Studio")


def test_a_row_moved_underneath_is_a_conflict(
    tracks: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another writer moves the row between the read and the write: the
    guarded update matches nothing and nothing is written."""
    job_id = add_industry("Example Studio")
    focused_id = _focused_id()
    real = store._rescore_for  # pyright: ignore[reportPrivateUsage]

    def moved_meanwhile(conn: Any, destination: Any, before: Any) -> Any:
        result = real(conn, destination, before)
        other = sqlite3.connect(default_db_path())
        with other:
            other.execute("UPDATE jobs SET track_id = ? WHERE id = ?", (focused_id, job_id))
        other.close()
        return result

    monkeypatch.setattr(store, "_rescore_for", moved_meanwhile)
    before = row("Example Studio", None)
    capsys.readouterr()
    assert move(job_id, FOCUSED) == 1
    assert f"job {job_id} changed while moving; run it again" in capsys.readouterr().err
    after = row("Example Studio", FOCUSED)
    assert (after["fit_score"], after["signals"]) == (before["fit_score"], before["signals"])
    assert all(event["reason_code"] != MOVED for event in events(job_id, FOCUSED))


def _focused_id() -> int:
    conn = connect()
    try:
        return resolve_scope(conn, FOCUSED).track.id
    finally:
        conn.close()


def test_a_move_leaves_other_rows_alone(tracks: Path, capsys: pytest.CaptureFixture[str]) -> None:
    moving = add_industry("Example Studio")
    add_industry("Staying Co")
    staying_before = row("Staying Co", None)
    assert move(moving, FOCUSED) == 0
    assert [job["company"] for job in rows(None)] == ["Staying Co"]
    assert row("Staying Co", None) == staying_before
    capsys.readouterr()
    assert main(["review"]) == 0
    out = capsys.readouterr().out
    assert "Staying Co" in out
    assert "Example Studio" not in out
