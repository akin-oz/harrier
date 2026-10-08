"""Every decision on a job is recorded, with who made it and why (spec 079).

The tracker used to keep a job's present and forget its past: `set_status`
overwrote the status, and the candidate's skip, a company's verdict and a
system closure all became the same word in the same column. These tests hold
the history that replaces that, and above all the one rule the history exists
for: a company's verdict is never recorded as the candidate's decision.

Every row here is synthetic: invented companies, invented postings.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.db import connect
from harrier.screening.descriptions import save_description_cache
from harrier.tracker import store as tracker_store
from harrier.tracker.actions import TrackerActionError, change_status, record_company_outcome
from harrier.tracker.reasons import (
    ACTORS,
    COMPANY,
    REASON_CODES,
    UNCLASSIFIED,
    actor_of,
    codes_for,
    infer_code,
    label_of,
)
from harrier.tracker.score import score_fields
from harrier.tracker.store import (
    TrackerError,
    add_job,
    backfill_events,
    get_job,
    list_events,
    set_status,
    update_fields,
)
from harrier.tracks import default_scope
from harrier_api.app import create_app
from harrier_cli.main import main


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return connect()


def _job(conn: sqlite3.Connection, index: int = 1, **fields: str) -> int:
    return add_job(
        conn,
        {
            "company": f"Quillfeather Labs {index}",
            "title": "Senior Frontend Engineer",
            "location": "Remote, Europe",
            "url": f"https://boards.example.com/quillfeather/{index}",
            "source": "greenhouse",
            "fit_score": "90",
            **fields,
        },
        scope=default_scope(conn),
    )


def _count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0])


# --- one event per change, committed with it -----------------------------------


def test_every_status_change_appends_one_event(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    created = list_events(conn, job_id)
    assert [(e["kind"], e["actor"], e["to_status"]) for e in created] == [
        ("created", "system", "prospect")
    ]

    previous = "prospect"
    for status in ("shortlisted", "tailored_cv_requested", "applied", "interviewing", "rejected"):
        before = _count(conn)
        set_status(conn, job_id, status)
        assert _count(conn) == before + 1, f"moving to {status} did not append exactly one event"
        last = list_events(conn, job_id)[-1]
        assert (last["from_status"], last["to_status"]) == (previous, status)
        previous = status


def test_an_event_records_the_score_the_candidate_saw(conn: sqlite3.Connection) -> None:
    """The score and version from before the write, and the description that
    was judged. A later rescore changes the row and never the event."""
    job_id = _job(conn, scoring_version="0a1b2c3d4e5f")
    description = "Remote across Europe. TypeScript and React."
    save_description_cache(get_job(conn, job_id)["url"], description)

    set_status(conn, job_id, "shortlisted")
    decided = list_events(conn, job_id)[-1]
    assert decided["fit_score"] == "90"
    assert decided["scoring_version"] == "0a1b2c3d4e5f"
    assert decided["description_sha256"] == hashlib.sha256(description.encode()).hexdigest()

    update_fields(conn, job_id, score_fields(40, ["rescored"], "ffffffffffff"))
    set_status(conn, job_id, "rejected", rejection_reason="missing stack")
    events = list_events(conn, job_id)
    assert events[-2]["fit_score"] == "90", "a rescore rewrote history"
    assert events[-1]["fit_score"] == "40"
    assert events[-1]["scoring_version"] == "ffffffffffff"


def test_a_damaged_description_file_does_not_block_a_decision(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A cache write cut off mid-character leaves bytes that are not UTF-8.
    The entry reads as missing, as malformed JSON already did, so the move is
    recorded without a digest instead of being refused (spec 079 amendment)."""
    job_id = _job(conn)
    save_description_cache(get_job(conn, job_id)["url"], "Ship the caf\u00e9 ordering app. " * 4)
    [path] = (tmp_path / "data" / "descriptions").glob("*.json")
    written = path.read_bytes()
    path.write_bytes(written[: written.index("\u00e9".encode()) + 1])

    set_status(conn, job_id, "shortlisted")
    assert get_job(conn, job_id)["status"] == "shortlisted"
    assert list_events(conn, job_id)[-1]["description_sha256"] == ""


