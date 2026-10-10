"""FastAPI application: the read path over the tracker (spec 005).

The OpenAPI document generated from this app is the API contract (ADR-005).
Routes speak Pydantic models only; the web app speaks generated types only.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import AsyncIterator
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from harrier.db import DatabaseOwnedByHost, connect, default_db_path, lease_directory
from harrier.demo import repo_root
from harrier.hostlease import oldest_hold
from harrier.logsetup import configure_logging
from harrier.tracker import list_jobs
from harrier.tracker.reasons import CANDIDATE, COMPANY, SYSTEM, codes_for
from harrier.tracker.selector import SelectorError
from harrier.tracker.store import TrackerError
from harrier.tracks import Scope as TrackScope
from harrier_api.capture_routes import capture_router
from harrier_api.demo import demo_db_path, is_demo_mode, seed_demo_db
from harrier_api.deps import (
    DATABASE_HELD_DETAIL,
    VERB_IN_BODY,
    Conn,
    DatabaseHeldOut,
    DatabaseHoldOut,
    DatabaseRoute,
    ErrorOut,
    require_operation,
    scope_for,
)
from harrier_api.localauth import (
    TOKEN_RESPONSES,
    TRUSTED_HOSTS,
    load_or_create_token,
    require_token,
)
from harrier_api.mail_routes import mail_router
from harrier_api.ops_routes import ops_router
from harrier_api.outreach_routes import outreach_router
from harrier_api.runmodels import Manager, RunOut, run_out
from harrier_api.runs import RunManager, RunParams, RunState, format_sse, write_run_input
from harrier_api.tracks_routes import tracks_router

API_VERSION = "0.1.0"

# Stamped into the image at build time (Dockerfile, spec 051). Read from the
# environment rather than from git, because the container has no .git directory
# and asking git inside it would answer about whatever tree happened to be
# mounted rather than about the code that is running.
BUILD_UNKNOWN = "unknown"


def build_revision() -> str:
    return os.environ.get("HARRIER_REVISION", "").strip() or BUILD_UNKNOWN


def build_timestamp() -> str:
    return os.environ.get("HARRIER_BUILT_AT", "").strip() or BUILD_UNKNOWN


# Kept in lockstep with harrier.tracker.STATUSES by test_status_literal_matches.
JobStatus = Literal[
    "prospect",
    "shortlisted",
    "tailored_cv_requested",
    "applied",
    "interviewing",
    "rejected",
]


class JobOut(BaseModel):
    id: int
    company: str
    title: str
    location: str
    url: str
    source: str
    added_at: str
    fit_score: str
    status: JobStatus
    applied_date: str
    last_contact: str
    next_action: str
    outreach_status: str
    last_outreach_at: str
    next_outreach_action: str
    best_contact_name: str
    best_contact_linkedin: str
    contacts_found: str
    outreach_priority: str
    rejection_reason: str
    notes: str
    score: str
    archetype: str
    source_label: str
    external_key: str
    signals: str
    remote_filter: str
    manual_added: str
    created_at: str
    updated_at: str
    # Spec 094: the row's track, its deadline (empty for none), and whether
    # that deadline is before the server's date.
    track: str
    deadline: str
    deadline_passed: bool


class HealthOut(BaseModel):
    name: str
    version: str
    demo: bool
    database: str
    # Null exactly when `database_hold` is set: the count needs the database,
    # and a host process holds it (spec 075).
    job_count: int | None
    database_hold: DatabaseHoldOut | None
    # What is actually running. A process started from an old tree, or a
    # container built from one, answers every request correctly while serving
    # code nobody is looking at, and that is the failure spec 051 exists to
    # remove rather than reproduce in a new shape. "unknown" is the honest
    # answer for a process started outside the image build, which is what
    # `just dev` is, rather than a revision it cannot know.
    revision: str
    built_at: str


router = APIRouter(route_class=DatabaseRoute)


@router.get("/health", operation_id="getHealth")
def health() -> HealthOut:
    """Always 200 while the process runs, so the compose healthcheck never
    marks the container unhealthy because a host run holds the database.

    It looks for a host lease before it opens anything, and with one present
    it does not open the database at all; that is why it takes no `Conn`
    (spec 075). Its own lease, when it runs on the host, is not a hold.
    """
    hold = None if is_demo_mode() else oldest_hold(lease_directory(), ignore_pid=os.getpid())
    job_count: int | None = None
    if hold is None:
        conn = connect(demo_db_path() if is_demo_mode() else None, same_thread=False)
        try:
            job_count = int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        finally:
            conn.close()
    return HealthOut(
        name="harrier",
        version=API_VERSION,
        demo=is_demo_mode(),
        database=str(demo_db_path() if is_demo_mode() else default_db_path()),
        job_count=job_count,
        database_hold=None if hold is None else DatabaseHoldOut(subcommand=hold[0], since=hold[1]),
        revision=build_revision(),
        built_at=build_timestamp(),
    )


class SessionOut(BaseModel):
    token: str


@router.get("/session", operation_id="getSession")
def get_session() -> SessionOut:
    """The local API token, for the app this API serves.

    A cross-origin page may issue this request but cannot read the response:
    no CORS headers are sent, so the browser withholds the body. A page that
    made itself same-origin by DNS rebinding could read it, which is what the
    trusted-host middleware exists to prevent, and why that middleware is the
    load-bearing half of this pair rather than the token.
    """
    return SessionOut(token=load_or_create_token())


@router.get("/jobs", operation_id="listJobs")
def jobs(
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("list"))],
    status: Annotated[JobStatus | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
) -> list[JobOut]:
    rows = list_jobs(conn, scope, status=status, source=source)
    return [_as_job_out(row, scope) for row in rows]


class StartRunIn(BaseModel):
    kind: Literal["demo", "discovery"]


class RunEventOut(BaseModel):
    """The JSON payload of one SSE message on /runs/{id}/events.

    Declared on the route's response documentation so it lands in the OpenAPI
    components and the web app consumes the generated type (spec 006). The
    stream itself is text/event-stream; each message's data field is one of
    these, flattened per event type: log_line carries line, progress carries
    step/total/message, state_change carries state/exit_code.
    """

    type: str
    line: str | None = None
    step: int | None = None
    total: int | None = None
    message: str | None = None
    state: RunState | None = None
    exit_code: int | None = None


tracker_router = APIRouter(route_class=DatabaseRoute)


# The reason codes as the contract declares them (spec 080). Built from
# `harrier.tracker.reasons` at import and never written out here, so a code
# added to that table reaches the OpenAPI document on the next `just contract`
# and becomes a type error in the browser wherever a label for it is missing.
#
# A rejection takes the candidate's and the system's codes. A company's codes
# are not members, so a company verdict sent as a rejection is a 422 from the
# schema rather than a check someone has to remember to write.
#
# Type checkers cannot follow a class built at runtime, so they are shown an
# empty StrEnum, whose members are strings; pydantic, FastAPI and the schema
# get the real one.
if TYPE_CHECKING:

    class RejectionCode(StrEnum): ...

    class CompanyOutcomeCode(StrEnum): ...

else:
    RejectionCode = StrEnum(
        "RejectionCode", {code: code for code in (*codes_for(CANDIDATE), *codes_for(SYSTEM))}
    )
    CompanyOutcomeCode = StrEnum("CompanyOutcomeCode", {code: code for code in codes_for(COMPANY)})


class StatusChangeIn(BaseModel):
    verb: str
    reason: str | None = None
    # Optional, so the command line's free text and older clients keep
    # working: without a code the domain infers one (spec 079).
    reason_code: RejectionCode | None = None


class CompanyOutcomeIn(BaseModel):
    code: CompanyOutcomeCode
    # Accepted for parity with `harrier company-outcome`; the browser sends
    # none, because the pill is the confirmation (spec 080).
    note: str | None = None


class AddJobIn(BaseModel):
    company: str
    title: str
    location: str = ""
    url: str = ""
    source: str = "manual"
    description: str = ""
    # A real calendar date or nothing; an impossible date is 422 (spec 094).
    deadline: date | None = None


class AddJobOut(BaseModel):
    status: str
    message: str
    job: JobOut | None = None


class RescoreOut(BaseModel):
    previous: str
    current: int
    job: JobOut


TRACKER_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut, "description": "the selector named no job, or more than one"},
    409: {"model": ErrorOut, "description": "the tracker refused the change"},
    **TOKEN_RESPONSES,
}


def _as_job_out(job: dict[str, str], scope: TrackScope) -> JobOut:
    """The same conversion `/jobs` uses, so one row shape reaches the client."""
    deadline = (job.get("deadline") or "").strip()
    return JobOut.model_validate(
        {
            **job,
            "id": int(job["id"]),
            # Every row a scoped route returns is the scope's (spec 092).
            "track": scope.track.slug,
            "deadline": deadline,
            # The server decides, so the browser's clock cannot disagree with
            # the queue the server ordered (spec 094).
            "deadline_passed": bool(deadline) and deadline < date.today().isoformat(),
        }
    )


@tracker_router.post(
    "/tracker/{selector}/status",
    operation_id="changeJobStatus",
    dependencies=[Depends(require_token)],
    responses=TRACKER_ERRORS,
)
def change_job_status(
    selector: str,
    body: StatusChangeIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for(VERB_IN_BODY))],
) -> JobOut:
    """The same function the CLI's shortlist, track, applied, interviewing
    and reject verbs call (spec 042)."""
    from harrier.tracker.actions import TrackerActionError, change_status

    code = body.reason_code.value if body.reason_code is not None else None
    require_operation(scope, body.verb)
    try:
        return _as_job_out(
            change_status(conn, scope, selector, body.verb, reason=body.reason, reason_code=code),
            scope,
        )
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TrackerActionError, TrackerError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@tracker_router.post(
    "/tracker/{selector}/outcome",
    operation_id="recordCompanyOutcome",
    dependencies=[Depends(require_token)],
    responses=TRACKER_ERRORS,
)
def record_job_outcome(
    selector: str,
    body: CompanyOutcomeIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("company-outcome"))],
) -> JobOut:
    """What a company did with an application: the same function
    `harrier company-outcome` calls (specs 079, 080)."""
    from harrier.tracker.actions import TrackerActionError, record_company_outcome

    try:
        return _as_job_out(
            record_company_outcome(conn, scope, selector, body.code.value, note=body.note),
            scope,
        )
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TrackerActionError, TrackerError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@tracker_router.post(
    "/tracker",
    operation_id="addJob",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def add_job_by_hand(
    body: AddJobIn, conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("add"))]
) -> AddJobOut:
    from harrier.tracker.actions import add_manually

    result, job = add_manually(
        conn,
        scope,
        company=body.company,
        title=body.title,
        location=body.location,
        url=body.url,
        source=body.source,
        description=body.description,
        deadline=body.deadline.isoformat() if body.deadline is not None else "",
    )
    # A duplicate and a rejection are refusals the operator asked for, not
    # server errors, so they carry the reason rather than a status code the
    # UI would have to translate back into words.
    return AddJobOut(
        status=result.status,
        message=result.message,
        job=_as_job_out(job, scope) if job else None,
    )


@tracker_router.post(
    "/tracker/{selector}/rescore",
    operation_id="rescoreJob",
    dependencies=[Depends(require_token)],
    responses=TRACKER_ERRORS,
)
def rescore_job(
    selector: str, conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("reevaluate"))]
) -> RescoreOut:
    from harrier.tracker.actions import TrackerActionError, rescore

    try:
        result = rescore(conn, scope, selector)
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except TrackerActionError as error:
        # A refusal, not a fault. Rescoring a job whose description was never
        # captured would score it against less input than the import had
        # (spec 033), and the operator gets the domain's own words rather
        # than a 500.
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RescoreOut(
        previous=result.previous, current=result.current, job=_as_job_out(result.job, scope)
    )


@tracker_router.get("/tracker/queue", operation_id="listQueue")
def list_queue(
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("next"))],
    undecided: bool = False,
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[JobOut]:
    """`next` and `review`, which are the same ranking over different
    statuses: `next` is everything active, `review` narrows to what still
    needs a decision from the operator."""
    from harrier.tracker.actions import next_up, review_queue

    rows = review_queue(conn, scope, limit) if undecided else next_up(conn, scope, limit)
    return [_as_job_out(row, scope) for row in rows]


@tracker_router.get("/tracker/counts", operation_id="trackerCounts")
def tracker_counts(
    conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("counts"))]
) -> dict[str, int]:
    from harrier.tracker.actions import counts

    return counts(conn, scope)


class JobEventOut(BaseModel):
    """One recorded decision about a job (spec 079). `backfilled` marks an
    event reconstructed after the fact, whose time is not the decision's."""

    at: str
    kind: str
    actor: str
    from_status: str
    to_status: str
    reason_code: str
    # How the code reads, from `harrier.tracker.reasons`; empty with no code.
    reason_label: str
    reason_text: str
    fit_score: str
    backfilled: bool


