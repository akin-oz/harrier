"""FastAPI application: the read path over the tracker (spec 005).

The OpenAPI document generated from this app is the API contract (ADR-005).
Routes speak Pydantic models only; the web app speaks generated types only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from harrier.db import default_db_path
from harrier.tracker import list_jobs
from harrier_api.capture_routes import capture_router
from harrier_api.demo import demo_db_path, is_demo_mode, seed_demo_db
from harrier_api.deps import Conn
from harrier_api.runs import Run, RunManager, RunState, format_sse

API_VERSION = "0.1.0"

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
    manual_reject: str
    manual_added: str
    created_at: str
    updated_at: str


class HealthOut(BaseModel):
    name: str
    version: str
    demo: bool
    database: str
    job_count: int


router = APIRouter()


@router.get("/health", operation_id="getHealth")
def health(conn: Conn) -> HealthOut:
    count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
    return HealthOut(
        name="harrier",
        version=API_VERSION,
        demo=is_demo_mode(),
        database=str(demo_db_path() if is_demo_mode() else default_db_path()),
        job_count=int(count[0]),
    )


@router.get("/jobs", operation_id="listJobs")
def jobs(
    conn: Conn,
    status: Annotated[JobStatus | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
) -> list[JobOut]:
    rows = list_jobs(conn, status=status, source=source)
    return [JobOut.model_validate({**row, "id": int(row["id"])}) for row in rows]


class RunOut(BaseModel):
    id: str
    kind: str
    state: RunState
    created_at: str
    started_at: str | None
    ended_at: str | None
    exit_code: int | None


class StartRunIn(BaseModel):
    kind: Literal["demo"]


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


def _run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        kind=run.kind,
        state=run.state,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        exit_code=run.exit_code,
    )


def get_manager(request: Request) -> RunManager:
    return cast(RunManager, request.app.state.run_manager)


Manager = Annotated[RunManager, Depends(get_manager)]

runs_router = APIRouter()


@runs_router.post("/runs", operation_id="startRun")
async def start_run(body: StartRunIn, manager: Manager) -> RunOut:
    return _run_out(await manager.start(body.kind))


@runs_router.get("/runs", operation_id="listRuns")
def list_runs(manager: Manager) -> list[RunOut]:
    return [_run_out(run) for run in manager.list_runs()]


@runs_router.get("/runs/{run_id}", operation_id="getRun")
def get_run(run_id: str, manager: Manager) -> RunOut:
    run = manager.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(run)


@runs_router.post("/runs/{run_id}/cancel", operation_id="cancelRun")
async def cancel_run(run_id: str, manager: Manager) -> RunOut:
    run = await manager.cancel(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _run_out(run)


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


def create_app(run_manager: RunManager | None = None) -> FastAPI:
    if is_demo_mode():
        seed_demo_db()
    app = FastAPI(
        title="harrier",
        version=API_VERSION,
        description="Local-first job search automation API.",
    )
    app.state.run_manager = run_manager if run_manager is not None else RunManager()
    app.include_router(router)
    app.include_router(runs_router)
    app.include_router(capture_router)
    return app


app = create_app()
