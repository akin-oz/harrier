"""FastAPI application: the read path over the tracker (spec 005).

The OpenAPI document generated from this app is the API contract (ADR-005).
Routes speak Pydantic models only; the web app speaks generated types only.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from harrier.apply.brief import Brief
from harrier.connection import POSTGRES_NOT_YET, Connection, store_target
from harrier.db import DatabaseOwnedByHost, connect, default_db_path, lease_directory
from harrier.demo import repo_root
from harrier.hostlease import oldest_hold
from harrier.logredact import scrub_outbound
from harrier.logsetup import configure_logging, scrub_uvicorn_handlers
from harrier.tracker import list_jobs
from harrier.tracker.reasons import CANDIDATE, COMPANY, SYSTEM, codes_for
from harrier.tracker.selector import SelectorError
from harrier.tracker.store import (
    EligibilityAssessment,
    EligibilityView,
    TrackerError,
    eligibility_views,
)
from harrier.tracks import DEFAULT_TRACK_ID
from harrier.tracks import Scope as TrackScope
from harrier_api.capture_routes import capture_router
from harrier_api.demo import demo_db_path, is_demo_mode, seed_demo_db
from harrier_api.deps import (
    CAUSE_JOIN,
    DATABASE_HELD_DETAIL,
    VERB_IN_BODY,
    Conn,
    DatabaseHeldOut,
    DatabaseHoldOut,
    DatabaseRoute,
    ErrorOut,
    merge_responses,
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
from harrier_api.runmodels import RUN_CONFLICT_RESPONSES, Manager, RunOut, run_out
from harrier_api.runs import (
    RunConflictError,
    RunManager,
    RunParams,
    RunState,
    format_sse,
    write_run_input,
)
from harrier_api.settings_routes import settings_router
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


# Kept in lockstep with harrier.tracker.schema by the eligibility tests (spec 122).
EligibilityVerdict = Literal["confirmed", "possible", "unknown", "excluded"]
EligibilityRoute = Literal["employee", "contractor", "eor", "relocation"]
EligibilityField = Literal["", "title", "location", "description", "external"]


class EligibilityOut(BaseModel):
    """What a row reads about Türkiye (spec 122). `verdict` is geography
    only and `routes` engagement only. `origin` is `none` for a row with no
    assessment, which reads `unknown`. `disagrees` is true when the current
    assessment is the candidate's and the rules' latest differs from it in
    verdict or routes; nothing else follows from it."""

    verdict: EligibilityVerdict
    routes: list[EligibilityRoute]
    origin: Literal["rule", "candidate", "none"]
    evidence_url: str
    excerpt: str
    field: EligibilityField
    checked_at: str
    version: str
    description_sha256: str
    disagrees: bool


class EligibilityEntryOut(BaseModel):
    """One recorded assessment, as written (spec 122)."""

    verdict: EligibilityVerdict
    routes: list[EligibilityRoute]
    origin: Literal["rule", "candidate"]
    evidence_url: str
    excerpt: str
    field: EligibilityField
    matched: str
    checked_at: str
    version: str
    description_sha256: str
    at: str


def _eligibility_out(view: EligibilityView) -> EligibilityOut:
    current = view.current
    if current is None:
        return EligibilityOut(
            verdict="unknown",
            routes=[],
            origin="none",
            evidence_url="",
            excerpt="",
            field="",
            checked_at="",
            version="",
            description_sha256="",
            disagrees=False,
        )
    return EligibilityOut.model_validate(
        {
            "verdict": current.verdict,
            "routes": list(current.routes),
            "origin": current.origin,
            "evidence_url": current.evidence_url,
            "excerpt": current.excerpt,
            "field": current.field,
            "checked_at": current.checked_at,
            "version": current.version,
            "description_sha256": current.description_sha256,
            "disagrees": view.disagrees,
        }
    )


def _eligibility_entry_out(entry: EligibilityAssessment) -> EligibilityEntryOut:
    return EligibilityEntryOut.model_validate(
        {
            "verdict": entry.verdict,
            "routes": list(entry.routes),
            "origin": entry.origin,
            "evidence_url": entry.evidence_url,
            "excerpt": entry.excerpt,
            "field": entry.field,
            "matched": entry.matched,
            "checked_at": entry.checked_at,
            "version": entry.version,
            "description_sha256": entry.description_sha256,
            "at": entry.at,
        }
    )


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
    # Spec 122: whether the posting can engage someone based in Türkiye.
    eligibility: EligibilityOut


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
    return _as_job_outs(conn, list_jobs(conn, scope, status=status, source=source), scope)


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


def _as_job_outs(conn: Connection, rows: list[dict[str, str]], scope: TrackScope) -> list[JobOut]:
    """Many rows, with every row's eligibility read in one query (spec 122)."""
    views = eligibility_views(conn, scope)
    empty = EligibilityView(None, None)
    return [_as_job_out(row, scope, views.get(int(row["id"]), empty)) for row in rows]