@tracker_router.get(
    "/tracker/{selector}/events",
    operation_id="listJobEvents",
    responses={
        404: {"model": ErrorOut, "description": "the selector named no job, or more than one"}
    },
)
def list_job_events(
    selector: str,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("events show"))],
) -> list[JobEventOut]:
    """`events show`: a job's history, in the order it was recorded.

    No token, on any track. Its free text is the same class as the `notes`
    and `rejection_reason` that `GET /jobs` serves without one (spec 095).
    """
    from harrier.tracker.reasons import REASON_CODES
    from harrier.tracker.store import list_events

    job_id = _job_id_for(conn, scope, selector)
    return [
        JobEventOut(
            at=event["at"],
            kind=event["kind"],
            actor=event["actor"],
            from_status=event["from_status"],
            to_status=event["to_status"],
            reason_code=event["reason_code"],
            reason_label=REASON_CODES.get(event["reason_code"], ("", ""))[1],
            reason_text=event["reason_text"],
            fit_score=event["fit_score"],
            backfilled=event["backfilled"] == "1",
        )
        for event in list_events(conn, scope, job_id)
    ]


# --- apply: artifacts for one job (spec 047) ---

apply_router = APIRouter(route_class=DatabaseRoute)


class TailorIn(BaseModel):
    jd_text: str = ""
    no_ai: bool = False


