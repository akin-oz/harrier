"""The tracker operations, in one place both callers reach (spec 042).

Every tracker verb lived inline in the CLI's dispatch function. Adding HTTP
routes on top of that meant writing the same rules twice, and two
implementations drift while both test suites pass, because each covers its
own copy. That is the failure this module exists to prevent, and
`tests/test_ui_tracker.py::test_the_cli_and_the_api_call_the_same_function`
is what holds it: it patches the action and drives both paths through it.

Nothing here is new behaviour. Each function is the CLI's existing branch,
moved, so a difference between the command line and the browser would be a
bug in one of them rather than a design decision nobody wrote down.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from harrier.capture import CaptureResult, add_captured_job
from harrier.screening.config import load_candidate_config
from harrier.screening.descriptions import load_cached_description
from harrier.screening.normalized import make_normalized_job
from harrier.screening.policy import policy_version
from harrier.screening.rules import score_job
from harrier.tracker.queue import UNDECIDED_STATUSES, rank_active, status_counts
from harrier.tracker.score import score_fields, stored_score
from harrier.tracker.selector import resolve_selector
from harrier.tracker.store import get_job, list_jobs, set_status, update_fields

# The CLI verb each status change is spelled as, and the status it produces.
# One mapping, so the browser cannot invent a sixth transition the command
# line does not have.
STATUS_BY_VERB = {
    "shortlist": "shortlisted",
    "track": "tailored_cv_requested",
    "applied": "applied",
    "interviewing": "interviewing",
    "reject": "rejected",
}


class TrackerActionError(RuntimeError):
    """An operation was refused. The message is what the operator sees."""


@dataclass(frozen=True)
class RescoreResult:
    job: dict[str, str]
    previous: str
    current: int


def change_status(
    conn: sqlite3.Connection,
    selector: str,
    verb: str,
    *,
    reason: str | None = None,
    applied_date: str | None = None,
) -> dict[str, str]:
    """Move one job to the status a verb names.

    The selector is resolved here rather than by the caller, so the browser
    and the command line disagree about which job they meant only if
    `resolve_selector` is wrong, which is its own module with its own tests.
    """
    target = STATUS_BY_VERB.get(verb)
    if target is None:
        raise TrackerActionError(f"unknown tracker verb: {verb}")
    job = resolve_selector(conn, selector)
    # The reason is only meaningful on a rejection, which is the only status
    # the store stamps it against. Passing it on any other transition would
    # be silently dropped, so it is named here rather than absorbed.
    if reason and target != "rejected":
        raise TrackerActionError(f"a reason is only recorded on a rejection, not on {verb}")
    return set_status(
        conn,
        int(job["id"]),
        target,
        applied_date=applied_date,
        rejection_reason=reason if target == "rejected" else None,
    )


def add_manually(
    conn: sqlite3.Connection,
    *,
    company: str,
    title: str,
    location: str = "",
    url: str = "",
    source: str = "manual",
    description: str = "",
) -> tuple[CaptureResult, dict[str, str] | None]:
    """Add a job by hand, scored and deduped like any captured one.

    Returns the result and the row it refers to, because `CaptureResult`
    carries no row and both callers want to show what happened. A duplicate
    returns the existing row rather than nothing: "already tracked" is more
    useful with the tracked thing attached.

    The row is found the way the duplicate was: by URL when there is one, and
    otherwise by company and title, which is how `find_duplicate` matches. The
    lookup used to run only when a URL was given, so a URL-less duplicate was
    correctly refused and then reported with nothing attached, which is the
    one case where the message needed the row most (review finding on PR #41).
    """
    result = add_captured_job(
        conn,
        company=company,
        title=title,
        location=location,
        url=url,
        source=source or "manual",
        description=description,
    )
    wanted = url.strip()
    wanted_company = company.strip().casefold()
    wanted_title = title.strip().casefold()
    for candidate in list_jobs(conn):
        if wanted and candidate["url"] == wanted:
            return result, candidate
        if not wanted and (
            candidate["company"].casefold() == wanted_company
            and candidate["title"].casefold() == wanted_title
        ):
            return result, candidate
    return result, None


def rescore(conn: sqlite3.Connection, selector: str) -> RescoreResult:
    """Score one job again against the current configuration.

    Rescoring uses the description stored at import. It used to pass an empty
    one while the scorer reads the description in three places, so the verb
    whose purpose is rescoring scored against strictly less input than the
    first pass had, then overwrote the real number with the result. That was
    spec 033's finding, and when this function was written it carried the
    defect deliberately so both callers stayed wrong the same way. Spec 033
    has landed, so the fix lives here now, once, for the command line and the
    browser together.

    A job whose description was never captured is refused rather than scored
    low: there is no honest number to give it. The caller decides what that
    means, which is exit 2 on the command line and a 409 over HTTP.
    """
    job = resolve_selector(conn, selector)
    description = load_cached_description(job["url"])
    if not description:
        raise TrackerActionError(
            "no stored description, so rescoring would score it against less than the "
            "first pass had. Run discovery again to capture one."
        )
    normalized = make_normalized_job(
        source=job["source"] or "manual",
        company=job["company"],
        title=job["title"],
        location=job["location"],
        url=job["url"],
        description=description,
    )
    candidate_cfg = load_candidate_config(conn)
    score, reasons = score_job(normalized, candidate_cfg)
    # A stored score of 0 is a score. The blank column is the only thing that
    # means unscored, so it is the only thing that reads as "-".
    previous = str(stored_score(job)) if job.get("fit_score", "").strip() else "-"
    updated = update_fields(
        conn, int(job["id"]), score_fields(score, reasons, policy_version(candidate_cfg))
    )
    return RescoreResult(job=updated, previous=previous, current=score)


def next_up(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, str]]:
    """What to work on now, in the CLI's ordering."""
    return rank_active(list_jobs(conn), limit)


def review_queue(conn: sqlite3.Connection, limit: int | None = None) -> list[dict[str, str]]:
    """What still needs a decision, which is a narrower question than `next`."""
    return rank_active(list_jobs(conn), limit, statuses=UNDECIDED_STATUSES)


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return status_counts(list_jobs(conn))


def one(conn: sqlite3.Connection, job_id: int) -> dict[str, str]:
    return get_job(conn, job_id)
