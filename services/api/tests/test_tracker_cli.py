"""The tracker verbs and the selector (spec 027).

The selector gets the most attention here because its failure mode is the
worst one in the CLI: silently editing somebody else's application.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.tracker import (
    UNDECIDED_STATUSES,
    SelectorError,
    add_job,
    get_job,
    list_jobs,
    rank_active,
    resolve_selector,
    set_status,
    status_counts,
)
from harrier_cli.main import main


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    for index, (company, title) in enumerate(
        [
            ("Example Co", "Senior Frontend Engineer"),
            ("Example Labs", "Staff Engineer"),
            ("Other Works", "Product Engineer"),
        ],
        start=1,
    ):
        add_job(
            conn,
            {
                "company": company,
                "title": title,
                "url": f"https://boards.example.com/{index}",
                "source": "greenhouse",
                "fit_score": str(90 - index * 10),
                "added_at": f"2026-08-0{index}",
            },
        )
    return conn


# --- the selector ------------------------------------------------------------


def test_a_numeric_selector_is_the_job_id(db: sqlite3.Connection) -> None:
    # Stated change from the old CLI, which indexed into the CSV by row
    # number and therefore moved whenever a row above it was removed.
    assert resolve_selector(db, "2")["company"] == "Example Labs"


def test_a_unique_substring_matches_one_row(db: sqlite3.Connection) -> None:
    assert resolve_selector(db, "Labs")["id"] == "2"
    assert resolve_selector(db, "product")["company"] == "Other Works"


def test_the_url_is_searchable_too(db: sqlite3.Connection) -> None:
    assert resolve_selector(db, "example.com/3")["company"] == "Other Works"


def test_an_ambiguous_selector_aborts_and_lists_the_candidates(db: sqlite3.Connection) -> None:
    """Never resolved by picking one: the operator narrows it themselves."""
    with pytest.raises(SelectorError) as excinfo:
        resolve_selector(db, "Example")
    message = str(excinfo.value)
    assert "ambiguous" in message
    assert "Example Co" in message
    assert "Example Labs" in message


def test_a_selector_matching_nothing_is_an_error(db: sqlite3.Connection) -> None:
    with pytest.raises(SelectorError, match="no tracker rows match"):
        resolve_selector(db, "nothing here")
    with pytest.raises(SelectorError, match="no job with id"):
        resolve_selector(db, "999")
    with pytest.raises(SelectorError, match="empty selector"):
        resolve_selector(db, "   ")


def test_an_ambiguous_selector_changes_nothing(db: sqlite3.Connection) -> None:
    before = [job["status"] for job in list_jobs(db)]
    assert main(["shortlist", "Example"]) == 1
    assert [job["status"] for job in list_jobs(db)] == before


# --- the transitions ---------------------------------------------------------


def test_each_verb_sets_its_status(db: sqlite3.Connection) -> None:
    for verb, expected in (
        ("shortlist", "shortlisted"),
        ("track", "tailored_cv_requested"),
        ("interviewing", "interviewing"),
    ):
        assert main([verb, "1"]) == 0
        assert get_job(db, 1)["status"] == expected


def test_applied_seeds_the_outreach_block_and_the_follow_up(db: sqlite3.Connection) -> None:
    assert main(["applied", "1", "--applied-date", "2026-08-01"]) == 0
    job = get_job(db, 1)
    assert job["status"] == "applied"
    assert job["applied_date"] == "2026-08-01"
    assert job["next_action"] == "follow up if no reply by 2026-08-08"
    assert job["outreach_status"] == "needs_contacts"
    assert job["next_outreach_action"] == "find contacts"
    assert job["outreach_priority"] == "high"


def test_reject_records_the_reason(db: sqlite3.Connection) -> None:
    assert main(["reject", "2", "hybrid,", "region", "policy"]) == 0
    job = get_job(db, 2)
    assert job["status"] == "rejected"
    assert job["rejection_reason"] == "hybrid, region policy"


def test_reject_without_a_reason_still_works(db: sqlite3.Connection) -> None:
    assert main(["reject", "2"]) == 0
    assert get_job(db, 2)["status"] == "rejected"


# --- add ---------------------------------------------------------------------


def test_add_routes_through_the_shared_scoring_path(db: sqlite3.Connection) -> None:
    assert (
        main(
            [
                "add",
                "--company",
                "Fresh Co",
                "--title",
                "Senior Frontend Engineer",
                "--url",
                "https://boards.example.com/fresh",
            ]
        )
        == 0
    )
    added = resolve_selector(db, "Fresh Co")
    assert added["status"] == "prospect"
    # Scored on the way in, like a discovered row.
    assert added["score"] != ""


def test_add_refuses_a_duplicate_url(db: sqlite3.Connection) -> None:
    before = len(list_jobs(db))
    assert (
        main(
            [
                "add",
                "--company",
                "Example Co",
                "--title",
                "Senior Frontend Engineer",
                "--url",
                "https://boards.example.com/1",
            ]
        )
        == 1
    )
    assert len(list_jobs(db)) == before


def test_add_without_a_company_is_refused(db: sqlite3.Connection) -> None:
    assert main(["add", "--company", "  ", "--title", "Engineer"]) == 1


# --- the queue ---------------------------------------------------------------


def test_rank_puts_the_nearest_to_sending_first(db: sqlite3.Connection) -> None:
    set_status(db, 1, "prospect")
    set_status(db, 2, "tailored_cv_requested")
    set_status(db, 3, "shortlisted")
    order = [job["id"] for job in rank_active(list_jobs(db))]
    assert order == ["2", "3", "1"]


def test_score_breaks_ties_within_a_stage(db: sqlite3.Connection) -> None:
    ranked = rank_active(list_jobs(db))
    # All three are prospects, so the higher fit_score leads.
    assert [job["id"] for job in ranked] == ["1", "2", "3"]


def test_rejected_rows_never_appear_in_the_queue(db: sqlite3.Connection) -> None:
    set_status(db, 1, "rejected")
    assert "1" not in [job["id"] for job in rank_active(list_jobs(db))]


def test_status_counts_cover_every_legal_status(db: sqlite3.Connection) -> None:
    counts = status_counts(list_jobs(db))
    assert counts["prospect"] == 3
    assert counts["rejected"] == 0


def test_next_and_review_run(db: sqlite3.Connection) -> None:
    assert main(["next", "--limit", "2"]) == 0
    assert main(["review"]) == 0


def test_next_on_an_empty_tracker_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "empty"))
    assert main(["next"]) == 0


# --- reevaluate --------------------------------------------------------------


def test_reevaluate_without_a_stored_description_is_skipped(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """A job whose description was never captured cannot be rescored honestly.

    It used to be rescored anyway, with `description=""` against a scorer that
    reads the description in three places, and the lower number then replaced
    the real one. Reporting it as skipped is the only answer that does not
    quietly destroy the score it claims to refresh (spec 033).
    """
    before = get_job(db, 1)
    assert main(["reevaluate", "1"]) == 2
    assert "no stored description" in capsys.readouterr().err
    assert get_job(db, 1)["fit_score"] == before["fit_score"]


def test_reevaluate_rescores_against_the_current_config(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the description it was imported with, rescoring reproduces the
    number the import produced. Asserting only that the fields are non-empty
    would pass against a constant."""
    from harrier.screening.config import load_candidate_config
    from harrier.screening.descriptions import save_description_cache
    from harrier.screening.normalized import make_normalized_job
    from harrier.screening.rules import score_job

    job = get_job(db, 1)
    description = (
        "We are hiring a senior frontend engineer for a fully remote role across "
        "Europe. You will work in TypeScript and React, own delivery end to end, "
        "and care about testing and performance."
    )
    save_description_cache(job["url"], description)
    expected, _ = score_job(
        make_normalized_job(
            source=job["source"] or "manual",
            company=job["company"],
            title=job["title"],
            location=job["location"],
            url=job["url"],
            description=description,
        ),
        load_candidate_config(db),
    )

    assert main(["reevaluate", "1"]) == 0
    rescored = get_job(db, 1)
    assert rescored["fit_score"] == str(expected)
    assert rescored["score"] == rescored["fit_score"]
    assert rescored["signals"] != ""
    assert rescored["scoring_version"] != ""