class CoverLetterIn(BaseModel):
    notes: str = ""


class AnswersIn(BaseModel):
    questions: str = ""


class EvaluateIn(BaseModel):
    jd_text: str = ""


class ArtifactOut(BaseModel):
    """One artifact kind for a job, present or not.

    Carries the filename rather than the path: the operator does not need the
    absolute location, and it would put the home directory into every
    response for nothing.
    """

    kind: str
    exists: bool
    produced_by: str
    media_type: str
    filename: str


APPLY_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "no job matched the selector"},
    **TOKEN_RESPONSES,
}

ARTIFACT_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "no such job, artifact kind, or the artifact is not produced yet"},
    **TOKEN_RESPONSES,
}


def _job_id_for(conn: sqlite3.Connection, scope: TrackScope, selector: str) -> int:
    from harrier.tracker.selector import resolve_selector

    try:
        return int(resolve_selector(conn, scope, selector)["id"])
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _apply_params(
    conn: sqlite3.Connection, scope: TrackScope, selector: str, text: str, *, no_ai: bool
) -> RunParams:
    """Resolve the selector and stage the free text, in that order.

    The order matters: staging first would write a file for a job that does
    not exist, and nothing would ever consume or remove it.
    """
    job_id = _job_id_for(conn, scope, selector)
    stripped = text.strip()
    return RunParams(
        job_id=job_id,
        switches=frozenset({"--no-ai"}) if no_ai else frozenset(),
        input_path=write_run_input(text) if stripped else None,
    )


