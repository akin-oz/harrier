"""What the candidate thought of a posting, read from spec 079's events (spec 077).

The label is the candidate's own judgement and nothing else. A forward
decision (shortlisted, tailored, applied) is "acted on"; a first decision
that rejects it is "skipped". A company's response is never a label: an
employer turning down an application says nothing about whether the
candidate wanted the job, and spec 079 keeps those events apart so that no
reader has to guess. A system closure before the candidate judged the job
says nothing either, so the job is left out.

Each exclusion is named, so the export can count them and the report can
say how much of the tracker the model never saw.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from harrier.screening.descriptions import MIN_DESCRIPTION_LENGTH_FOR_SCORING

ACTED_ON = 1
SKIPPED = 0
FORWARD_STATUSES: frozenset[str] = frozenset({"shortlisted", "tailored_cv_requested", "applied"})

UNDECIDED = "undecided"
ACTOR_UNKNOWN = "actor-unknown"
SYSTEM_CLOSED = "system-closed"
DESCRIPTION_MISSING = "description-missing"
DESCRIPTION_CHANGED = "description-changed"
EXCLUSIONS: tuple[str, ...] = (
    UNDECIDED,
    ACTOR_UNKNOWN,
    SYSTEM_CLOSED,
    DESCRIPTION_MISSING,
    DESCRIPTION_CHANGED,
)


@dataclass(frozen=True)
class Labelled:
    """One job's label, and the decision it came from."""

    job_id: int
    label: int
    # The ordering key for the time split: the decision's own time for a
    # live event, the day the job arrived for a backfilled one, whose time
    # is only an upper bound that a rescore can push late.
    decided_at: str
    live: bool
    # What the row carried when it was decided. Reported, never a feature.
    shown: str
    shown_version: str


def _deciding_event(decisions: Sequence[Mapping[str, str]]) -> tuple[int, Mapping[str, str]] | None:
    forward = [event for event in decisions if event["to_status"] in FORWARD_STATUSES]
    if forward:
        return ACTED_ON, forward[0]
    if decisions and decisions[0]["to_status"] == "rejected":
        return SKIPPED, decisions[0]
    return None


def label_job(
    job: Mapping[str, str], events: Sequence[Mapping[str, str]], description: str
) -> Labelled | str:
    """The job's label, or the name of the rule that excludes it.

    `events` are the job's `job_events` rows in the order they were recorded;
    `description` is the cached description the extractor would read now.
    """
    decisions = [event for event in events if event["kind"] == "decision"]
    by_candidate = [event for event in decisions if event["actor"] == "candidate"]
    if not by_candidate:
        if any(event["actor"] == "system" for event in decisions):
            return SYSTEM_CLOSED
        if any(event["actor"] == "unknown" for event in decisions):
            return ACTOR_UNKNOWN
        return UNDECIDED
    decided = _deciding_event(by_candidate)
    if decided is None:
        return UNDECIDED
    label, event = decided

    if len(description.strip()) < MIN_DESCRIPTION_LENGTH_FOR_SCORING:
        return DESCRIPTION_MISSING
    # The text the candidate judged is not the text the extractor would read:
    # a label for one posting attached to the features of another.
    judged = event.get("description_sha256", "")
    if judged and judged != hashlib.sha256(description.encode("utf-8")).hexdigest():
        return DESCRIPTION_CHANGED

    live = str(event.get("backfilled", "0")) != "1"
    added = (job.get("added_at") or "").strip()
    decided_at = event["at"] if live or not added else added
    return Labelled(
        job_id=int(job["id"]),
        label=label,
        decided_at=decided_at,
        live=live,
        shown=event.get("fit_score", "") if live else "",
        shown_version=event.get("scoring_version", "") if live else "",
    )