def test_a_status_change_and_its_event_commit_together(conn: sqlite3.Connection) -> None:
    """Both or neither. A trigger makes the event insert fail; the status
    change it belonged to must not survive it, and neither may a new row."""
    job_id = _job(conn)
    conn.execute(
        "CREATE TEMP TRIGGER refuse_events BEFORE INSERT ON job_events "
        "BEGIN SELECT RAISE(ABORT, 'refused for the test'); END"
    )
    with pytest.raises(sqlite3.DatabaseError, match="refused for the test"):
        set_status(conn, job_id, "shortlisted")
    assert get_job(conn, job_id)["status"] == "prospect"

    jobs_before = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError, match="refused for the test"):
        _job(conn, 2)
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == jobs_before


def test_job_events_is_append_only(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE job_events SET actor = 'company'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM job_events")
    # Nor can the history be orphaned by deleting the job it describes.
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


# --- a company's verdict is never the candidate's decision ---------------------


def test_a_company_verdict_is_never_a_candidate_decision(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    applied = _job(conn, 1)
    other = _job(conn, 2)
    assert main(["applied", str(applied)]) == 0
    assert main(["applied", str(other)]) == 0

    # Recorded through the verb that exists for it, as the company's outcome.
    assert main(["company-outcome", str(applied), "company_rejected"]) == 0
    recorded = list_events(conn, applied)[-1]
    assert (recorded["kind"], recorded["actor"], recorded["reason_code"]) == (
        "outcome",
        "company",
        "company_rejected",
    )
    assert get_job(conn, applied)["status"] == "rejected"

    # Typed as the candidate's rejection, it is refused and the right verb named,
    # whether the code is given or inferred from the words.
    events_before = _count(conn)
    capsys.readouterr()
    assert main(["reject", str(other), "--code", "ghosted"]) == 2
    assert "company-outcome" in capsys.readouterr().err
    assert main(["reject", str(other), "rejected", "by", "company"]) == 2
    assert "company-outcome" in capsys.readouterr().err
    assert get_job(conn, other)["status"] == "applied"
    assert _count(conn) == events_before

    # The database itself refuses the pairing, whatever a caller writes.
    for kind, actor in (("outcome", "candidate"), ("decision", "company")):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO job_events (job_id, kind, actor, to_status) VALUES (?, ?, ?, ?)",
                (applied, kind, actor, "rejected"),
            )

    company_codes = set(codes_for(COMPANY))
    for row in conn.execute("SELECT actor, reason_code FROM job_events"):
        if row["reason_code"] in company_codes:
            assert row["actor"] == "company"


def test_the_interviewing_verb_is_a_company_outcome(conn: sqlite3.Connection) -> None:
    """From the command line and from the browser alike: an interview is
    something the company did, so the old verb cannot file it as the
    candidate's decision."""
    by_cli = _job(conn, 1)
    by_api = _job(conn, 2)
    assert main(["applied", str(by_cli)]) == 0
    assert main(["interviewing", str(by_cli)]) == 0

    client = TestClient(create_app())
    response = client.post(
        f"/tracker/{by_api}/status", json={"verb": "interviewing"}, headers=auth()
    )
    assert response.status_code == 200, response.text

    for job_id in (by_cli, by_api):
        last = list_events(conn, job_id)[-1]
        assert (last["kind"], last["actor"], last["reason_code"]) == (
            "outcome",
            "company",
            "interview_invited",
        )
        assert get_job(conn, job_id)["status"] == "interviewing"


def test_a_company_cannot_reject_an_application_never_sent(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    job_id = _job(conn)
    events_before = _count(conn)
    assert main(["company-outcome", str(job_id), "company_rejected"]) == 2
    assert "no application or invited interview was recorded" in capsys.readouterr().err
    assert get_job(conn, job_id)["status"] == "prospect"
    assert _count(conn) == events_before

    # The candidate's own reason is not a company's response either. Asked on
    # a row the company did engage with, so only the actor check can refuse.
    applied = _job(conn, 2)
    assert main(["applied", str(applied)]) == 0
    capsys.readouterr()
    events_before = _count(conn)
    assert main(["company-outcome", str(applied), "stack"]) == 2
    assert "harrier reject" in capsys.readouterr().err
    assert get_job(conn, applied)["status"] == "applied"
    # Beneath the command, the one writer refuses the same pair.
    with pytest.raises(TrackerError, match="stack is recorded as candidate"):
        set_status(conn, applied, "rejected", reason_code="stack", actor=COMPANY)
    assert _count(conn) == events_before


def test_an_interview_invitation_needs_no_application(conn: sqlite3.Connection) -> None:
    """A recruiter can approach about a job nobody applied to, and
    `harrier.tracker.transitions` keeps that move legal on purpose. The
    application rule is about rejections (spec 079 amendment)."""
    job_id = _job(conn)
    record_company_outcome(conn, str(job_id), "interview_invited", note="recruiter reached out")
    last = list_events(conn, job_id)[-1]
    assert (last["kind"], last["actor"], last["reason_code"]) == (
        "outcome",
        "company",
        "interview_invited",
    )
    assert last["reason_text"] == "recruiter reached out"
    assert get_job(conn, job_id)["status"] == "interviewing"


# --- the reason table -----------------------------------------------------------


def test_a_company_can_answer_the_interview_it_invited(conn: sqlite3.Connection) -> None:
    """An invited interview is engagement, like an application: after a
    recruiter's approach the company can still turn the candidate down, and
    that is its outcome on every path that records it (spec 079 amendment).
    The browser offers this response on an interviewing row (spec 080)."""
    by_verb = _job(conn, 1)
    by_status = _job(conn, 2)
    for job_id in (by_verb, by_status):
        record_company_outcome(conn, str(job_id), "interview_invited")
        assert get_job(conn, job_id)["applied_date"] == ""

    record_company_outcome(conn, str(by_verb), "assessment_failed")
    set_status(conn, by_status, "rejected", reason_code="ghosted")
    for job_id, code in ((by_verb, "assessment_failed"), (by_status, "ghosted")):
        last = list_events(conn, job_id)[-1]
        assert (last["kind"], last["actor"], last["reason_code"]) == ("outcome", "company", code)
        assert get_job(conn, job_id)["status"] == "rejected"


def test_every_reason_code_has_one_actor() -> None:
    owners = {code: actor_of(code) for code in REASON_CODES}
    assert set(owners.values()) <= set(ACTORS)
    partition = [code for actor in ACTORS for code in codes_for(actor)]
    assert sorted(partition) == sorted(REASON_CODES), "a code is in no actor's list, or two"
    # A company rejection is stored on the row as its label, and that label
    # reads back as the same code, so history and inference cannot disagree.
    for code in codes_for(COMPANY):
        if code != "interview_invited":
            assert infer_code(label_of(code)) == code, code


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("rejected by company", "company_rejected"),
        ("Rejected without an offer", "company_rejected"),
        ("ghosted", "ghosted"),
        ("no response from the recruiter", "no_response"),
        ("failed the take-home assignment", "assessment_failed"),
        ("vacancy closed", "vacancy_closed"),
        ("the vacancy is closed", "vacancy_closed"),
        ("auto_reject:vacancy_closed", "vacancy_closed"),
        ("auto_reject:hybrid", "auto_reject"),
        ("ai-evaluation: not remote", "ai_evaluation"),
        ("duplicate", "duplicate"),
        ("application expired", "application_expired"),
        ("hybrid", "not_remote"),
        ("onsite", "not_remote"),
        ("missing stack", "stack"),
        ("location", "location"),
        ("language", "language"),
        ("freelance", "contract_type"),
        ("overqualified", "role_too_junior"),
        ("lack of experience", "role_too_senior"),
        ("timezone", "timezone"),
        ("poor culture", "company"),
        ("salary too low", "compensation"),
        ("equity-only offer", "compensation"),
        ("graduate scheme", "role_too_junior"),
        ("iOS and Swift, not web", "stack"),
        ("niche domain", "company"),
        # A typo that keeps the stem still reads as the word.
        ("languge", "language"),
        # A Turkish keyboard's dotless i, which lower() leaves alone.
        ("hybr\u0131d", "not_remote"),
        # The company's verdict wins over the candidate's own words.
        ("rejected by company, hybrid", "company_rejected"),
        ("", UNCLASSIFIED),
        ("a reason nobody wrote a pattern for", UNCLASSIFIED),
    ],
)
def test_infer_code(text: str, code: str) -> None:
    assert infer_code(text) == code


