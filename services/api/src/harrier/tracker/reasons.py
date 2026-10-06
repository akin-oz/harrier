"""Who decided, and why: the reason codes and the actor each belongs to (spec 079).

A rejection used to be one status and one free-text column. The candidate's
own skip ("missing stack"), the company's verdict ("rejected by company") and
a system closure ("vacancy is closed") shared both, so a reader of the row
could not tell the candidate's judgement of a posting from an employer's
judgement of the candidate. Anything that learns from the tracker reads the
first as a label and must never read the second as one.

So every code here belongs to exactly one actor, and the event that records a
move stores the pair. The table is the authority: adding a code is a code
change reviewed against spec 079, not a free-text spelling that drifts.

Inference turns free text into a code for the paths that still send text
(the browser until spec 080, and history). It is ordered, specific before
general, and a text that matches nothing is `unclassified`, never a guess.
The patterns are generic phrases, not anything copied from a tracker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

CANDIDATE = "candidate"
COMPANY = "company"
SYSTEM = "system"
UNKNOWN = "unknown"
ACTORS: tuple[str, ...] = (CANDIDATE, COMPANY, SYSTEM, UNKNOWN)

CREATED = "created"
DECISION = "decision"
OUTCOME = "outcome"
KINDS: tuple[str, ...] = (CREATED, DECISION, OUTCOME)

UNCLASSIFIED = "unclassified"
INTERVIEW_INVITED = "interview_invited"

# Code, its one actor, and how it reads on a row. Ordered by actor so the
# command-line help lists them grouped.
REASON_CODES: dict[str, tuple[str, str]] = {
    "not_remote": (CANDIDATE, "not remote"),
    "location": (CANDIDATE, "location"),
    "stack": (CANDIDATE, "stack"),
    "role_too_senior": (CANDIDATE, "role too senior"),
    "role_too_junior": (CANDIDATE, "role too junior"),
    "contract_type": (CANDIDATE, "contract type"),
    "language": (CANDIDATE, "language"),
    "timezone": (CANDIDATE, "timezone"),
    "company": (CANDIDATE, "company"),
    "compensation": (CANDIDATE, "compensation"),
    "other": (CANDIDATE, "other"),
    "company_rejected": (COMPANY, "rejected by company"),
    "ghosted": (COMPANY, "ghosted"),
    "no_response": (COMPANY, "no response"),
    "assessment_failed": (COMPANY, "assessment failed"),
    INTERVIEW_INVITED: (COMPANY, "interview invited"),
    "vacancy_closed": (SYSTEM, "vacancy closed"),
    "duplicate": (SYSTEM, "duplicate"),
    "application_expired": (SYSTEM, "application expired"),
    "ai_evaluation": (SYSTEM, "ai evaluation"),
    # The old pipeline's own rejections, written as `auto_reject:<rule>`. A
    # rule decided them, not the candidate, so "auto_reject:hybrid" is not
    # the candidate's `not_remote` however much the words agree.
    "auto_reject": (SYSTEM, "automatic rule"),
    UNCLASSIFIED: (UNKNOWN, "unclassified"),
}


def actor_of(code: str) -> str:
    return REASON_CODES[code][0]


def label_of(code: str) -> str:
    return REASON_CODES[code][1]


def codes_for(actor: str) -> tuple[str, ...]:
    return tuple(code for code, (owner, _) in REASON_CODES.items() if owner == actor)


# First match wins, so the order is the policy: system facts before company
# verdicts before the candidate's own reasons. "auto_reject:vacancy_closed"
# is a closure before it is an automatic rule, and "rejected by company,
# hybrid" is the company's verdict before it is the candidate's location
# preference. `interview_invited` is deliberately absent: inference serves
# rejections, and no rejection text means an invitation.
_INFERENCE: tuple[tuple[str, str], ...] = (
    (
        r"vacancy_closed|\bclosed\b|\bno longer accepting\b|\bposition (?:has been )?filled\b"
        r"|\b(?:posting|listing|job ad) (?:has )?(?:expired|removed)\b|\bposting removed\b",
        "vacancy_closed",
    ),
    (r"\bapplication (?:has )?expired\b", "application_expired"),
    (r"\bduplicate\b|\bdupe\b", "duplicate"),
    (r"^ai-evaluation:", "ai_evaluation"),
    (r"^auto_reject:", "auto_reject"),
    (r"\bghost(?:ed|ing)?\b", "ghosted"),
    (r"\bno (?:response|reply|answer)\b|\bnever heard back\b|\bunanswered\b", "no_response"),
    (
        r"\b(?:failed|did not pass|didn't pass)\b.*\b(?:assessment|assignment|take[- ]home"
        r"|test|challenge)\b|\b(?:assessment|assignment|take[- ]home|challenge) failed\b",
        "assessment_failed",
    ),
    (
        r"\brejected by (?:the )?(?:company|employer|recruiter|hiring)\b|\bcompany rejected\b"
        r"|\brejected without an offer\b|\bnot (?:moving|proceeding) forward\b"
        r"|\brejection (?:email|letter|mail)\b",
        "company_rejected",
    ),
    (
        r"\bhybrid\b|\bon[- ]?site\b|\bin[- ]office\b|\bnot (?:fully )?remote\b|\brelocation\b",
        "not_remote",
    ),
    (r"\btime ?zones?\b", "timezone"),
    (r"\blangu\w*|\bfluent\b|\bnative speaker\b", "language"),
    (r"\b(?:freelanc\w*|contractor|marketplace|part[- ]time|fixed[- ]term)\b", "contract_type"),
    (
        r"\bover[- ]?qualified\b|\bjunior\b|\bintern(?:ship)?\b|\bentry[- ]level\b"
        r"|\bgraduate\b",
        "role_too_junior",
    ),
    (
        r"\black of experience\b|\bunder[- ]?qualified\b|\btoo senior\b"
        r"|\b(?:not enough|insufficient) experience\b",
        "role_too_senior",
    ),
    (
        r"\b(?:tech )?stack\b|\bangular\b|\bjava\b|\bphp\b|\bruby\b|\bback[- ]?end\b"
        r"|\bios\b|\bswift\b|\bmobile\b",
        "stack",
    ),
    (
        r"\blocation\b|\bgeo(?:graphic)? restriction\b|\bregion\b"
        r"|\b(?:uk|us|usa|eu|emea|apac|latam)[- ]only\b",
        "location",
    ),
    (
        r"\bsalary\b|\bcompensation\b|\bcomp\b|\bunderpaid\b|\bunpaid\b|\bequity[- ]only\b"
        r"|\bvolunteer\w*|\bfree of charge\b",
        "compensation",
    ),
    (r"\bculture\b|\bglassdoor\b|\bindustry\b|\bdomain\b|\bethic\w*|\bscam\b", "company"),
    (r"\bmanual rejection\b|\bnot interested\b", "other"),
)


# Folded before matching. `str.lower` leaves the Turkish dotless i (U+0131)
# alone, so "hybrid" typed on a Turkish keyboard would otherwise match
# nothing. Escaped, because the two letters are indistinguishable on screen.
_FOLD = str.maketrans({"\u0131": "i", "\u0130": "i"})


def infer_code(text: str | None) -> str:
    """The code a free-text reason names, or `unclassified` when none fits."""
    normalized = re.sub(r"\s+", " ", (text or "").translate(_FOLD).strip().lower())
    if not normalized:
        return UNCLASSIFIED
    for pattern, code in _INFERENCE:
        if re.search(pattern, normalized):
            return code
    return UNCLASSIFIED


def company_engaged(job: Mapping[str, str]) -> bool:
    """Whether a company can have responded to this job at all.

    An application it received, or an interview it invited. A recruiter can
    invite about a job nobody applied to, and the company can then reject,
    ghost or fail the candidate like any other. A rule that asked for an
    application alone refused every such response, though the browser offers
    it on an interviewing row (spec 079 amendment).
    """
    return bool((job.get("applied_date") or "").strip()) or job.get("status") == "interviewing"


class ReasonError(ValueError):
    """A code and an actor that cannot describe the same move."""


@dataclass(frozen=True)
class Move:
    """How one status change is recorded: what kind of event, by whom, why."""

    kind: str
    actor: str
    code: str


def classify_move(
    before: Mapping[str, str],
    target: str,
    *,
    reason_code: str | None = None,
    reason_text: str | None = None,
    actor: str | None = None,
) -> Move:
    """Decide the event a move to `target` records, from the row before it.

    The rules that keep a company's verdict out of the candidate's decisions
    live here, under the one status writer, so every caller gets them: the
    command line, the browser, the batch evaluator and anything added later.

    - A move to `interviewing` is the company's outcome, whatever verb asked
      for it. An interview is something the company did.
    - A rejection takes its actor from its code. A company code on a row the
      company engaged with (an application, or an interview it invited) is
      the company's outcome; on any other row it cannot be, and it is
      recorded as `unknown` rather than as a candidate decision.
    - Every other move is the candidate's decision unless a caller names the
      system.

    A caller that names an actor the code contradicts is refused: the pair is
    what makes the record trustworthy, so it is never silently corrected.
    """
    if reason_code is not None and reason_code not in REASON_CODES:
        raise ReasonError(f"unknown reason code {reason_code!r}; known: {', '.join(REASON_CODES)}")

    if target == "interviewing":
        if reason_code not in (None, INTERVIEW_INVITED):
            raise ReasonError(f"a move to interviewing is {INTERVIEW_INVITED}, not {reason_code}")
        if actor not in (None, COMPANY):
            raise ReasonError("an interview is the company's outcome, never a decision")
        return Move(OUTCOME, COMPANY, INTERVIEW_INVITED)

    if target == "rejected":
        code = reason_code if reason_code is not None else infer_code(reason_text)
        owner = actor_of(code)
        if owner == COMPANY:
            if code == INTERVIEW_INVITED:
                raise ReasonError("an interview invitation does not reject a job")
            if company_engaged(before):
                move = Move(OUTCOME, COMPANY, code)
            else:
                # A company cannot respond to a job it never received an
                # application for or invited an interview about. Filed as
                # unknown rather than refused, because the free text arrived
                # from a caller that cannot be asked again; the command line
                # refuses before it gets here.
                move = Move(DECISION, UNKNOWN, UNCLASSIFIED)
        else:
            move = Move(DECISION, owner, code)
        if actor is not None and actor != move.actor:
            raise ReasonError(f"{code} is recorded as {move.actor}, not {actor}")
        return move

    if reason_code:
        raise ReasonError("a reason code is recorded only on a rejection or an interview")
    if actor not in (None, CANDIDATE, SYSTEM):
        raise ReasonError(f"a move to {target} is a decision, never a {actor} outcome")
    return Move(DECISION, actor or CANDIDATE, "")
