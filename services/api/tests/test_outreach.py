"""Behavior pins for contacts, staged discovery, and the outreach queue
(spec 016), ported from the old repo's tests/test_find_contacts.py and
tests/test_outreach_lib.py onto the database store."""

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import harrier.outreach.discovery as discovery_module
from harrier.db import connect
from harrier.outreach import (
    approve_candidate,
    build_search_specs,
    contacts_for_job,
    filter_outreach_rows,
    find_best_contacts_for_job,
    find_contacts_for_job,
    infer_relevance,
    load_candidates_artifact,
    merge_ranked_contacts,
    normalize_profile_result,
    parse_linked_jobs,
    refresh_outreach_fields,
    score_contact_fit,
    upsert_contact,
    write_candidates_artifact,
)
from harrier.outreach.discovery import DEFAULT_ACTOR
from harrier.tracker import list_contacts

SAMPLE_ITEM: dict[str, Any] = {
    "fullName": "Jane Recruiter",
    "headline": "Senior Technical Recruiter",
    "linkedinUrl": "https://www.linkedin.com/in/jane-recruiter/",
    "location": {"linkedinText": "Lisbon, Portugal"},
    "snippet": "Hiring frontend engineers across Europe.",
    "currentCompany": "Remote",
}
RECRUITER_SPEC = {"query": '"Remote" recruiter', "target_relevance": "recruiter"}


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return connect()


# ---------------------------------------------------------------------------
# Discovery: specs, normalization, scoring, dedupe
# ---------------------------------------------------------------------------


def test_build_search_specs_for_frontend_role() -> None:
    specs = build_search_specs("Remote", "Senior Frontend Engineer")
    queries = [spec["query"] for spec in specs]
    assert '"Remote" recruiter' in queries
    assert '"Remote" "talent acquisition"' in queries
    assert any("frontend engineering manager" in query for query in queries)


def test_normalize_profile_result_separates_applied_job_title_and_person_title() -> None:
    normalized = normalize_profile_result(
        SAMPLE_ITEM,
        company="Remote",
        role="Senior Frontend Engineer",
        job_url="https://example.com/job",
        spec=RECRUITER_SPEC,
    )
    assert normalized["applied_job_title"] == "Senior Frontend Engineer"
    assert normalized["person_title"] == "Senior Technical Recruiter"
    assert normalized["location"] == "Lisbon, Portugal"
    assert normalized["relevance"] == "recruiter"
    assert int(normalized["fit_score"]) >= 60
    assert normalized["fit_reason"]
    assert normalized["review_status"] == "pending"


def test_fit_score_ranking_prefers_hiring_side_match_over_generic_hr() -> None:
    strong = score_contact_fit(
        company="Remote",
        applied_job_title="Senior Frontend Engineer",
        person_title="Head of Frontend",
        location="Remote, Europe",
        snippet="Leading frontend hiring across Europe",
        current_company="Remote",
        target_relevance="hiring_manager",
    )
    weak = score_contact_fit(
        company="Remote",
        applied_job_title="Senior Frontend Engineer",
        person_title="People Operations Generalist",
        location="Austin, United States",
        snippet="HR operations and onboarding",
        current_company="Remote",
        target_relevance="recruiter",
    )
    assert int(str(strong["fit_score"])) > int(str(weak["fit_score"]))
    assert strong["relevance"] == "hiring_manager"
    assert "generic HR" in str(weak["fit_reason"])


def test_merge_ranked_contacts_dedupes_same_profile_by_best_fit_score() -> None:
    merged = merge_ranked_contacts(
        [
            {
                "person_name": "Jane Recruiter",
                "linkedin_url": "https://www.linkedin.com/in/jane-recruiter/",
                "relevance": "recruiter",
                "fit_score": "74",
                "fit_reason": "reason=a",
            },
            {
                "person_name": "Jane Recruiter",
                "linkedin_url": "https://www.linkedin.com/in/jane-recruiter/",
                "relevance": "recruiter",
                "fit_score": "66",
                "fit_reason": "reason=b",
            },
        ]
    )
    assert len(merged) == 1
    assert merged[0]["fit_score"] == "74"
    assert "reason=a" in merged[0]["fit_reason"]