def _as_one_job_out(conn: Connection, job: dict[str, str], scope: TrackScope) -> JobOut:
    return _as_job_outs(conn, [job], scope)[0]


def _as_job_out(job: dict[str, str], scope: TrackScope, eligibility: EligibilityView) -> JobOut:
    """The same conversion `/jobs` uses, so one row shape reaches the client."""
    deadline = (job.get("deadline") or "").strip()
    return JobOut.model_validate(
        {
            **job,
            "id": int(job["id"]),
            "eligibility": _eligibility_out(eligibility),
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
        return _as_one_job_out(
            conn,
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
        return _as_one_job_out(
            conn,
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
        job=_as_one_job_out(conn, job, scope) if job else None,
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
        previous=result.previous,
        current=result.current,
        job=_as_one_job_out(conn, result.job, scope),
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
    return _as_job_outs(conn, rows, scope)


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


# --- eligibility (spec 122) ---


class EligibilitySetIn(BaseModel):
    """The candidate's own assessment. `confirmed` and `excluded` need the
    URL the evidence was read at and the words that decide it. `checked_at`
    is a date or a UTC time; now when omitted."""

    verdict: EligibilityVerdict
    routes: list[EligibilityRoute] = []
    evidence_url: str = ""
    excerpt: str = ""
    checked_at: str | None = None


@tracker_router.get(
    "/tracker/{selector}/eligibility",
    operation_id="listJobEligibility",
    responses={
        404: {"model": ErrorOut, "description": "the selector named no job, or more than one"}
    },
)
def list_job_eligibility(
    selector: str,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("eligibility show"))],
) -> list[EligibilityEntryOut]:
    """`eligibility show`: a job's assessments, newest first."""
    from harrier.tracker.store import eligibility_history

    job_id = _job_id_for(conn, scope, selector)
    return [_eligibility_entry_out(entry) for entry in eligibility_history(conn, scope, job_id)]


@tracker_router.post(
    "/tracker/{selector}/eligibility",
    operation_id="setJobEligibility",
    dependencies=[Depends(require_token)],
    responses=TRACKER_ERRORS,
)
def set_job_eligibility(
    selector: str,
    body: EligibilitySetIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("eligibility set"))],
) -> EligibilityOut:
    """`eligibility set`: the same function the CLI calls. It stays the row's
    current assessment until the candidate records another."""
    from harrier.tracker.actions import TrackerActionError, set_eligibility

    try:
        view = set_eligibility(
            conn,
            scope,
            selector,
            verdict=body.verdict,
            routes=tuple(body.routes),
            evidence_url=body.evidence_url,
            excerpt=body.excerpt,
            checked_at=body.checked_at,
        )
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (TrackerActionError, TrackerError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _eligibility_out(view)


class EligibilityReassessIn(BaseModel):
    """Report by default; `apply` writes, as a second, separate request."""

    apply: bool = False


class EligibilityReassessOut(BaseModel):
    """Counts only, never an excerpt (spec 122)."""

    applied: bool
    examined: int
    assessed: int
    unchanged: int
    under_candidate: int
    disagrees: int
    rejected_skipped: int
    no_description: int
    by_verdict: dict[str, int]


@tracker_router.post(
    "/ops/eligibility-reassess",
    operation_id="reassessEligibility",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def reassess_job_eligibility(
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("eligibility reassess"))],
    body: EligibilityReassessIn = EligibilityReassessIn(),  # noqa: B008
) -> EligibilityReassessOut:
    """`eligibility reassess`: the same function the CLI calls. The rules
    run again on open rows with a stored description; nothing is written
    without `apply`."""
    from harrier.tracker.actions import reassess_eligibility

    report = reassess_eligibility(conn, scope, apply=body.apply)
    return EligibilityReassessOut(
        applied=report.applied,
        examined=report.examined,
        assessed=report.assessed,
        unchanged=report.unchanged,
        under_candidate=report.under_candidate,
        disagrees=report.disagrees,
        rejected_skipped=report.rejected_skipped,
        no_description=report.no_description,
        by_verdict=dict(report.by_verdict),
    )


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

