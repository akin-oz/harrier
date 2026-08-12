"""What a tracker row may not say about itself (spec 036).

Three illegal states were reachable through the sanctioned write path, and
each makes a row lie about its own history. The tests here are about the
states rather than about the verbs, because the states were reachable from
more than one verb: `set_status` stamped an applied date and `update_fields`
could then clear it, so an invariant enforced in one place was an invariant
the other had never heard of.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.tracker.invariants import check_rows, invariant_breach
from harrier.tracker.schema import STATUSES
from harrier.tracker.store import TrackerError, add_job, get_job, set_status, update_fields
from harrier.tracker.transitions import transition_allowed
from harrier_cli.main import main


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    add_job(
        conn,
        {
            "company": "Example Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/example/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
    )
    return conn


# --- every status stays reachable ---------------------------------------------


def test_every_status_is_reachable_from_every_wrong_one() -> None:
    """The half of this spec worth guarding.

    The failure a transition table can introduce is being too strict to fix a
    misclick, and it would be found by someone whose interview is already
    booked. The first implementation refused forward skips, which also refused
    a recruiter approach taking a prospect straight to interviewing: a real
    event, not an error. See the module docstring in `transitions.py`.
    """
    for current in STATUSES:
        for target in STATUSES:
            assert transition_allowed(current, target), f"{current} to {target} was refused"


def test_an_unknown_status_is_still_refused(db: sqlite3.Connection) -> None:
    from harrier.tracker.store import UnknownStatusError

    with pytest.raises(UnknownStatusError):
        set_status(db, 1, "promoted")


# --- applied carries its date -------------------------------------------------


def test_applied_cannot_lose_its_date_through_the_generic_update(db: sqlite3.Connection) -> None:
    """The path that made the invariant escapable.

    `set_status` stamps the date, and then `update_fields` could clear it,
    leaving a row that says it was applied to on no particular day.
    """
    set_status(db, 1, "applied")
    assert get_job(db, 1)["applied_date"] != ""
    with pytest.raises(TrackerError, match="must carry the date"):
        update_fields(db, 1, {"applied_date": ""})
    assert get_job(db, 1)["applied_date"] != ""


def test_the_breach_is_named_the_same_way_wherever_it_is_found() -> None:
    """One statement of what is wrong, so the refusal and the report agree."""
    row = {"status": "applied", "applied_date": ""}
    assert "must carry the date" in invariant_breach(row)
    assert check_rows([{**row, "id": "7"}]) == [("7", invariant_breach(row))]


# --- leaving rejected drops the reason ----------------------------------------


def test_leaving_rejected_clears_the_rejection_reason(db: sqlite3.Connection) -> None:
    """A resurrected job carrying the reason it was rejected reads as though
    it were rejected again for that reason. Only the rejecting branch ever
    touched the field, so it survived forever."""
    set_status(db, 1, "rejected", rejection_reason="wrong stack")
    assert get_job(db, 1)["rejection_reason"] == "wrong stack"
    set_status(db, 1, "shortlisted")
    assert get_job(db, 1)["rejection_reason"] == ""


def test_a_rejection_reason_cannot_be_added_to_a_live_job(db: sqlite3.Connection) -> None:
    set_status(db, 1, "shortlisted")
    with pytest.raises(TrackerError, match="must not carry a rejection reason"):
        update_fields(db, 1, {"rejection_reason": "changed my mind"})


# --- the outreach axis ---------------------------------------------------------


def test_walking_back_past_applied_resets_the_outreach_axis(db: sqlite3.Connection) -> None:
    """The invariant the code contradicted.

    Outreach is documented as orthogonal to the pipeline status, and it was
    coupled in one direction only: seeded on applied, never reset. A job
    walked back to prospect still said contact had been sent. This spec keeps
    the orthogonality claim and makes the code honour it, rather than deleting
    the claim.
    """
    set_status(db, 1, "applied")
    update_fields(db, 1, {"outreach_status": "sent", "last_outreach_at": "2026-08-01"})
    assert get_job(db, 1)["outreach_status"] == "sent"

    set_status(db, 1, "prospect")
    row = get_job(db, 1)
    assert row["outreach_status"] == ""
    assert row["last_outreach_at"] == ""


def test_outreach_survives_a_move_that_stays_at_or_above_applied(
    db: sqlite3.Connection,
) -> None:
    """Orthogonal means orthogonal. Moving applied to interviewing is not a
    reason to forget who was contacted."""
    set_status(db, 1, "applied")
    update_fields(db, 1, {"outreach_status": "sent"})
    set_status(db, 1, "interviewing")
    assert get_job(db, 1)["outreach_status"] == "sent"


def test_a_prospect_cannot_be_given_a_sent_outreach_status(db: sqlite3.Connection) -> None:
    with pytest.raises(TrackerError, match="must not claim outreach"):
        update_fields(db, 1, {"outreach_status": "sent"})


def test_a_planned_outreach_is_not_a_claim_that_it_happened(db: sqlite3.Connection) -> None:
    """`needs_contacts` is a plan and is fine on a prospect. Only the values
    that assert something happened are refused."""
    update_fields(db, 1, {"outreach_status": "needs_contacts"})
    assert get_job(db, 1)["outreach_status"] == "needs_contacts"


# --- rows that predate the rules ----------------------------------------------


def test_a_row_that_already_breaks_a_rule_can_still_be_repaired(
    db: sqlite3.Connection,
) -> None:
    """Refusing every write to a row that already breaks a rule would make
    rows written before these rules unrepairable, which is the opposite of
    what the spec asks for."""
    set_status(db, 1, "applied")
    db.execute("UPDATE jobs SET applied_date = '' WHERE id = 1")
    db.commit()
    assert invariant_breach(get_job(db, 1)) != ""

    # A write that does not touch the breach is allowed through.
    update_fields(db, 1, {"next_action": "chase the recruiter"})
    assert get_job(db, 1)["next_action"] == "chase the recruiter"
    # And the row can be put right.
    update_fields(db, 1, {"applied_date": "2026-08-01"})
    assert invariant_breach(get_job(db, 1)) == ""


def test_check_reports_pre_existing_rows_and_changes_nothing(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_status(db, 1, "applied")
    db.execute("UPDATE jobs SET applied_date = '' WHERE id = 1")
    db.commit()
    before = dict(get_job(db, 1))

    assert main(["check"]) == 1
    assert "must carry the date" in capsys.readouterr().err
    assert dict(get_job(db, 1)) == before


def test_check_is_quiet_and_succeeds_on_a_clean_tracker(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check"]) == 0
    assert "no tracker rows break" in capsys.readouterr().out


# --- contacts reference jobs by identity --------------------------------------


def _a_contact(
    conn: sqlite3.Connection,
    url: str,
    *,
    company: str = "Example Labs",
    role: str = "Senior Frontend Engineer",
) -> dict[str, str]:
    from harrier.outreach.contacts import upsert_contact

    return upsert_contact(
        conn,
        company=company,
        role=role,
        job_url=url,
        person_name="A Person",
        person_title="Engineering Manager",
        linkedin_url="https://www.linkedin.com/in/example-person",
        source="test",
    )


def test_a_contact_stays_linked_to_its_job_after_the_title_is_edited(
    db: sqlite3.Connection,
) -> None:
    """The defect this closes. The link was a text comparison, so renaming a
    job silently detached every contact about it: nothing errors, the outreach
    note simply stops belonging to anything."""
    from harrier.outreach.contacts import parse_linked_jobs
    from harrier.outreach.joblink import job_for_link
    from harrier.tracker.store import list_contacts

    job = get_job(db, 1)
    _a_contact(db, job["url"])

    update_fields(db, 1, {"title": "Staff Frontend Engineer"})

    contact = list_contacts(db)[0]
    link = parse_linked_jobs(contact["linked_jobs"])[0]
    assert link["job_id"] == "1"
    followed = job_for_link(db, link)
    assert followed is not None
    assert followed["title"] == "Staff Frontend Engineer"
    # The text in the link is the old title, and that is fine: it is what a
    # reader sees when the row is gone, not how the row is found.
    assert link["job_title"] == "Senior Frontend Engineer"


def test_a_link_to_an_untracked_job_is_kept_and_reported(db: sqlite3.Connection) -> None:
    """A contact can be about a job that was never tracked. Dropping the link
    would lose the record of who was spoken to, so it is kept and named.

    A different company as well as a different URL: `resolve_job_id` falls
    back to company and title, deliberately, so the same role posted at a
    second URL still resolves. Only a job that matches on neither is
    genuinely untracked.
    """
    from harrier.outreach.joblink import unresolved_links

    _a_contact(db, "https://boards.example.com/elsewhere/999", company="Someone Else Ltd")
    problems = unresolved_links(db)
    assert len(problems) == 1
    assert "matches no tracked job" in problems[0][1]


def test_the_backfill_links_old_records_and_drops_none(db: sqlite3.Connection) -> None:
    """Links written before the id existed. The count of unmatched ones is
    reported rather than swallowed, and nothing is deleted either way."""
    from harrier.outreach.contacts import parse_linked_jobs, serialize_linked_jobs
    from harrier.outreach.joblink import backfill_job_ids
    from harrier.tracker.store import list_contacts, update_contact_fields

    job = get_job(db, 1)
    _a_contact(db, job["url"])
    contact = list_contacts(db)[0]
    # Strip the ids, as a record written before this change would have been,
    # and add one that never matched anything.
    old = [
        {"company": job["company"], "job_title": job["title"], "job_url": job["url"]},
        {"company": "Gone Ltd", "job_title": "Engineer", "job_url": ""},
    ]
    update_contact_fields(db, int(contact["id"]), {"linked_jobs": serialize_linked_jobs(old)})

    resolved, unmatched = backfill_job_ids(db)
    assert (resolved, unmatched) == (1, 1)

    links = parse_linked_jobs(list_contacts(db)[0]["linked_jobs"])
    assert len(links) == 2, "the backfill dropped a link it could not match"
    assert [link["job_id"] for link in links] == ["1", ""]


def test_check_reports_an_unresolved_contact_link(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    _a_contact(db, "https://boards.example.com/elsewhere/999", company="Someone Else Ltd")
    assert main(["check"]) == 1
    assert "matches no tracked job" in capsys.readouterr().err


# --- the dead surface ----------------------------------------------------------


def test_manual_reject_is_gone_from_every_surface_together() -> None:
    """It was a column, a response field and a published contract field
    describing a decision nothing recorded. A reader who trusted it would
    have concluded no rejection was ever manual (spec 036)."""
    from pathlib import Path as _Path

    from harrier.tracker.schema import NOTE_KEYS, TRACKER_FIELDS

    assert "manual_reject" not in set(NOTE_KEYS) | set(TRACKER_FIELDS)

    contract = _Path(__file__).resolve().parents[3] / "packages" / "contract" / "openapi.json"
    assert "manual_reject" not in contract.read_text(encoding="utf-8")

    api = _Path(__file__).resolve().parents[1] / "src" / "harrier_api" / "app.py"
    assert "manual_reject" not in api.read_text(encoding="utf-8")


def test_a_database_at_the_previous_version_loses_the_dead_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal reaches databases that already exist, not only fresh ones."""
    from harrier.db import connect as open_db
    from harrier.tracker.schema import MIGRATIONS

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    path = tmp_path / "data"
    path.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(path / "tracker.db")
    old.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in MIGRATIONS:
        if version > 3:
            continue
        for statement in statements:
            old.execute(statement)
        old.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    old.commit()
    assert "manual_reject" in {row[1] for row in old.execute("PRAGMA table_info(jobs)")}
    old.close()

    conn = open_db()
    assert "manual_reject" not in {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()


# --- the export is on demand ---------------------------------------------------


def test_a_status_change_does_not_write_the_csv_export(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-003 said every mutating CLI command refreshed the export. Nothing
    ever did, and implementing it would rewrite the whole tracker to record
    one decision and put a second copy of the truth beside the database the
    ADR chose. The claim was corrected; this holds the code to it (spec 036).
    """
    monkeypatch.chdir(tmp_path)
    assert main(["shortlist", "1"]) == 0
    assert not (tmp_path / "tracker" / "jobs.csv").exists()

    assert main(["export", "--dest", str(tmp_path / "tracker")]) == 0
    assert (tmp_path / "tracker" / "jobs.csv").is_file()