def test_find_contacts_requires_apify_token(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discovery_module, "env_config", lambda: {"token": "", "actor": DEFAULT_ACTOR}
    )
    with pytest.raises(RuntimeError, match="missing APIFY_TOKEN"):
        find_contacts_for_job(
            company="Remote", role="Senior Frontend Engineer", job_url="https://example.com/job"
        )


def test_find_contacts_stages_candidates_without_writing_contacts(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discovery_module, "env_config", lambda: {"token": "token", "actor": DEFAULT_ACTOR}
    )

    def fake_search(
        query: str, token: str, actor: str, max_items: int, timeout_seconds: int
    ) -> list[dict[str, Any]]:
        return [SAMPLE_ITEM]

    monkeypatch.setattr(discovery_module, "apify_profile_search", fake_search)
    summary = find_contacts_for_job(
        company="Remote", role="Senior Frontend Engineer", job_url="https://example.com/job"
    )
    assert summary["candidate_count"] == 1
    assert summary["raw_item_count"] == 5
    assert len(list(summary["search_results"])) == 5  # type: ignore[arg-type]
    # The staging discipline: no contact was written.
    assert list_contacts(db) == []
    assert Path(str(summary["artifact_path"])).exists()


def test_find_best_contacts_stops_after_strong_recruiter_match(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        discovery_module, "env_config", lambda: {"token": "token", "actor": DEFAULT_ACTOR}
    )
    calls: list[str] = []

    def fake_search(
        query: str, token: str, actor: str, max_items: int, timeout_seconds: int
    ) -> list[dict[str, Any]]:
        calls.append(query)
        return [SAMPLE_ITEM]

    monkeypatch.setattr(discovery_module, "apify_profile_search", fake_search)
    summary = find_best_contacts_for_job(
        company="Remote",
        role="Senior Frontend Engineer",
        job_url="https://example.com/job",
        max_items=4,
    )
    assert calls == ['"Remote" recruiter']
    assert summary["search_mode"] == "best_contact_only"
    assert summary["searches_run"] == 1
    assert summary["max_items_per_search"] == 4


def test_approve_adds_candidate_to_contacts(db: sqlite3.Connection) -> None:
    payload: dict[str, object] = {
        "company": "Remote",
        "role": "Senior Frontend Engineer",
        "job_url": "https://example.com/job",
        "candidates": [
            {
                "person_name": "Jane Recruiter",
                "company": "Remote",
                "applied_job_title": "Senior Frontend Engineer",
                "person_title": "Senior Technical Recruiter",
                "relevance": "recruiter",
                "fit_score": "72",
                "fit_reason": "same company match; direct recruiting title",
                "location": "Lisbon, Portugal",
                "source": "apify_profile_search",
                "linkedin_url": "https://www.linkedin.com/in/jane-recruiter/",
                "job_url": "https://example.com/job",
                "review_status": "pending",
                "raw_query": '"Remote" recruiter',
            }
        ],
    }
    write_candidates_artifact("Remote", "Senior Frontend Engineer", payload)
    added = approve_candidate(
        db,
        "Remote",
        "Senior Frontend Engineer",
        "https://example.com/job",
        "https://www.linkedin.com/in/jane-recruiter/",
    )
    assert added is not None
    stored = list_contacts(db)
    assert len(stored) == 1
    assert stored[0]["applied_job_title"] == "Senior Frontend Engineer"
    assert stored[0]["person_title"] == "Senior Technical Recruiter"
    assert stored[0]["fit_score"] == "72"
    refreshed = load_candidates_artifact("Remote", "Senior Frontend Engineer")
    assert refreshed is not None
    candidates = refreshed["candidates"]
    assert isinstance(candidates, list)
    from typing import cast as _cast

    first = _cast("dict[str, str]", _cast("list[object]", candidates)[0])
    assert first["review_status"] == "approved"


# ---------------------------------------------------------------------------
# Contacts store
# ---------------------------------------------------------------------------


def test_infer_relevance() -> None:
    assert infer_relevance("Senior Recruiter") == "recruiter"
    assert infer_relevance("Engineering Manager") == "eng_manager"
    assert infer_relevance("Co-Founder") == "founder_cto"


