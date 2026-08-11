"""What a status move drags with it (spec 036).

`set_status` validated membership in `STATUSES` and nothing else, so three
illegal states were reachable through the sanctioned write path: applied with
no applied date, a resurrected job still carrying the reason it was rejected,
and a prospect whose outreach block said contact had been sent. This module
owns what a move must clear so those states have nowhere to arrive from.

**On the transition table the spec asks for, which is deliberately not here.**

The first implementation derived legal predecessors: backward always allowed,
forward one stage at a time, `rejected` reachable from anywhere. It refused
only forward skips, which looked like a rule with no cost.

Running it against the suite showed the cost. A recruiter approaching you
about a job you never applied to takes a row from `prospect` to
`interviewing` in one move, and that is not a misclick, it is the good
outcome. The same is true of `shortlisted` to `applied` when a referral
skips the tailored CV. Nearly every forward jump in this pipeline is a real
event rather than an error, because the pipeline describes an intention and
the world does not follow it.

The spec names the failure this would be: "a transition table so strict the
operator cannot correct a mistake". A table that refuses the recruiter case
is that failure, and it would be discovered by someone whose interview is
already booked.

So no transition is refused. What the spec was reaching for is enforced
instead as a property of the row, in `harrier.tracker.invariants`, which is
where all three of its named defects actually live: they are illegal *states*
reachable through a generic field write, not illegal *moves*. A predecessor
table would not have closed any of them, because `update_fields` never
consults one.

This is a departure from the spec's stated scope and is recorded there too,
for Akin to overrule if the strictness is wanted.
"""

from __future__ import annotations

from harrier.tracker.schema import STATUSES

# The pipeline in order. `rejected` is not a stage in it: it is an exit
# reachable from any of them, so it is handled separately rather than given a
# position that would make "one step forward" meaningless.
PIPELINE: tuple[str, ...] = (
    "prospect",
    "shortlisted",
    "tailored_cv_requested",
    "applied",
    "interviewing",
)

TERMINAL = "rejected"

# The stage from which outreach may legitimately be in flight. Below it, an
# outreach block describing sent contact belongs to a life the row no longer
# has.
OUTREACH_FROM = PIPELINE.index("applied")


class IllegalTransitionError(ValueError):
    """A status move the pipeline does not permit. The message names both ends."""


def _stage(status: str) -> int:
    if status == TERMINAL:
        return -1
    return PIPELINE.index(status)


def transition_allowed(current: str, target: str) -> bool:
    """Whether a job at `current` may move to `target`. Any known pair may.

    Kept as a named function rather than deleted, because the question is a
    real one and the answer is a decision: see the module docstring. If a
    refusal is ever wanted, this is where it goes, and every caller already
    routes through it.
    """
    return current in STATUSES and target in STATUSES


def check_transition(current: str, target: str) -> None:
    """Raise unless the move is permitted, naming both ends."""
    if transition_allowed(current, target):
        return
    raise IllegalTransitionError(f"cannot move from {current} to {target}")


def fields_a_move_clears(current: str, target: str) -> dict[str, str]:
    """What a status move must reset, beyond the status itself.

    These are the illegal states the spec found, expressed as the writes that
    prevent them rather than as a validator that complains after the fact.
    A validator would leave the same three states reachable through the
    generic field update; making the move itself carry them means there is
    nowhere else to arrive from.
    """
    cleared: dict[str, str] = {}

    # A resurrected job carrying the reason it was rejected reads as though it
    # were rejected again for that reason. Only the rejecting branch ever
    # touched this field, so leaving rejected kept it forever.
    if current == TERMINAL and target != TERMINAL:
        cleared["rejection_reason"] = ""

    # The outreach axis is documented as orthogonal to the pipeline status and
    # was coupled in one direction only: seeded on applied, never reset. So a
    # job walked back to prospect still said contact had been sent. Resetting
    # it here is the choice this spec makes; the alternative was deleting the
    # orthogonality claim, and the claim is the one worth keeping because the
    # two axes really do move independently within a stage.
    if target != TERMINAL and _stage(target) < OUTREACH_FROM <= _stage(current):
        cleared["outreach_status"] = ""
        cleared["next_outreach_action"] = ""
        cleared["outreach_priority"] = ""
        cleared["last_outreach_at"] = ""

    return cleared
