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
