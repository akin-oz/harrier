"""What a tracker row may not say about itself (spec 036).

Three states were reachable through the sanctioned write path, and each one
makes the row lie about its own history: applied with no applied date, a
resurrected job still carrying the reason it was rejected, and a prospect
whose outreach block says contact was sent.

The checks live here rather than inside `set_status` because that is not
where they were escaped. `set_status` stamps an applied date, and then the
generic field update could clear it: an invariant enforced by one verb is an
invariant the next verb has never heard of. Both write paths consult this,
and `check_rows` uses the same function so the report and the enforcement
cannot disagree about what is wrong.

Existing rows are reported, never rewritten. A migration that silently edits
somebody's job search to satisfy a rule invented afterwards destroys the
record of what they actually did, which is worse than the inconsistency.
"""

from __future__ import annotations

from collections.abc import Iterable

from harrier.tracker.transitions import OUTREACH_FROM, PIPELINE, TERMINAL

# Outreach values that assert something happened, as opposed to a seeded plan
# to make it happen. A row walked back below applied may hold the plan; it may
# not hold the claim.
OUTREACH_CLAIMS: frozenset[str] = frozenset({"sent", "replied", "bounced", "no_reply"})


def invariant_breach(job: dict[str, str]) -> str:
    """The first thing wrong with this row, in the operator's words, or "".

    One message rather than a list: the caller is usually a write being
    refused, and the operator fixes one thing and tries again.
    """
    status = (job.get("status") or "").strip()

    if status == "applied" and not (job.get("applied_date") or "").strip():
        return "a job marked applied must carry the date it was applied on"

    if status != TERMINAL and (job.get("rejection_reason") or "").strip():
        return (
            f"a job at {status or 'no status'} must not carry a rejection reason: "
            "it belongs to a rejection that was undone"
        )

    if status in PIPELINE and PIPELINE.index(status) < OUTREACH_FROM:
        claimed = (job.get("outreach_status") or "").strip().lower()
        if claimed in OUTREACH_CLAIMS:
            return (
                f"a job at {status} must not claim outreach {claimed!r}: "
                "the outreach belongs to an application this row no longer has"
            )

    return ""


def check_rows(jobs: Iterable[dict[str, str]]) -> list[tuple[str, str]]:
    """Every row that breaks an invariant, as (id, what is wrong).

    Reporting only. Called by `harrier check`, which exists so a tracker that
    predates these rules can be inspected without being rewritten.
    """
    return [(job.get("id", "?"), breach) for job in jobs if (breach := invariant_breach(job))]