# What an apply run route can answer 409 for: the track refusal every scoped
# route has, a non-default track from the browser (spec 101), and a run of
# the kind already active. Joined, so the contract names every cause.
APPLY_RUN_RESPONSES = merge_responses(
    APPLY_ERRORS,
    {409: {"description": "application documents from the browser are the default track's"}},
    RUN_CONFLICT_RESPONSES,
)
# `evaluate` never runs on another track: the track refusal answers first.
EVALUATE_RESPONSES = merge_responses(APPLY_ERRORS, RUN_CONFLICT_RESPONSES)

ARTIFACT_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "no such job, artifact kind, or the artifact is not produced yet"},
    **TOKEN_RESPONSES,
}


def _job_id_for(conn: Connection, scope: TrackScope, selector: str) -> int:
    from harrier.tracker.selector import resolve_selector

    try:
        return int(resolve_selector(conn, scope, selector)["id"])
    except SelectorError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _apply_params(
    conn: Connection,
    scope: TrackScope,
    operation: str,
    selector: str,
    text: str,
    *,
    no_ai: bool,
) -> RunParams:
    """Refuse another track, resolve the selector and stage the free text, in
    that order.

    The order matters: staging first would write a file for a job that does
    not exist, and nothing would ever consume or remove it.
    """
    if scope.track.id != DEFAULT_TRACK_ID:
        # These run on an academic track from the command line (spec 101);
        # from the browser they are the default track's, as `discover` is
        # (spec 095).
        raise HTTPException(
            status_code=409, detail=f"{operation} is not available on track {scope.track.slug}"
        )
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
    responses=APPLY_RUN_RESPONSES,
)
async def tailor_resume(
    selector: str,
    body: TailorIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("tailor"))],
    manager: Manager,
) -> RunOut:
    """The same `tailor` verb the CLI runs, as a run (spec 047)."""
    params = _apply_params(conn, scope, "tailor", selector, body.jd_text, no_ai=body.no_ai)
    return run_out(await manager.start("tailor", params))


@apply_router.post(
    "/apply/{selector}/cover-letter",
    operation_id="draftCoverLetter",
    dependencies=[Depends(require_token)],
    responses=APPLY_RUN_RESPONSES,
)
async def draft_cover_letter(
    selector: str,
    body: CoverLetterIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("cover-letter"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, "cover-letter", selector, body.notes, no_ai=False)
    return run_out(await manager.start("cover-letter", params))


@apply_router.post(
    "/apply/{selector}/answers",
    operation_id="draftAnswers",
    dependencies=[Depends(require_token)],
    responses=APPLY_RUN_RESPONSES,
)
async def draft_answers(
    selector: str,
    body: AnswersIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("answers"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, "answers", selector, body.questions, no_ai=False)
    return run_out(await manager.start("answers", params))


@apply_router.post(
    "/apply/{selector}/evaluate",
    operation_id="evaluateOffer",
    dependencies=[Depends(require_token)],
    responses=EVALUATE_RESPONSES,
)
async def evaluate_offer_route(
    selector: str,
    body: EvaluateIn,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("evaluate"))],
    manager: Manager,
) -> RunOut:
    params = _apply_params(conn, scope, "evaluate", selector, body.jd_text, no_ai=False)
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


# One set per route, each listing only what that route sends: a read cannot
# be refused a brief, and a write cannot meet a stored brief that no longer
# parses (spec 125).
BRIEF_SELECTOR_CAUSE = "the selector named no job, or more than one"

GET_BRIEF_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorOut,
        "description": CAUSE_JOIN.join((BRIEF_SELECTOR_CAUSE, "no brief is stored for the job")),
    },
    409: {
        "model": ErrorOut,
        "description": "the stored brief no longer parses, in the store's words; "
        "saving a brief replaces it",
    },
    **TOKEN_RESPONSES,
}

PUT_BRIEF_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorOut, "description": "the store refused the brief, in its words"},
    404: {"model": ErrorOut, "description": BRIEF_SELECTOR_CAUSE},
    **TOKEN_RESPONSES,
}