def test_upsert_contact_merges_same_person_across_multiple_jobs(
    db: sqlite3.Connection,
) -> None:
    upsert_contact(
        db,
        company="Acme",
        role="Senior Frontend Engineer",
        job_url="https://example.com/jobs/1",
        person_name="Jane Recruiter",
        person_title="Recruiter",
        linkedin_url="https://linkedin.com/in/jane",
        source="manual",
        relevance="recruiter",
    )
    upsert_contact(
        db,
        company="Beta",
        role="Senior Frontend Engineer",
        job_url="https://example.com/jobs/2",
        person_name="Jane Recruiter",
        person_title="Recruiter",
        linkedin_url="https://linkedin.com/in/jane",
        source="manual",
        relevance="recruiter",
    )
    contacts = list_contacts(db)
    assert len(contacts) == 1
    linked_jobs = parse_linked_jobs(contacts[0]["linked_jobs"])
    assert len(linked_jobs) == 2
    assert {item["company"] for item in linked_jobs} == {"Acme", "Beta"}
    matched = contacts_for_job(contacts, "Acme", "Senior Frontend Engineer", "")
    assert len(matched) == 1


# ---------------------------------------------------------------------------
# Outreach state machine
# ---------------------------------------------------------------------------

JANE = {
    "person_name": "Jane Recruiter",
    "person_title": "Recruiter",
    "linkedin_url": "https://linkedin.com/in/jane",
    "relevance": "recruiter",
}


def test_refresh_outreach_fields_requires_contacts_after_apply() -> None:
    row = {
        "status": "applied",
        "applied_date": "2026-03-17",
        "outreach_status": "",
        "last_outreach_at": "",
        "next_outreach_action": "",
        "next_action": "follow up if no reply by 2026-03-24",
    }
    refresh_outreach_fields(row, [])
    assert row["outreach_status"] == "needs_contacts"
    assert row["next_outreach_action"] == "find contacts"
    assert row["next_action"] == "find contacts for outreach"
    assert row["outreach_priority"] == "high"


def test_refresh_outreach_fields_suggests_first_outreach_after_business_days() -> None:
    row = {
        "status": "applied",
        "applied_date": "2020-01-01",
        "outreach_status": "needs_contacts",
        "last_outreach_at": "",
        "next_outreach_action": "",
        "next_action": "follow up if no reply by 2020-01-08",
    }
    refresh_outreach_fields(row, [dict(JANE)])
    assert row["outreach_status"] == "ready"
    assert row["next_outreach_action"] == "send first outreach"
    assert row["next_action"] == "send first outreach"


def test_refresh_outreach_fields_waits_for_reply_with_business_day_math() -> None:
    recent = (date.today() - timedelta(days=1)).isoformat()
    row = {
        "status": "applied",
        "applied_date": "2026-03-17",
        "outreach_status": "sent",
        "last_outreach_at": recent,
        "next_outreach_action": "",
        "next_action": "follow up if no reply by 2026-03-24",
    }
    refresh_outreach_fields(row, [dict(JANE)])
    assert row["next_outreach_action"] == "wait for reply"
    assert row["next_action"].startswith("wait for outreach reply until ")


def test_filter_outreach_rows_due_only_excludes_waiting_rows() -> None:
    rows = [
        {"status": "applied", "next_outreach_action": "send first outreach", "company": "Ready"},
        {"status": "applied", "next_outreach_action": "find contacts", "company": "NeedsContacts"},
        {"status": "applied", "next_outreach_action": "wait for reply", "company": "AlreadySent"},
        {
            "status": "applied",
            "next_outreach_action": "wait until outreach window",
            "company": "WaitingWindow",
        },
        {"status": "prospect", "next_outreach_action": "find contacts", "company": "Prospect"},
    ]
    due = filter_outreach_rows(rows, due_only=True)
    assert [row["company"] for row in due] == ["Ready", "NeedsContacts"]
    everything = filter_outreach_rows(rows, due_only=False)
    assert [row["company"] for row in everything] == [
        "Ready",
        "NeedsContacts",
        "AlreadySent",
        "WaitingWindow",
    ]