@apply_router.post(
    "/apply/{selector}/resume",
    operation_id="tailorResume",
    dependencies=[Depends(require_token)],
    responses=APPLY_ERRORS,
)
async def tailor_resume(
    selector: str,
    body: TailorIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("tailor"))],
    manager: Manager,
) -> RunOut:
    """The same `tailor` verb the CLI runs, as a run (spec 047)."""
    params = _apply_params(conn, scope, selector, body.jd_text, no_ai=body.no_ai)
    return run_out(await manager.start("tailor", params))


@apply_router.post(
    "/apply/{selector}/cover-letter",
    operation_id="draftCoverLetter",
    dependencies=[Depends(require_token)],
    responses=APPLY_ERRORS,
)
async def draft_cover_letter(
    selector: str,
    body: CoverLetterIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("cover-letter"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, selector, body.notes, no_ai=False)
    return run_out(await manager.start("cover-letter", params))


@apply_router.post(
    "/apply/{selector}/answers",
    operation_id="draftAnswers",
    dependencies=[Depends(require_token)],
    responses=APPLY_ERRORS,
)
async def draft_answers(
    selector: str,
    body: AnswersIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("answers"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, selector, body.questions, no_ai=False)
    return run_out(await manager.start("answers", params))


@apply_router.post(
    "/apply/{selector}/evaluate",
    operation_id="evaluateOffer",
    dependencies=[Depends(require_token)],
    responses=APPLY_ERRORS,
)
async def evaluate_offer_route(
    selector: str,
    body: EvaluateIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("evaluate"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, selector, body.jd_text, no_ai=False)
    return run_out(await manager.start("evaluate", params))


@apply_router.get(
    "/apply/{selector}/artifacts",
    operation_id="listArtifacts",
    dependencies=[Depends(require_token)],
    responses=APPLY_ERRORS,
)
def list_artifacts(
    selector: str, conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("artifacts"))]
) -> list[ArtifactOut]:
    """The index requires the token even though it is a read.

    Tracker reads do not, and this deliberately differs: the names here are
    derived from the candidate's own name and the company, and the bodies
    behind them are the densest personal content the system holds (spec 047).
    """
    from harrier.artifacts import artifacts_for_job

    job_id = _job_id_for(conn, scope, selector)
    return [
        ArtifactOut(
            kind=item.kind,
            exists=item.exists,
            produced_by=item.produced_by,
            media_type=item.media_type,
            filename=item.path.name,
        )
        for item in artifacts_for_job(conn, scope, job_id)
    ]


@apply_router.get(
    "/apply/{selector}/artifacts/{kind}",
    operation_id="readArtifact",
    dependencies=[Depends(require_token)],
    responses=ARTIFACT_ERRORS,
)
def read_artifact(
    selector: str,
    kind: str,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("artifacts"))],
) -> FileResponse:
    from harrier.artifacts import UnknownArtifactKind, artifact_for_job

    job_id = _job_id_for(conn, scope, selector)
    try:
        artifact = artifact_for_job(conn, scope, job_id, kind)
    except UnknownArtifactKind as error:
        # A path-shaped kind lands here, refused before anything touched the
        # filesystem, because the kind is a closed set (spec 047).
        raise HTTPException(status_code=404, detail=f"unknown artifact kind: {kind}") from error
    if not artifact.exists:
        raise HTTPException(
            status_code=404,
            detail=f"no {kind} for this job yet; run {artifact.produced_by} to produce it",
        )
    return FileResponse(
        artifact.path,
        media_type=artifact.media_type,
        filename=artifact.path.name,
    )