# --- review, validators, and dedupe (review findings on PR #27) --------------


def test_review_lists_only_rows_awaiting_a_decision(db: sqlite3.Connection) -> None:
    """applied and interviewing are decided: the next move is someone
    else's. review passed everything to the ranker and listed them."""
    set_status(db, 1, "applied")
    set_status(db, 2, "interviewing")
    queued = rank_active(list_jobs(db), statuses=UNDECIDED_STATUSES)
    assert [job["id"] for job in queued] == ["3"]


def test_next_still_shows_decided_but_active_rows(db: sqlite3.Connection) -> None:
    # next answers "what am I working on", which includes a sent
    # application waiting on a reply.
    set_status(db, 1, "applied")
    assert "1" in [job["id"] for job in rank_active(list_jobs(db))]


def test_an_undated_row_sorts_behind_a_dated_one(db: sqlite3.Connection) -> None:
    """An empty added_at compares before every date, so undated rows led
    the queue (review finding on PR #27)."""
    from harrier.tracker import update_fields

    update_fields(db, 1, {"added_at": "", "fit_score": "70"})
    update_fields(db, 2, {"added_at": "2026-08-01", "fit_score": "70"})
    ranked = [job["id"] for job in rank_active(list_jobs(db)) if job["id"] in {"1", "2"}]
    assert ranked == ["2", "1"]


def test_a_malformed_applied_date_is_refused_by_the_parser(db: sqlite3.Connection) -> None:
    # Reached date.fromisoformat and escaped as a traceback before.
    with pytest.raises(SystemExit):
        main(["applied", "1", "--applied-date", "not-a-date"])
    assert get_job(db, 1)["status"] == "prospect"


def test_a_bad_limit_is_refused_by_the_parser(db: sqlite3.Connection) -> None:
    for bad in ("zero", "-1", "0"):
        with pytest.raises(SystemExit):
            main(["next", "--limit", bad])


def test_add_refuses_a_duplicate_company_and_title(db: sqlite3.Connection) -> None:
    """The reachable third dedupe path for a manual add: no URL, same
    company and title as a tracked row."""
    before = len(list_jobs(db))
    assert main(["add", "--company", "Example Co", "--title", "Senior Frontend Engineer"]) == 1
    assert len(list_jobs(db)) == before


def test_reevaluate_reports_a_previous_score_of_zero_as_zero(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`previous or "-"` printed a stored 0 as "no previous score", which is a
    different statement about the row (review finding on PR #42). Only a blank
    column means unscored."""
    from harrier.screening.descriptions import save_description_cache
    from harrier.tracker.store import update_fields

    job = get_job(db, 1)
    update_fields(db, 1, {"fit_score": "0", "score": "0"})
    save_description_cache(job["url"], "Remote across Europe. TypeScript and React.")
    assert main(["reevaluate", "1"]) == 0
    assert "rescored 0 ->" in capsys.readouterr().out
