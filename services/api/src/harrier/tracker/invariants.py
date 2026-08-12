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


def all_breaches(job: dict[str, str]) -> list[str]:
    """Everything wrong with this row, in the operator's words.

    All of them, not the first. Comparing only the first breach let a second
    one through: a row already failing the applied-date rule reported that
    same message before and after a write that added a rejection reason, so
    the write looked like it introduced nothing (review finding on PR #44).
    """
    status = (job.get("status") or "").strip()
    found: list[str] = []

    if status == "applied" and not (job.get("applied_date") or "").strip():
        found.append("a job marked applied must carry the date it was applied on")

    if status != TERMINAL and (job.get("rejection_reason") or "").strip():
        found.append(
            f"a job at {status or 'no status'} must not carry a rejection reason: "
            "it belongs to a rejection that was undone"
        )

    if status in PIPELINE and PIPELINE.index(status) < OUTREACH_FROM:
        claimed = (job.get("outreach_status") or "").strip().lower()
        if claimed in OUTREACH_CLAIMS:
            found.append(
                f"a job at {status} must not claim outreach {claimed!r}: "
                "the outreach belongs to an application this row no longer has"
            )

    return found


def invariant_breach(job: dict[str, str]) -> str:
    """The first thing wrong with this row, or "".

    One message for the places that show one: a refusal the operator reads,
    and a report line. Enforcement uses `all_breaches`, because "did this
    write introduce a problem" cannot be answered by comparing one message.
    """
    breaches = all_breaches(job)
    return breaches[0] if breaches else ""


def check_rows(jobs: Iterable[dict[str, str]]) -> list[tuple[str, str]]:
    """Every row that breaks an invariant, as (id, what is wrong).

    Reporting only. Called by `harrier check`, which exists so a tracker that
    predates these rules can be inspected without being rewritten.

    Every breach on a row, not the first: a row with two problems that
    reported one would send the operator round the loop again for the second,
    and they are fixing the row, not the message.
    """
    return [(job.get("id", "?"), breach) for job in jobs for breach in all_breaches(job)]