# --- apply: the application brief (spec 066, routed by spec 095) ---


class LetterLimitsOut(BaseModel):
    max_words: int | None = None
    max_sentences: int | None = None
    paragraphs: int | None = None


class AnswerLimitsOut(BaseModel):
    max_words: int | None = None
    max_sentences: int | None = None


class BriefOut(BaseModel):
    """A stored brief, as the store parsed it. Every field is present, empty
    when the brief does not set it, so the page can show it as fields."""

    never_name: list[str]
    guidance_url: str
    employer_guidance: str
    letter: LetterLimitsOut
    answers: AnswerLimitsOut
    evidence: list[str]
    views: dict[str, str]
    compensation_number: str
    confirmed_skills: list[str]


BRIEF_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "the store refused the brief, in its words"},
    404: {"model": ErrorOut, "description": "no such job, or no brief stored for it"},
    **TOKEN_RESPONSES,
}


def _brief_out(raw: object) -> BriefOut:
    from harrier.apply.brief import parse_brief

    brief = parse_brief(raw)
    return BriefOut(
        never_name=list(brief.never_name),
        guidance_url=brief.guidance_url,
        employer_guidance=brief.employer_guidance,
        letter=LetterLimitsOut(
            max_words=brief.letter.max_words,
            max_sentences=brief.letter.max_sentences,
            paragraphs=brief.letter.paragraphs,
        ),
        answers=AnswerLimitsOut(
            max_words=brief.answers.max_words, max_sentences=brief.answers.max_sentences
        ),
        evidence=list(brief.evidence),
        views=dict(brief.views),
        compensation_number=brief.compensation_number,
        confirmed_skills=list(brief.confirmed_skills),
    )


