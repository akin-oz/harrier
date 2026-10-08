"""Every reader of tracker rows reads one track (spec 092).

Two tracks, built by hand: the second is inserted straight into `tracks`,
because no command creates one until spec 093. Every row is synthetic and
every database sits under `tmp_path` (spec 060).
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.digest import build_digest
from harrier.mail.run import run_watch
from harrier.mail.watch import GmailMessage, read_events
from harrier.scoring.export import export_features
from harrier.screening.reconsider import human_rejected_keys
from harrier.tracker import (
    DuplicateJobError,
    JobNotFoundError,
    add_job,
    get_job,
    list_jobs,
    set_status,
    update_fields,
)
from harrier.tracker.actions import next_up, review_queue
from harrier.tracker.export import export_csv
from harrier.tracker.store import backfill_events, list_events
from harrier.tracks import Scope, default_scope, resolve_scope
from harrier_api.app import create_app
from harrier_cli.main import main

FIRST_ROWS = [
    {
        "company": "Example Co",
        "title": "Senior Frontend Engineer",
        "url": "https://boards.example.com/a/1",
    },
    {"company": "Example Labs", "title": "Staff Engineer", "url": "https://boards.example.com/a/2"},
]
SECOND_ROWS = [
    {
        "company": "Other Works",
        "title": "Product Engineer",
        "url": "https://boards.example.com/b/1",
    },
    {
        "company": "Second Institute",
        "title": "Platform Engineer",
        "url": "https://boards.example.com/b/2",
    },
]


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def two_tracks(data_dir: Path) -> Iterator[tuple[sqlite3.Connection, Scope, Scope]]:
    conn = connect()
    conn.execute(
        "INSERT INTO tracks (id, slug, kind, label) VALUES (2, 'second', 'academic', 'Second')"
    )
    conn.commit()
    first = default_scope(conn)
    second = resolve_scope(conn, "second")
    for row in FIRST_ROWS:
        add_job(conn, row, scope=first)
    for row in SECOND_ROWS:
        add_job(conn, row, scope=second)
    yield conn, first, second
    conn.close()


def ids(rows: list[dict[str, str]]) -> set[str]:
    return {row["id"] for row in rows}


# --- the queue, the API, the digest, the export -------------------------------


def test_queue_never_shows_another_tracks_rows(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, second = two_tracks
    mine = ids(list_jobs(conn, first))
    theirs = ids(list_jobs(conn, second))
    assert mine and theirs and not (mine & theirs)
    assert ids(next_up(conn, first)) == mine
    assert ids(review_queue(conn, first)) == mine
    assert ids(next_up(conn, second)) == theirs

    # The API resolves the default track once per request, so the browser
    # sees the first track and nothing of the second.
    client: TestClient = TestClient(create_app())
    listed = {str(job["id"]) for job in client.get("/jobs").json()}
    queued = {str(job["id"]) for job in client.get("/tracker/queue").json()}
    assert listed == mine
    assert queued == mine


def test_digest_and_export_read_one_track(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope], tmp_path: Path
) -> None:
    conn, first, second = two_tracks
    digest = build_digest(conn, first, date(2026, 8, 3))
    assert "Example Co" in digest
    assert "Other Works" not in digest and "Second Institute" not in digest

    jobs_path, _ = export_csv(conn, first, tmp_path / "export")
    exported = jobs_path.read_text(encoding="utf-8")
    assert "Example Co" in exported
    assert "Other Works" not in exported

    jobs_path, _ = export_csv(conn, second, tmp_path / "export-second")
    exported = jobs_path.read_text(encoding="utf-8")
    assert "Other Works" in exported and "Example Co" not in exported


# --- dedupe across tracks, by-id reads within one ------------------------------


def test_a_url_stored_in_one_track_is_a_duplicate_in_another(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, second = two_tracks
    before = len(list_jobs(conn, second))
    with pytest.raises(DuplicateJobError, match=r"in track job"):
        add_job(
            conn,
            {"company": "Anyone", "title": "Anything", "url": FIRST_ROWS[0]["url"]},
            scope=second,
        )
    add_job(
        conn,
        {"company": "Keyed Co", "title": "Keyed role", "url": "", "external_key": "ext-1"},
        scope=first,
    )
    with pytest.raises(DuplicateJobError, match=r"in track job"):
        add_job(
            conn,
            {"company": "Someone", "title": "Something", "url": "", "external_key": "ext-1"},
            scope=second,
        )
    assert len(list_jobs(conn, second)) == before


def test_a_row_outside_the_scope_is_not_found_by_id(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, second = two_tracks
    other_id = int(list_jobs(conn, second)[0]["id"])
    for read in (
        lambda: get_job(conn, first, other_id),
        lambda: set_status(conn, first, other_id, "shortlisted"),
        lambda: update_fields(conn, first, other_id, {"notes": "touched"}),
        lambda: list_events(conn, first, other_id),
    ):
        with pytest.raises(JobNotFoundError) as refused:
            read()
        assert str(refused.value) == f"no job with id {other_id}"
    untouched = get_job(conn, second, other_id)
    assert untouched["status"] == "prospect" and untouched["notes"] == ""


# --- labels, the mail watch, backfill, reconsider -------------------------------


def test_training_labels_ignore_other_tracks(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, second = two_tracks
    for scope in (first, second):
        for row in list_jobs(conn, scope):
            set_status(conn, scope, int(row["id"]), "shortlisted")
    # Every row the export considered, labelled or excluded, is the scope's:
    # the counts add up to one track's rows, never the file's.
    result = export_features(conn, first)
    assert result.rows + sum(result.excluded.values()) == len(FIRST_ROWS)
    result = export_features(conn, second)
    assert result.rows + sum(result.excluded.values()) == len(SECOND_ROWS)


def test_mail_matching_reads_one_track_and_records_it(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, _ = two_tracks

    def message(identifier: str, company: str) -> GmailMessage:
        return GmailMessage(
            message_id=identifier,
            snippet="",
            sender=f"Recruiter <talent@{company.lower().replace(' ', '')}.example>",
            sender_email=f"talent@{company.lower().replace(' ', '')}.example",
            subject=f"Interview invitation at {company}",
            body_plain=f"We would like to invite you to interview for the role at {company}.",
            timestamp="2026-08-03T10:00:00+00:00",
        )

    messages = [message("m-first", "Example Co"), message("m-second", "Other Works")]
    run_watch(conn, first, dry_run=True, fetch=lambda: messages, send=_send_nothing)
    recorded = {str(event["messageId"]): event for event in read_events().events}
    assert recorded["m-first"]["track"] == "job"
    assert recorded["m-first"]["tracker_row"] in ids(list_jobs(conn, first))
    # The other track's company is not in this scope's rows, so no match.
    assert recorded["m-second"]["tracker_row"] is None
    assert recorded["m-second"]["track"] == ""


def _send_nothing(*_: object) -> int:
    return 0


def test_backfill_reconstructs_one_track(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope],
) -> None:
    conn, first, _ = two_tracks
    # Rows written around the write path, so they have no events.
    for track_id, company in ((1, "Quiet First"), (2, "Quiet Second")):
        conn.execute(
            "INSERT INTO jobs (company, title, url, status, track_id) "
            "VALUES (?, ?, ?, 'prospect', ?)",
            (company, "Engineer", f"https://boards.example.com/quiet/{track_id}", track_id),
        )
    conn.commit()
    counts = backfill_events(conn, first)
    assert sum(counts.values()) == 1
    eventless = conn.execute(
        "SELECT company FROM jobs WHERE id NOT IN (SELECT job_id FROM job_events)"
    ).fetchall()
    assert [row[0] for row in eventless] == ["Quiet Second"]


def test_reconsider_and_batch_evaluation_read_one_track(
    two_tracks: tuple[sqlite3.Connection, Scope, Scope], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn, first, second = two_tracks
    for scope in (first, second):
        row = list_jobs(conn, scope)[0]
        set_status(conn, scope, int(row["id"]), "rejected", rejection_reason="stack")
    protected = human_rejected_keys(conn, first)
    assert any("boards.example.com/a/1" in key for key in protected)
    assert not any("boards.example.com/b/" in key for key in protected)

    import harrier.offers.batch as batch_module
    from harrier.offers.batch import BatchOptions, evaluate_prospects

    looked_at: list[str] = []

    def stub_evaluate(
        conn: sqlite3.Connection, company: str, title: str, url: str, jd_text: str
    ) -> object:
        looked_at.append(url)
        raise batch_module.EvaluationError("stubbed: no evaluator in this test")

    monkeypatch.setattr(batch_module, "evaluate_offer", stub_evaluate)
    summary = evaluate_prospects(conn, second, BatchOptions())
    assert summary.errors == len(looked_at) == 1
    assert looked_at == [
        row["url"] for row in list_jobs(conn, second) if row["status"] == "prospect"
    ]


# --- the default scope is resolved once per entry point -----------------------


def test_the_default_scope_is_resolved_once_per_entry(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed = connect()
    add_job(seed, FIRST_ROWS[0], scope=default_scope(seed))
    seed.close()

    reads: list[str] = []
    real_connect = sqlite3.connect

    def trace(statement: str) -> None:
        if "FROM tracks WHERE id" in statement:
            reads.append(statement)

    def tracing_connect(database: Path, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(database, **kwargs)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing_connect)
    assert main(["review"]) == 0
    capsys.readouterr()
    assert len(reads) == 1, reads

    reads.clear()
    client: TestClient = TestClient(create_app())
    assert client.get("/jobs").status_code == 200
    assert len(reads) == 1, reads
