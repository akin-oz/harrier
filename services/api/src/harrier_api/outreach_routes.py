"""Outreach endpoints (spec 048).

Every route calls the same domain function the corresponding CLI verb calls.
Two product invariants shape this module and are worth stating where the code
is rather than only in the spec:

**Contact discovery stages candidates for approval; nothing writes contacts
directly.** Discovery writes a staged artifact and a human decides. The only
route here that can reach the contacts store is the approval one, and
`approve_candidate` refuses anything not in that artifact.

**Nothing sends.** `mark-sent` records that the operator sent a message
themselves. No route in this file reaches a send path, and a test asserts it.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from harrier.tracker.selector import SelectorError
from harrier.tracker.store import TrackerError
from harrier_api.deps import Conn
from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import Manager, RunOut, run_out
from harrier_api.runs import RunParams, write_run_input

outreach_router = APIRouter()


# --- request and response shapes ---------------------------------------------


class FindContactsIn(BaseModel):
    best_only: bool = False
    max_items: int | None = None


class CandidateRef(BaseModel):
    """A staged candidate, identified the way the CLI identifies one."""

    linkedin_url: str


class SnoozeIn(BaseModel):
    until: str


class MarkIn(BaseModel):
    date: str | None = None


class DraftIn(BaseModel):
    contact_linkedin: str = ""
    contact_name: str = ""
    contact_role: str = ""
    audience: str = ""
    tone: str = "direct"
    jd_text: str = ""
    ai: bool = False


class BackfillIn(BaseModel):
    limit: int | None = None
    dry_run: bool = False


class CandidateOut(BaseModel):
    person_name: str
    person_title: str
    relevance: str
    fit_score: str
    linkedin_url: str
    review_status: str


class ContactOut(BaseModel):
    id: int
    person_name: str
    person_title: str
    relevance: str
    company: str
    contact_status: str
    linkedin_url: str


class OutreachRowOut(BaseModel):
    """A tracker row seen through the outreach axis, which is orthogonal to
    the status lifecycle and stays that way."""

    id: int
    company: str
    title: str
    outreach_status: str
    next_outreach_action: str
    best_contact_name: str
    best_contact_linkedin: str


OUTREACH_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "no job matched the selector, or the candidate was never staged"},
    409: {"description": "the domain refused the change"},
    **TOKEN_RESPONSES,
}


def _job_row(conn: sqlite3.Connection, selector: str) -> dict[str, str]:
    from harrier.tracker.selector import resolve_selector

    try:
        return resolve_selector(conn, selector)
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _row_out(row: dict[str, str]) -> OutreachRowOut:
    return OutreachRowOut(
        id=int(row["id"]),
        company=row.get("company", ""),
        title=row.get("title", ""),
        outreach_status=row.get("outreach_status", ""),
        next_outreach_action=row.get("next_outreach_action", ""),
        best_contact_name=row.get("best_contact_name", ""),
        best_contact_linkedin=row.get("best_contact_linkedin", ""),
    )


# --- discovery and the approval queue ----------------------------------------


@outreach_router.post(
    "/outreach/{selector}/find-contacts",
    operation_id="findContacts",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
async def find_contacts(
    selector: str, body: FindContactsIn, conn: Conn, manager: Manager
) -> RunOut:
    """A run, and one that spends money: it reaches Hunter and Apify.

    It stages candidates. It writes no contact, which is the invariant this
    module exists to keep.
    """
    row = _job_row(conn, selector)
    numbers = {"--max-items": body.max_items} if body.max_items is not None else {}
    params = RunParams(
        job_id=int(row["id"]),
        switches=frozenset({"--best-only"}) if body.best_only else frozenset(),
        numbers=numbers,
    )
    return run_out(await manager.start("find-contacts", params))


@outreach_router.get(
    "/outreach/{selector}/candidates",
    operation_id="listCandidates",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def list_candidates(selector: str, conn: Conn) -> list[CandidateOut]:
    """The staged artifact, read through the same extraction approve uses.

    This carries a real person's name and title, so it authenticates, for the
    same reason spec 047's artifact reads do.
    """
    from harrier.outreach import staged_candidates

    row = _job_row(conn, selector)
    return [
        CandidateOut(
            person_name=candidate.get("person_name", ""),
            person_title=candidate.get("person_title", ""),
            relevance=candidate.get("relevance", ""),
            fit_score=str(candidate.get("fit_score", "")),
            linkedin_url=candidate.get("linkedin_url", ""),
            review_status=candidate.get("review_status", ""),
        )
        for candidate in staged_candidates(row.get("company", ""), row.get("title", ""))
    ]


@outreach_router.post(
    "/outreach/{selector}/candidates/approve",
    operation_id="approveCandidate",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def approve(selector: str, body: CandidateRef, conn: Conn) -> ContactOut:
    """The only path in this API from a staged candidate to a stored contact.

    A candidate discovery never staged is refused rather than created, which
    is the staging invariant holding at the one place it could be bypassed.
    """
    from harrier.outreach import approve_candidate, sync_tracker_outreach

    row = _job_row(conn, selector)
    added = approve_candidate(
        conn,
        row.get("company", ""),
        row.get("title", ""),
        row.get("url", ""),
        body.linkedin_url,
    )
    if added is None:
        raise HTTPException(
            status_code=404,
            detail="candidate not found in the staged artifact",
        )
    sync_tracker_outreach(conn)
    return ContactOut(
        id=int(added.get("id", 0) or 0),
        person_name=added.get("person_name", ""),
        person_title=added.get("person_title", ""),
        relevance=added.get("relevance", ""),
        company=added.get("company", ""),
        contact_status=added.get("contact_status", ""),
        linkedin_url=added.get("linkedin_url", ""),
    )


@outreach_router.post(
    "/outreach/{selector}/candidates/reject",
    operation_id="rejectCandidate",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def reject(selector: str, body: CandidateRef, conn: Conn) -> CandidateOut:
    from harrier.outreach import update_candidate_review_status

    row = _job_row(conn, selector)
    updated = update_candidate_review_status(
        row.get("company", ""), row.get("title", ""), body.linkedin_url, "rejected"
    )
    if updated is None:
        raise HTTPException(
            status_code=404,
            detail="candidate not found in the staged artifact",
        )
    return CandidateOut(
        person_name=updated.get("person_name", ""),
        person_title=updated.get("person_title", ""),
        relevance=updated.get("relevance", ""),
        fit_score=str(updated.get("fit_score", "")),
        linkedin_url=updated.get("linkedin_url", ""),
        review_status=updated.get("review_status", ""),
    )


@outreach_router.post(
    "/outreach/{selector}/best-contact",
    operation_id="setBestContact",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def set_best_contact(selector: str, body: CandidateRef, conn: Conn) -> OutreachRowOut:
    from harrier.outreach import set_best_contact_for_job

    row = _job_row(conn, selector)
    updated = set_best_contact_for_job(conn, int(row["id"]), body.linkedin_url)
    if updated is None:
        # The CLI's own outcome: not an error, and not a success either.
        raise HTTPException(status_code=409, detail="contact is not linked to this job")
    return _row_out(updated)


# --- the contact store and the due queue -------------------------------------


@outreach_router.get(
    "/outreach/contacts",
    operation_id="listOutreachContacts",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def list_outreach_contacts(conn: Conn) -> list[ContactOut]:
    from harrier.tracker import list_contacts

    return [
        ContactOut(
            id=int(contact.get("id", 0) or 0),
            person_name=contact.get("person_name", ""),
            person_title=contact.get("person_title", ""),
            relevance=contact.get("relevance", ""),
            company=contact.get("company", ""),
            contact_status=contact.get("contact_status", ""),
            linkedin_url=contact.get("linkedin_url", ""),
        )
        for contact in list_contacts(conn)
    ]


@outreach_router.get(
    "/outreach/due",
    operation_id="listOutreachDue",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def list_outreach_due(conn: Conn) -> list[OutreachRowOut]:
    from harrier.outreach import outreach_due_rows

    return [_row_out(row) for row in outreach_due_rows(conn)]


@outreach_router.post(
    "/outreach/sync",
    operation_id="syncOutreach",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def sync_outreach(conn: Conn) -> list[OutreachRowOut]:
    from harrier.outreach import sync_tracker_outreach

    return [_row_out(row) for row in sync_tracker_outreach(conn)]


@outreach_router.post(
    "/outreach/{selector}/sent",
    operation_id="markOutreachSent",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def mark_sent(selector: str, body: MarkIn, conn: Conn) -> OutreachRowOut:
    """Records that the operator sent something themselves.

    It sends nothing. The page says so in those words, because a control
    next to a generated draft is exactly where that would be misread.
    """
    from harrier.outreach import mark_job_outreach_sent

    row = _job_row(conn, selector)
    try:
        return _row_out(mark_job_outreach_sent(conn, int(row["id"]), sent_at=body.date))
    except (TrackerError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@outreach_router.post(
    "/outreach/{selector}/replied",
    operation_id="markOutreachReplied",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def mark_replied(selector: str, body: MarkIn, conn: Conn) -> OutreachRowOut:
    from harrier.outreach import mark_job_outreach_replied

    row = _job_row(conn, selector)
    try:
        return _row_out(mark_job_outreach_replied(conn, int(row["id"]), replied_at=body.date))
    except (TrackerError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@outreach_router.post(
    "/outreach/{selector}/snooze",
    operation_id="snoozeOutreach",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
def snooze(selector: str, body: SnoozeIn, conn: Conn) -> OutreachRowOut:
    from harrier.outreach import snooze_job_outreach

    row = _job_row(conn, selector)
    try:
        return _row_out(snooze_job_outreach(conn, int(row["id"]), body.until))
    except (TrackerError, ValueError) as error:
        # An unparseable date is the domain's refusal, in its own words.
        raise HTTPException(status_code=409, detail=str(error)) from error


# --- drafting ----------------------------------------------------------------


@outreach_router.post(
    "/outreach/{selector}/draft",
    operation_id="draftOutreach",
    dependencies=[Depends(require_token)],
    responses=OUTREACH_ERRORS,
)
async def draft_outreach(selector: str, body: DraftIn, conn: Conn, manager: Manager) -> RunOut:
    """The contact and the tone travel in a file, not argv.

    A contact's name and LinkedIn URL are a real person's details, and argv
    is readable from the process table by every other process on the machine
    (spec 047's rule, applied to spec 048's inputs).
    """
    row = _job_row(conn, selector)
    payload = {
        "contact_linkedin": body.contact_linkedin,
        "contact_name": body.contact_name,
        "contact_role": body.contact_role,
        "audience": body.audience,
        "tone": body.tone,
        "jd_text": body.jd_text,
    }
    params = RunParams(
        job_id=int(row["id"]),
        switches=frozenset({"--ai"}) if body.ai else frozenset(),
        input_path=write_run_input(json.dumps(payload)),
    )
    return run_out(await manager.start("outreach-draft", params))


@outreach_router.post(
    "/outreach/backfill-posters",
    operation_id="backfillPosters",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def backfill(body: BackfillIn, manager: Manager) -> RunOut:
    """Acts on every LinkedIn row rather than one job, so it takes no
    selector and locks one at a time, as discovery does."""
    numbers = {"--limit": body.limit} if body.limit is not None else {}
    params = RunParams(
        switches=frozenset({"--dry-run"}) if body.dry_run else frozenset(),
        numbers=numbers,
    )
    return run_out(await manager.start("backfill-posters", params))