@apply_router.get(
    "/apply/{selector}/brief",
    operation_id="getBrief",
    dependencies=[Depends(require_token)],
    responses=BRIEF_ERRORS,
)
def get_brief(
    selector: str, conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("brief show"))]
) -> BriefOut:
    """`brief show`. A read that requires the token: a brief holds the
    operator's own notes about an application (specs 047, 095)."""
    import json

    from harrier.apply.brief import brief_text

    job_id = _job_id_for(conn, scope, selector)
    content = brief_text(conn, job_id)
    if content is None:
        raise HTTPException(status_code=404, detail=f"no brief for job {job_id}")
    return _brief_out(json.loads(content))


@apply_router.put(
    "/apply/{selector}/brief",
    operation_id="putBrief",
    dependencies=[Depends(require_token)],
    responses=BRIEF_ERRORS,
)
def put_brief(
    selector: str,
    body: dict[str, Any],
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("brief set"))],
) -> BriefOut:
    """`brief set`, with the brief as the body instead of a host file.

    The body is any JSON object, so the store's checks decide what a brief
    is: an unknown key or a wrong type is 400 in the store's words, as the
    CLI prints them, never a 422 written by this layer (spec 095).
    """
    import json

    from harrier.apply.brief import BriefError, store_brief

    job_id = _job_id_for(conn, scope, selector)
    try:
        store_brief(conn, job_id, json.dumps(body))
    except BriefError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _brief_out(body)


runs_router = APIRouter(route_class=DatabaseRoute)


