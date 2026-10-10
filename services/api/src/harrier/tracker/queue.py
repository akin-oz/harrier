"""What to work on next (spec 027).

The old CLI's `next` ranking, ported: the pipeline order says what is most
nearly ready to send, and score breaks ties within a stage. A tailored CV
waiting for review beats a shortlist entry, which beats an unread prospect,
because the first is one step from going out.

This is not the digest's ordering and is not meant to be. The digest
answers "what happened today and what is overdue"; this answers "what do I
do in the next ten minutes". Spec 027 originally asked for one rule serving
both, which would have broken parity with the old ranking for no gain.
"""

from __future__ import annotations

from harrier.tracker.schema import STATUSES
from harrier.tracker.score import stored_score

ACTIVE_STATUSES = frozenset(STATUSES) - {"rejected"}

# Rows still awaiting a decision from the operator. `applied` and
# `interviewing` are decided: the next move belongs to someone else.
UNDECIDED_STATUSES = frozenset({"prospect", "shortlisted", "tailored_cv_requested"})

# Lower sorts first. Closest-to-sending first; interviewing sits above
# applied because a live conversation outranks a sent application.
STAGE_PRIORITY = {
    "tailored_cv_requested": 0,
    "shortlisted": 1,
    "prospect": 2,
    "interviewing": 3,
    "applied": 4,
}


def _reverse(text: str) -> tuple[int, ...]:
    """A sort key that orders strings descending."""
    return tuple(-ord(character) for character in text)


def parse_score(job: dict[str, str]) -> int:
    """The row's score, from the one field that is authoritative (spec 033).

    This used to read `score or fit_score`, which meant the queue preferred
    whichever field had been written most recently while every other reader
    took `fit_score`. Import wrote one, rescoring wrote the other, and the
    two disagreed with nothing to say which was current.
    """
    return stored_score(job)


def _added_key(job: dict[str, str]) -> tuple[int, str]:
    """Newest first, with undated rows last.

    Negating an ISO string is not possible, so the sort reverses the whole
    key below instead. The leading flag keeps a blank `added_at` at the
    back: an empty string compares before every dated one, which put rows
    that never recorded their arrival at the front of the queue (review
    finding on PR #27).
    """
    added = job.get("added_at", "") or ""
    return (0 if added else 1, added)


def rank_active(
    jobs: list[dict[str, str]],
    limit: int | None = None,
    *,
    statuses: frozenset[str] = ACTIVE_STATUSES,
) -> list[dict[str, str]]:
    """Rows in the given statuses, most actionable first.

    `next` ranks everything active; `review` narrows to the undecided ones,
    because it exists to answer what still needs a decision from you.
    """
    active = [job for job in jobs if job["status"] in statuses]
    ranked = sorted(
        active,
        key=lambda job: (
            STAGE_PRIORITY.get(job["status"], 99),
            -parse_score(job),
            # Newest first within a stage and score: a fresh posting is more
            # likely to still be open. Undated rows sort last either way.
            _added_key(job)[0],
            _reverse(_added_key(job)[1]),
            int(job["id"]),
        ),
    )
    return ranked[:limit] if limit is not None else ranked


def status_counts(jobs: list[dict[str, str]]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for job in jobs:
        if job["status"] in counts:
            counts[job["status"]] += 1
    return counts


def rank_by_deadline(
    jobs: list[dict[str, str]],
    limit: int | None = None,
    *,
    statuses: frozenset[str] = ACTIVE_STATUSES,
    today: str,
) -> list[dict[str, str]]:
    """The academic kind's queue (spec 093): nearest open deadline first, then
    rows with no deadline, then rows whose deadline has passed. Passed rows are
    sunk, never hidden: a call the operator forgot is the row that most needs
    to be seen once. Within a group, pipeline stage, then id. `today` is a
    parameter so a test can pin it.
    """

    def key(job: dict[str, str]) -> tuple[int, str, int, int]:
        deadline = (job.get("deadline") or "").strip()
        if deadline and deadline >= today:
            group = 0
        elif not deadline:
            group = 1
        else:
            group = 2
        nearest = deadline if group == 0 else ""
        return (group, nearest, STAGE_PRIORITY.get(job["status"], 99), int(job["id"]))

    active = [job for job in jobs if job["status"] in statuses]
    ranked = sorted(active, key=key)
    return ranked[:limit] if limit is not None else ranked


def deadline_passed(job: dict[str, str], today: str) -> bool:
    deadline = (job.get("deadline") or "").strip()
    return bool(deadline) and deadline < today


def deadline_warning(job: dict[str, str], today: str) -> str | None:
    """What `tailor`, `cover-letter` and `answers` print when the row's
    deadline has passed, or None when it has not or there is none.

    A warning, never a refusal: some calls are reviewed on a rolling basis
    (spec 101). `today` is an ISO date, a parameter so a test can pin it.
    """
    if not deadline_passed(job, today):
        return None
    deadline = (job.get("deadline") or "").strip()
    return f"warning: the deadline for this call passed on {deadline}"


def rank_for(
    queue: str,
    jobs: list[dict[str, str]],
    limit: int | None = None,
    *,
    statuses: frozenset[str] = ACTIVE_STATUSES,
    today: str,
) -> list[dict[str, str]]:
    """The queue order a track's kind names (spec 093)."""
    if queue == "nearest_deadline":
        return rank_by_deadline(jobs, limit, statuses=statuses, today=today)
    return rank_active(jobs, limit, statuses=statuses)
