"""Write-path behavior pins (spec 004)."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.tracker import (
    NEXT_ACTION_DEFAULTS,
    DuplicateJobError,
    UnknownStatusError,
    add_job,
    get_job,
    list_jobs,
    set_status,
    update_fields,
)


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = connect(tmp_path / "t.db")
    yield connection
    connection.close()


def _job(**overrides: str) -> dict[str, str]:
    base = {
        "company": "Acme",
        "title": "Senior Frontend Engineer",
        "location": "Remote, Europe",
        "url": "https://boards.example.com/acme/1",
        "source": "greenhouse",
        "added_at": "2026-08-01",
        "status": "prospect",
        "notes": "score=78; archetype=product_engineer; external_key=gh:acme:1",
    }
    base.update(overrides)
    return base


def test_add_job_expands_notes_and_defaults_next_action(conn: sqlite3.Connection) -> None:
    job_id = add_job(conn, _job())
    job = get_job(conn, job_id)
    assert job["score"] == "78"
    assert job["archetype"] == "product_engineer"
    assert job["external_key"] == "gh:acme:1"
    assert job["notes"].startswith("score=78")  # original text preserved
    assert job["next_action"] == NEXT_ACTION_DEFAULTS["prospect"]


def test_add_job_rejects_duplicates_by_url_key_and_company_title(conn: sqlite3.Connection) -> None:
    add_job(conn, _job())
    with pytest.raises(DuplicateJobError):
        add_job(conn, _job(notes=""))  # same url
    with pytest.raises(DuplicateJobError):
        add_job(conn, _job(url="https://other.example.com/x", notes="external_key=gh:acme:1"))
    with pytest.raises(DuplicateJobError):
        add_job(conn, _job(url="https://other.example.com/y", notes="", company="acme"))


def test_unknown_status_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(UnknownStatusError):
        add_job(conn, _job(status="in_progress"))
    job_id = add_job(conn, _job())
    with pytest.raises(UnknownStatusError):
        set_status(conn, job_id, "ghosted")


def test_applied_seeds_outreach_block(conn: sqlite3.Connection) -> None:
    job_id = add_job(conn, _job())
    job = set_status(conn, job_id, "applied", applied_date="2026-08-08")
    assert job["status"] == "applied"
    assert job["applied_date"] == "2026-08-08"
    assert job["last_contact"] == "2026-08-08"
    assert job["next_action"] == "follow up if no reply by 2026-08-15"
    assert job["outreach_status"] == "needs_contacts"
    assert job["next_outreach_action"] == "find contacts"
    assert job["contacts_found"] == "0"
    assert job["outreach_priority"] == "high"


def test_applied_fills_only_blank_outreach_fields(conn: sqlite3.Connection) -> None:
    job_id = add_job(conn, _job())
    update_fields(conn, job_id, {"outreach_status": "ready", "outreach_priority": "low"})
    job = set_status(conn, job_id, "applied", applied_date="2026-08-08")
    assert job["outreach_status"] == "ready"
    assert job["outreach_priority"] == "low"
    assert job["next_outreach_action"] == "find contacts"


def test_rejected_records_reason_and_clears_next_action(conn: sqlite3.Connection) -> None:
    job_id = add_job(conn, _job())
    job = set_status(conn, job_id, "rejected", rejection_reason="remote policy changed")
    assert job["rejection_reason"] == "remote policy changed"
    assert job["next_action"] == ""


def test_update_fields_refuses_status(conn: sqlite3.Connection) -> None:
    job_id = add_job(conn, _job())
    with pytest.raises(Exception, match="set_status"):
        update_fields(conn, job_id, {"status": "applied"})


def test_list_jobs_filters(conn: sqlite3.Connection) -> None:
    a = add_job(conn, _job())
    add_job(
        conn,
        _job(url="https://boards.example.com/beta/2", company="Beta", title="Staff FE", notes=""),
    )
    set_status(conn, a, "shortlisted")
    assert len(list_jobs(conn)) == 2
    assert [j["company"] for j in list_jobs(conn, status="shortlisted")] == ["Acme"]
    assert len(list_jobs(conn, source="greenhouse")) == 2