@runs_router.post(
    "/runs",
    operation_id="startRun",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def start_run(body: StartRunIn, manager: Manager) -> RunOut:
    return run_out(await manager.start(body.kind))


@runs_router.get("/runs", operation_id="listRuns")
def list_runs(manager: Manager) -> list[RunOut]:
    return [run_out(run) for run in manager.list_runs()]


@runs_router.get("/runs/{run_id}", operation_id="getRun")
def get_run(run_id: str, manager: Manager) -> RunOut:
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run_out(run)


@runs_router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancelRun",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def cancel_run(run_id: str, manager: Manager) -> RunOut:
    run = await manager.cancel(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run_out(run)


@runs_router.get(
    "/runs/{run_id}/events",
    operation_id="streamRunEvents",
    responses={
        200: {
            "model": RunEventOut,
            "description": "SSE stream; each message's data field is a RunEventOut JSON payload.",
        }
    },
)
async def stream_run_events(run_id: str, request: Request, manager: Manager) -> StreamingResponse:
    if manager.get(run_id) is None:
        raise HTTPException(status_code=404, detail="run not found")
    last_id_header = request.headers.get("last-event-id", "0")
    try:
        last_event_id = int(last_id_header)
    except ValueError:
        last_event_id = 0

    async def event_stream() -> AsyncIterator[str]:
        async for event in manager.stream(run_id, last_event_id):
            yield format_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ConfigOut(BaseModel):
    """One configuration kind and where its current value comes from.

    `source` is the point of this endpoint: a reader has to be able to tell
    a value someone set from a value that is still coming out of a file,
    because unsetting the first restores the second (spec 023).
    """

    kind: str
    value: object
    source: Literal["store", "file"]
    updated_at: str | None
    # Null when the value was read. Otherwise the store's refusal, verbatim,
    # and `value` is null. Required rather than defaulted, so a generated
    # client cannot leave it unconsidered (spec 073).
    error: str | None


class ConfigIn(BaseModel):
    value: object


class ConfigErrorOut(BaseModel):
    """The body behind a 404 or a 400 on these routes.

    Declared so the generated client knows these outcomes exist. Store
    validation answers 400 rather than 422 on purpose: FastAPI already owns
    422 for a malformed request body, where the detail is a list of field
    errors. Reusing it would have put two different shapes behind one status
    and hidden the automatic one from the contract entirely (review finding
    on PR #20). A well-formed ConfigIn whose value is wrong for its kind is
    a different failure, and says so with a different code.
    """

    detail: str


CONFIG_ERRORS: dict[int | str, dict[str, object]] = {
    400: {
        "model": ConfigErrorOut,
        "description": "The value is not the shape this kind requires.",
    },
    404: {"model": ConfigErrorOut, "description": "No such configuration kind."},
}

config_router = APIRouter(route_class=DatabaseRoute)


def _config_out(conn: sqlite3.Connection, kind: str) -> ConfigOut:
    """What is in effect for one kind, including when it cannot be read.

    A kind whose value the store refuses is described rather than raised
    (spec 073). Raising made one broken kind a 500 for the whole list, gave
    the client no reason, and turned a DELETE that had already happened into
    a reported failure, because the response reads the fallback afterwards.

    The source is decided by whether a row exists, not by whether it
    parses. A broken row is reported as the store's problem and never
    replaced by the file's value: discovery reads the same row and fails on
    it, so the file's value would describe configuration nothing uses.
    """
    from harrier.userconfig import ConfigError, get_config, list_config

    rows = {row["kind"]: row["updated_at"] for row in list_config(conn)}
    if kind in rows:
        try:
            stored = get_config(conn, kind)
        except ConfigError as error:
            return ConfigOut(
                kind=kind, value=None, source="store", updated_at=rows[kind], error=str(error)
            )
        if stored is not None:
            return ConfigOut(
                kind=kind, value=stored, source="store", updated_at=rows[kind], error=None
            )
    try:
        value = _file_value(kind)
    except ConfigError as error:
        return ConfigOut(kind=kind, value=None, source="file", updated_at=None, error=str(error))
    return ConfigOut(kind=kind, value=value, source="file", updated_at=None, error=None)


def _file_value(kind: str) -> object:
    from harrier.userconfig import (
        COMPANY_HOLDS,
        DISCOVERY,
        FEEDS,
        LINKEDIN_SEARCHES,
        load_discovery_settings,
        load_feed_urls,
        load_hold_companies,
        load_search_urls,
    )

    if kind == FEEDS:
        return load_feed_urls()
    if kind == LINKEDIN_SEARCHES:
        return load_search_urls()
    if kind == DISCOVERY:
        return load_discovery_settings()
    if kind == COMPANY_HOLDS:
        return sorted(load_hold_companies())
    return None


# Both reads require the token (spec 023's open item, closed by spec 097):
# they serve the watchlist, the searches, the hold list and the academic
# searches, which describe the operator's own search.
@config_router.get(
    "/config",
    operation_id="listConfig",
    responses=TOKEN_RESPONSES,
    dependencies=[Depends(require_token)],
)
def list_configuration(conn: Conn) -> list[ConfigOut]:
    from harrier.userconfig import KINDS

    return [_config_out(conn, kind) for kind in KINDS]


@config_router.get(
    "/config/{kind}",
    operation_id="getConfig",
    responses={404: CONFIG_ERRORS[404], **TOKEN_RESPONSES},
    dependencies=[Depends(require_token)],
)
def get_configuration(kind: str, conn: Conn) -> ConfigOut:
    from harrier.userconfig import KINDS

    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=f"unknown configuration kind {kind}")
    return _config_out(conn, kind)


@config_router.put(
    "/config/{kind}",
    operation_id="putConfig",
    responses={**CONFIG_ERRORS, **TOKEN_RESPONSES},
    dependencies=[Depends(require_token)],
)
def put_configuration(kind: str, body: ConfigIn, conn: Conn) -> ConfigOut:
    from harrier.userconfig import KINDS, ConfigError, set_config

    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=f"unknown configuration kind {kind}")
    try:
        set_config(conn, kind, body.value)
    except ConfigError as error:
        # The shape rules live in the store, so the API cannot drift from
        # what the CLI accepts.
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _config_out(conn, kind)