def _brief_out(brief: Brief) -> BriefOut:
    """The response from the brief the store parsed, never a second parse."""
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
    responses=GET_BRIEF_ERRORS,
)
def get_brief(
    selector: str, conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("brief show"))]
) -> BriefOut:
    """`brief show`. A read that requires the token: a brief holds the
    operator's own notes about an application (specs 047, 095)."""
    from harrier.apply.brief import BriefError, brief_text, load_brief

    job_id = _job_id_for(conn, scope, selector)
    if brief_text(conn, job_id) is None:
        raise HTTPException(status_code=404, detail=f"no brief for job {job_id}")
    try:
        # The loader letters and answers use, so a damaged brief is refused
        # here in the words it is refused there (review of PR #208).
        brief = load_brief(conn, job_id)
    except BriefError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return _brief_out(brief)


@apply_router.put(
    "/apply/{selector}/brief",
    operation_id="putBrief",
    dependencies=[Depends(require_token)],
    responses=PUT_BRIEF_ERRORS,
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
        stored = store_brief(conn, job_id, json.dumps(body))
    except BriefError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _brief_out(stored)


runs_router = APIRouter(route_class=DatabaseRoute)

# The run manager holds its runs in memory, so an id from before a restart
# is unknown too (spec 125).
RUN_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut, "description": "the run id names no run this server holds"}
}


@runs_router.post(
    "/runs",
    operation_id="startRun",
    dependencies=[Depends(require_token)],
    responses=merge_responses(TOKEN_RESPONSES, RUN_CONFLICT_RESPONSES),
)
async def start_run(body: StartRunIn, manager: Manager) -> RunOut:
    return run_out(await manager.start(body.kind))


@runs_router.get("/runs", operation_id="listRuns")
def list_runs(manager: Manager) -> list[RunOut]:
    return [run_out(run) for run in manager.list_runs()]


@runs_router.get("/runs/{run_id}", operation_id="getRun", responses=RUN_NOT_FOUND)
def get_run(run_id: str, manager: Manager) -> RunOut:
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run_out(run)


@runs_router.post(
    "/runs/{run_id}/cancel",
    operation_id="cancelRun",
    dependencies=[Depends(require_token)],
    responses=merge_responses(TOKEN_RESPONSES, RUN_NOT_FOUND),
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
        },
        **RUN_NOT_FOUND,
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


def _config_out(conn: Connection, kind: str) -> ConfigOut:
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
    responses=merge_responses(CONFIG_ERRORS, TOKEN_RESPONSES),
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
    # Before anything opens SQLite. A bad HARRIER_DATABASE_URL raises
    # StoreUrlError, and a Postgres one refuses with the CLI's text: nothing
    # serves requests from Postgres until spec 112 (spec 103).
    if store_target().is_postgres:
        raise RuntimeError(POSTGRES_NOT_YET)
    # logsetup said it was called by the CLI and by the API. Only the CLI ever
    # called it, so the process serving the browser had no configured root
    # logger and no identity redaction (spec 045).
    configure_logging()
    # uvicorn logs an unhandled exception on its own handler, which the
    # root's filters never see (spec 133).
    scrub_uvicorn_handlers()
    if is_demo_mode():
        seed_demo_db()
    manager = run_manager if run_manager is not None else RunManager()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        # When the app is served, never when it is imported or built: an
        # import is `just contract` on the host, whose data directory is the
        # container's, and removing inputs there deleted a running
        # discovery's upload (review of PR #208). Only the inputs of runs
        # this server's journal shows were cut off go.
        manager.release_interrupted_inputs()
        yield

    app = FastAPI(
        title="harrier",
        version=API_VERSION,
        description="Local-first job search automation API.",
        lifespan=lifespan,
    )
    app.state.run_manager = manager

    @app.exception_handler(RunConflictError)
    async def run_conflict(request: Request, error: RunConflictError) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        # A run of this kind is active with other options. Every route that
        # starts a run declares the 409 (`RUN_CONFLICT_RESPONSES`). Its
        # text is scrubbed as an HTTPException's is (spec 133).
        return JSONResponse(
            status_code=409, content=ErrorOut(detail=scrub_outbound(str(error))).model_dump()
        )

    @app.exception_handler(StarletteHTTPException)
    async def scrubbed_http_error(request: Request, error: StarletteHTTPException) -> Response:  # pyright: ignore[reportUnusedFunction]
        # One place for every `HTTPException(detail=str(error))`, rather
        # than each raise site: a string detail leaves the process scrubbed,
        # with its status and headers. Any other detail is answered as
        # FastAPI answers it; a 422 has its own handler and never comes here
        # (spec 133). Typed `str` by Starlette; FastAPI's takes any value.
        detail = cast(object, error.detail)
        if isinstance(detail, str):
            error.detail = scrub_outbound(detail)
        return await http_exception_handler(request, error)

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
    app.include_router(settings_router)
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
