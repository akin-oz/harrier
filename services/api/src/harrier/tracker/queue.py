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

ACTIVE_STATUSES = frozenset(STATUSES) - {"rejected"}

# Lower sorts first. Closest-to-sending first; interviewing sits above
# applied because a live conversation outranks a sent application.
STAGE_PRIORITY = {
    "tailored_cv_requested": 0,
    "shortlisted": 1,
    "prospect": 2,
    "interviewing": 3,
    "applied": 4,
}


def parse_score(job: dict[str, str]) -> int:
    raw = (job.get("score") or job.get("fit_score") or "").strip()
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _added_key(job: dict[str, str]) -> str:
    # ISO dates sort lexically; a blank sorts oldest, which is what we want
    # for a row that never recorded when it arrived.
    return job.get("added_at", "") or ""


def rank_active(jobs: list[dict[str, str]], limit: int | None = None) -> list[dict[str, str]]:
    """Active rows, most actionable first. Rejected rows never appear."""
    active = [job for job in jobs if job["status"] in ACTIVE_STATUSES]
    ranked = sorted(
        active,
        key=lambda job: (
            STAGE_PRIORITY.get(job["status"], 99),
            -parse_score(job),
            # Newest first within a stage and score: a fresh posting is more
            # likely to still be open.
            tuple(-ord(character) for character in _added_key(job)),
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