@config_router.delete(
    "/config/{kind}",
    operation_id="deleteConfig",
    responses={404: CONFIG_ERRORS[404], **TOKEN_RESPONSES},
    dependencies=[Depends(require_token)],
)
def delete_configuration(kind: str, conn: Conn) -> ConfigOut:
    """Remove a stored value, restoring the file fallback."""
    from harrier.userconfig import KINDS, delete_config

    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=f"unknown configuration kind {kind}")
    delete_config(conn, kind)
    return _config_out(conn, kind)


def spa_dist_dir() -> Path:
    return repo_root() / "apps" / "web" / "dist"


class ApiPrefixMiddleware:
    """Strip a leading /api so one server can host both the SPA and the API.

    The web app always calls /api/... : in development Vite proxies that to
    this service and rewrites the prefix away, and when the built SPA is
    served from here (spec 021's demo) there is no proxy to do it. Rewriting
    in one ASGI hop keeps a single router set, so the OpenAPI document, and
    therefore the generated client, stays byte-identical (ADR-005).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = cast(str, scope.get("path", ""))
            if path == "/api" or path.startswith("/api/"):
                rewritten = path[len("/api") :] or "/"
                scope = {**scope, "path": rewritten, "raw_path": rewritten.encode("utf-8")}
        await self.app(scope, receive, send)


def create_app(run_manager: RunManager | None = None, spa_dir: Path | None = None) -> FastAPI:
    # logsetup said it was called by the CLI and by the API. Only the CLI ever
    # called it, so the process serving the browser had no configured root
    # logger and no identity redaction (spec 045).
    configure_logging()
    if is_demo_mode():
        seed_demo_db()
    app = FastAPI(
        title="harrier",
        version=API_VERSION,
        description="Local-first job search automation API.",
    )
    app.state.run_manager = (
        run_manager if run_manager is not None else RunManager(sweep_inputs=True)
    )

    @app.exception_handler(DatabaseOwnedByHost)
    async def database_held(request: Request, error: DatabaseOwnedByHost) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # Raised by `get_conn` inside the container while a host lease exists
        # (spec 075). Every route that depends on `Conn` declares it.
        body = DatabaseHeldOut(
            detail=DATABASE_HELD_DETAIL,
            hold=DatabaseHoldOut(subcommand=error.subcommand, since=error.since),
        )
        return JSONResponse(status_code=503, content=body.model_dump())

    app.include_router(router)
    app.include_router(runs_router)
    app.include_router(capture_router)
    app.include_router(config_router)
    app.include_router(tracker_router)
    app.include_router(apply_router)
    app.include_router(outreach_router)
    app.include_router(mail_router)
    app.include_router(ops_router)
    app.include_router(tracks_router)
    app.add_middleware(ApiPrefixMiddleware)
    # Closes DNS rebinding, which is what made every other protection here
    # bypassable: a page the operator visits resolves its own hostname to
    # 127.0.0.1 and then speaks to this API as same-origin. A rebound request
    # carries the attacker's hostname in Host, so it never reaches a route
    # (spec 035).
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(TRUSTED_HOSTS))
    dist = spa_dir if spa_dir is not None else spa_dist_dir()
    if dist.is_dir():
        # Mounted last so every API route still wins; html=True serves
        # index.html for the app shell. Absent before `pnpm build`, which is
        # why `just demo` builds first and `just dev` does not need this.
        app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


app = create_app()