# --- reopening and the batch evaluator ------------------------------------------


def test_reopening_keeps_the_history(conn: sqlite3.Connection) -> None:
    job_id = _job(conn)
    change_status(conn, str(job_id), "reject", reason="missing stack")
    rejection = list_events(conn, job_id)[-1]

    change_status(conn, str(job_id), "shortlist")
    events = list_events(conn, job_id)
    assert events[-2] == rejection, "reopening rewrote the rejection it reversed"
    assert (events[-1]["kind"], events[-1]["actor"], events[-1]["to_status"]) == (
        "decision",
        "candidate",
        "shortlisted",
    )
    # The row forgets the old reason (spec 036); the history does not.
    assert get_job(conn, job_id)["rejection_reason"] == ""
    assert rejection["reason_text"] == "missing stack"
    assert rejection["reason_code"] == "stack"


# --- backfill ---------------------------------------------------------------------


def _legacy_row(conn: sqlite3.Connection, index: int, **fields: str) -> int:
    """A row as it existed before spec 079: written with no events, the way
    the old tracker's import wrote them."""
    values = {
        "company": f"Ironbark Systems {index}",
        "title": "Product Engineer",
        "url": f"https://jobs.example.org/ironbark/{index}",
        "fit_score": "88",
        "scoring_version": "5e5e5e5e5e5e",
        "added_at": "2026-03-02",
        "created_at": "2026-08-10 17:14:23",
        "updated_at": "2026-08-11 09:00:00",
        **fields,
    }
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    cursor = conn.execute(
        f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", list(values.values())
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _shape(conn: sqlite3.Connection, job_id: int) -> list[tuple[str, str, str, str, str, str]]:
    return [
        (e["at"], e["kind"], e["actor"], e["from_status"], e["to_status"], e["reason_code"])
        for e in list_events(conn, job_id)
    ]


def test_backfill_reconstructs_what_the_row_still_holds(conn: sqlite3.Connection) -> None:
    waiting = _legacy_row(conn, 1)
    company_said_no = _legacy_row(
        conn,
        2,
        status="rejected",
        applied_date="2026-03-05",
        rejection_reason="rejected by company",
    )
    skipped = _legacy_row(conn, 3, status="rejected", rejection_reason="hybrid")
    never_sent = _legacy_row(conn, 4, status="rejected", rejection_reason="ghosted")
    invited = _legacy_row(conn, 5, status="interviewing", applied_date="2026-03-06")
    shortlisted = _legacy_row(conn, 6, status="shortlisted")
    by_hand = _legacy_row(
        conn,
        7,
        manual_added="2026-08-12",
        added_at="2026-08-12",
        created_at="2026-08-12 10:00:00",
        updated_at="2026-08-12 10:00:00",
    )

    dry = backfill_events(conn, dry_run=True)
    assert sum(dry.values()) > 0
    assert _count(conn) == 0, "a dry run wrote events"

    counts = backfill_events(conn)
    assert counts == dry

    arrived = "2026-03-02T00:00:00Z"
    closed = "2026-08-11T09:00:00Z"
    assert _shape(conn, waiting) == [(arrived, "created", "system", "", "prospect", "")]
    assert _shape(conn, company_said_no) == [
        (arrived, "created", "system", "", "prospect", ""),
        ("2026-03-05T00:00:00Z", "decision", "candidate", "prospect", "applied", ""),
        (closed, "outcome", "company", "applied", "rejected", "company_rejected"),
    ]
    assert _shape(conn, skipped)[-1] == (
        closed,
        "decision",
        "candidate",
        "prospect",
        "rejected",
        "not_remote",
    )
    # A company's word on a row nobody applied to cannot be the company's
    # outcome, and it is not the candidate's decision either.
    assert _shape(conn, never_sent)[-1] == (
        closed,
        "decision",
        "unknown",
        "prospect",
        "rejected",
        UNCLASSIFIED,
    )
    assert _shape(conn, invited)[-1] == (
        closed,
        "outcome",
        "company",
        "applied",
        "interviewing",
        "interview_invited",
    )
    assert _shape(conn, shortlisted)[-1] == (
        closed,
        "decision",
        "candidate",
        "prospect",
        "shortlisted",
        "",
    )
    assert _shape(conn, by_hand) == [
        ("2026-08-12T10:00:00Z", "created", "candidate", "", "prospect", ""),
    ]

    # Nothing the row cannot vouch for: no score at decision time, no digest.
    for row in conn.execute("SELECT * FROM job_events"):
        assert row["backfilled"] == 1
        assert row["fit_score"] == row["scoring_version"] == row["description_sha256"] == ""
    assert list_events(conn, skipped)[-1]["reason_text"] == "hybrid"


def test_a_legacy_row_decided_live_keeps_its_history(conn: sqlite3.Connection) -> None:
    """Backfill skips a job with any event, so a pre-079 row's first live move
    used to cost it everything before that move, for good under the
    append-only trigger. Its history is now written first, in the same
    transaction (spec 079 amendment)."""
    job_id = _legacy_row(conn, 1, status="shortlisted")
    assert main(["applied", str(job_id)]) == 0
    assert [
        (e["kind"], e["actor"], e["to_status"], e["backfilled"]) for e in list_events(conn, job_id)
    ] == [
        ("created", "system", "prospect", "1"),
        ("decision", "candidate", "shortlisted", "1"),
        ("decision", "candidate", "applied", "0"),
    ]
    assert not backfill_events(conn), "the live move left history for backfill to find"


def test_backfill_is_idempotent(conn: sqlite3.Connection) -> None:
    _legacy_row(conn, 1, status="rejected", rejection_reason="duplicate")
    live = _job(conn, 2)  # recorded live, so it already has its history
    first = backfill_events(conn)
    total = _count(conn)
    assert first
    assert not backfill_events(conn), "a second run found more to write"
    assert _count(conn) == total
    assert [e["backfilled"] for e in list_events(conn, live)] == ["0"]


def test_the_event_commands_run(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    legacy = _legacy_row(conn, 1, status="rejected", rejection_reason="missing stack")
    assert main(["events", "backfill", "--dry-run"]) == 0
    assert "would write 2 events" in capsys.readouterr().out
    assert _count(conn) == 0
    assert main(["events", "backfill"]) == 0
    assert main(["events", "show", str(legacy)]) == 0
    shown = capsys.readouterr().out
    assert "decision" in shown and "stack" in shown and "[backfilled]" in shown


# --- logs -------------------------------------------------------------------------


def test_event_writes_log_no_reason_text(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    """Ids and codes only. The reason is the candidate's own words, and the
    company and title identify the search."""
    caplog.set_level(logging.DEBUG)
    job_id = add_job(
        conn,
        {
            "company": "Zephyrine Widgetworks",
            "title": "Principal Gizmo Wrangler",
            "url": "https://boards.example.com/zephyrine/1",
        },
        scope=default_scope(conn),
    )
    set_status(conn, job_id, "applied")
    record_company_outcome(conn, str(job_id), "ghosted", note="a private note about the recruiter")
    other = _job(conn, 9)
    change_status(conn, str(other), "reject", reason="my own confidential reasoning")
    _legacy_row(conn, 3, status="rejected", rejection_reason="legacy secret wording")
    backfill_events(conn)

    assert "job event:" in caplog.text, "nothing was logged, so this proves nothing"
    for secret in (
        "Zephyrine",
        "Gizmo Wrangler",
        "private note",
        "confidential reasoning",
        "legacy secret",
        "Quillfeather",
        "Ironbark",
    ):
        assert secret not in caplog.text, secret


# --- review of the fixes (spec 079 amendment) -------------------------------------


@pytest.fixture
def rival(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A second connection to the same database, one that will not wait."""
    other = connect()
    other.execute("PRAGMA busy_timeout=0")
    yield other
    other.close()


def _race_once(rival: sqlite3.Connection, job_id: int, outcome: list[str]) -> None:
    """One competing move from the rival, recorded as getting in or waiting."""
    if outcome:
        return
    outcome.append("started")
    try:
        set_status(rival, job_id, "shortlisted")
        outcome[0] = "interleaved"
    except sqlite3.OperationalError:
        outcome[0] = "waited"


def test_a_first_move_holds_the_lock_from_read_to_write(
    conn: sqlite3.Connection, rival: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writers on a job with no events both read "no events" and both
    wrote its history, for good under the append-only trigger. The row is now
    read under the write lock, so a rival waits for the move; allowed no wait,
    it is turned away."""
    job_id = _legacy_row(conn, 1)
    outcome: list[str] = []
    real = tracker_store._plan_backfill  # pyright: ignore[reportPrivateUsage]

    def racing(job: Any) -> Any:
        _race_once(rival, job_id, outcome)
        return real(job)

    monkeypatch.setattr(tracker_store, "_plan_backfill", racing)
    set_status(conn, job_id, "rejected", rejection_reason="hybrid")
    assert outcome == ["waited"]
    assert [event["kind"] for event in list_events(conn, job_id)].count("created") == 1


def test_backfill_holds_the_lock_from_read_to_write(
    conn: sqlite3.Connection, rival: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same race from the other side: a live first move landing while
    backfill plans the same job."""
    job_id = _legacy_row(conn, 1)
    outcome: list[str] = []
    real = tracker_store._plan_backfill  # pyright: ignore[reportPrivateUsage]

    def racing(job: Any) -> Any:
        _race_once(rival, job_id, outcome)
        return real(job)

    monkeypatch.setattr(tracker_store, "_plan_backfill", racing)
    backfill_events(conn)
    assert outcome == ["waited"]
    assert [event["kind"] for event in list_events(conn, job_id)].count("created") == 1


def test_a_field_update_holds_the_lock_from_read_to_write(
    conn: sqlite3.Connection, rival: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariants a field write is checked against are read under the
    same lock, so they are the row the write lands on."""
    job_id = _job(conn)
    outcome: list[str] = []
    real = tracker_store.all_breaches

    def racing(row: Any) -> Any:
        _race_once(rival, job_id, outcome)
        return real(row)

    monkeypatch.setattr(tracker_store, "all_breaches", racing)
    update_fields(conn, job_id, {"next_action": "call back"})
    assert outcome == ["waited"]


def test_a_company_that_has_responded_has_engaged(conn: sqlite3.Connection) -> None:
    """A recruiter's invite, then the company's first response: the row now
    holds neither an application nor the interviewing status, but the events
    show the company engaged, so a later response is still its outcome, on
    every path."""
    by_verb = _job(conn, 1)
    by_status = _job(conn, 2)
    for job_id in (by_verb, by_status):
        record_company_outcome(conn, str(job_id), "interview_invited")
        record_company_outcome(conn, str(job_id), "ghosted")
    record_company_outcome(conn, str(by_verb), "company_rejected")
    set_status(conn, by_status, "rejected", reason_code="assessment_failed")
    for job_id, code in ((by_verb, "company_rejected"), (by_status, "assessment_failed")):
        last = list_events(conn, job_id)[-1]
        assert (last["kind"], last["actor"], last["reason_code"]) == ("outcome", "company", code)


@pytest.mark.parametrize("steps", [[], ["shortlisted"], ["shortlisted", "tailored_cv_requested"]])
def test_the_candidate_moving_a_job_is_not_the_company_engaging(
    conn: sqlite3.Connection, steps: list[str]
) -> None:
    """Shortlisting and asking for a tailored CV are the candidate's side.
    With no application, invited interview or earlier response, no company
    has seen the job: its verdict is refused by the command and filed as
    unknown by the status writer."""
    job_id = _job(conn)
    for status in steps:
        set_status(conn, job_id, status)
    events_before = _count(conn)
    with pytest.raises(TrackerActionError):
        record_company_outcome(conn, str(job_id), "ghosted")
    assert _count(conn) == events_before
    set_status(conn, job_id, "rejected", reason_code="ghosted")
    last = list_events(conn, job_id)[-1]
    assert (last["kind"], last["actor"], last["reason_code"]) == (
        "decision",
        "unknown",
        UNCLASSIFIED,
    )


def test_a_description_no_encoding_can_write_reads_as_missing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Valid JSON can carry a lone surrogate, which no text encodes, and
    hashing it raised inside the status writer. Only an outside writer can
    leave one; it reads as missing, like any other damage."""
    job_id = _job(conn)
    url = get_job(conn, job_id)["url"]
    save_description_cache(url, "A posting long enough to be cached here.")
    [path] = (tmp_path / "data" / "descriptions").glob("*.json")
    path.write_text(json.dumps({"url": url, "description": "Broken \ud83d text"}))
    set_status(conn, job_id, "shortlisted")
    assert list_events(conn, job_id)[-1]["description_sha256"] == ""
