"""Behavior pins for the daily digest (spec 019). The old
send_daily_digest.py had no tests; every pin here is new."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.digest import (
    build_digest,
    ghosted_applications,
    outreach_actions_due,
    parse_target_date,
    run_digest,
)
from harrier.mail.watch import events_path
from harrier.tracker import add_job, set_status, update_fields

TARGET = date(2026, 8, 10)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return connect()


def seed_job(
    conn: sqlite3.Connection,
    company: str,
    *,
    status: str = "prospect",
    fit_score: str = "80",
    added_at: str = "2026-08-01",
    applied_date: str = "",
    next_outreach_action: str = "",
) -> int:
    job_id = add_job(
        conn,
        {
            "company": company,
            "title": "Senior Frontend Engineer",
            "url": f"https://example.test/{company.lower().replace(' ', '-')}",
            "source": "greenhouse",
            "status": "prospect",
            "fit_score": fit_score,
            "added_at": added_at,
        },
    )
    if status == "applied":
        set_status(conn, job_id, "applied", applied_date=applied_date or "2026-08-01")
    elif status != "prospect":
        set_status(conn, job_id, status)
    if next_outreach_action:
        update_fields(conn, job_id, {"next_outreach_action": next_outreach_action})
    return job_id


def write_event(**overrides: object) -> None:
    event: dict[str, object] = {
        "kind": "interview_invite",
        "company": "Example Co",
        "role": "Senior Frontend Engineer",
        "tracker_row": "1",
        "timestamp": "2026-08-10T09:00:00+00:00",
        "next_action": "Reply and confirm interview availability.",
    }
    event.update(overrides)
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event) + "\n")


def test_digest_renders_all_five_sections(db: sqlite3.Connection) -> None:
    seed_job(db, "Fresh Co", added_at="2026-08-10", fit_score="91")
    seed_job(db, "Top Co", status="shortlisted", fit_score="95")
    seed_job(
        db,
        "Due Co",
        status="applied",
        applied_date="2026-08-05",
        next_outreach_action="send first outreach",
    )
    seed_job(db, "Ghosted Co", status="applied", applied_date="2026-07-01")
    write_event()
    digest = build_digest(db, TARGET)
    assert "New prospects today: 1" in digest
    assert "Fresh Co" in digest
    assert "Top 3 prospects" in digest
    assert "Top Co" in digest
    assert "send first outreach (1)" in digest
    assert "applications ghosted" in digest
    assert "Ghosted Co" in digest
    assert "interview invite: Example Co" in digest


def test_dry_run_sends_nothing(db: sqlite3.Connection) -> None:
    def no_send(message: str) -> int:
        raise AssertionError("dry run must never send")

    digest, rc = run_digest(db, TARGET, dry_run=True, send=no_send)
    assert rc == 0
    assert digest.startswith("Daily job digest — 2026-08-10")


def test_live_run_sends_once(db: sqlite3.Connection) -> None:
    sent: list[str] = []

    def capture(message: str) -> int:
        sent.append(message)
        return 0

    digest, rc = run_digest(db, TARGET, dry_run=False, send=capture)
    assert rc == 0
    assert sent == [digest]


def test_legacy_auto_added_note_counts_as_added_at(db: sqlite3.Connection) -> None:
    # Migrated rows can carry the date only in their notes (spec 019).
    from harrier.tracker import update_fields

    job_id = seed_job(db, "Migrated Co", added_at="")
    update_fields(db, job_id, {"notes": "auto_added=2026-08-10; source_label=greenhouse"})
    seeded = seed_job(db, "Seeded Co", added_at="")
    update_fields(db, seeded, {"notes": "tier_a_seed=2026-08-10"})
    digest = build_digest(db, TARGET)
    assert "New prospects today: 2" in digest
    assert "Migrated Co" in digest
    assert "Seeded Co" in digest


def test_malformed_event_kind_is_skipped(db: sqlite3.Connection) -> None:
    # A list or dict kind must not abort the digest on an unhashable
    # membership test (review finding).
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"kind": ["interview_invite"], "timestamp": "2026-08-10T09:00:00+00:00"})
        + "\n"
        + json.dumps({"kind": {"a": 1}, "timestamp": "2026-08-10T09:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    write_event(company="Good Co")
    digest = build_digest(db, TARGET)
    assert "interview invite: Good Co" in digest


def test_ghosted_cutoff_boundary(db: sqlite3.Connection) -> None:
    seed_job(db, "Exactly 21", status="applied", applied_date="2026-07-20")
    seed_job(db, "Twenty Days", status="applied", applied_date="2026-07-21")
    from harrier.tracker import list_jobs

    ghosted = ghosted_applications(list_jobs(db), TARGET)
    assert ghosted == ["Exactly 21"]
    # The rendered label must match the inclusive cutoff.
    assert "≥21d no response" in build_digest(db, TARGET)


def test_outreach_grouping_excludes_wait_states(db: sqlite3.Connection) -> None:
    seed_job(
        db,
        "Waiting Co",
        status="applied",
        applied_date="2026-08-05",
        next_outreach_action="wait for reply",
    )
    seed_job(
        db,
        "Followup Co",
        status="applied",
        applied_date="2026-08-01",
        next_outreach_action="send follow-up",
    )
    from harrier.tracker import list_jobs

    groups = outreach_actions_due(list_jobs(db))
    assert groups == {"send follow-up": ["Followup Co"]}


def test_updates_filter_dedupe_and_order(db: sqlite3.Connection) -> None:
    write_event(company="Older", timestamp="2026-08-10T08:00:00+00:00")
    write_event(company="Newer", timestamp="2026-08-10T10:00:00+00:00")
    write_event(company="Newer", timestamp="2026-08-10T10:00:00+00:00")  # duplicate
    write_event(company="Wrong Day", timestamp="2026-08-09T10:00:00+00:00")
    write_event(company="Confirmation", kind="application_confirmation")
    digest = build_digest(db, TARGET)
    assert digest.index("Newer") < digest.index("Older")
    assert digest.count("Newer") == 1
    assert "Wrong Day" not in digest
    assert "Confirmation" not in digest


def test_legacy_handler_output_prefix_is_tolerated(db: sqlite3.Connection) -> None:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "kind": "assessment",
        "company": "Legacy Co",
        "role": "Senior Frontend Engineer",
        "timestamp": "2026-08-10T09:00:00+00:00",
        "next_action": "Review the assessment.",
    }
    path.write_text(f"prefix HANDLER_OUTPUT: {json.dumps(legacy)}\n", encoding="utf-8")
    digest = build_digest(db, TARGET)
    assert "assessment: Legacy Co" in digest


def test_parse_target_date_default_and_override() -> None:
    assert parse_target_date("2026-08-10") == TARGET
    assert parse_target_date(None) is not None
    with pytest.raises(ValueError):
        parse_target_date("not-a-date")
