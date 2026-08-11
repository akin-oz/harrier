"""FastAPI application: the read path over the tracker (spec 005).

The OpenAPI document generated from this app is the API contract (ADR-005).
Routes speak Pydantic models only; the web app speaks generated types only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from harrier.db import default_db_path
from harrier.demo import repo_root
from harrier.tracker import list_jobs
from harrier_api.capture_routes import capture_router
from harrier_api.demo import demo_db_path, is_demo_mode, seed_demo_db
from harrier_api.deps import Conn
from harrier_api.localauth import (
    TOKEN_RESPONSES,
    TRUSTED_HOSTS,
    load_or_create_token,
    require_token,
)
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


@runs_router.post(
    "/runs",
    operation_id="startRun",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
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

config_router = APIRouter()


def _config_out(conn: sqlite3.Connection, kind: str) -> ConfigOut:
    from harrier.userconfig import get_config, list_config

    stored = get_config(conn, kind)
    if stored is not None:
        rows = {row["kind"]: row["updated_at"] for row in list_config(conn)}
        return ConfigOut(kind=kind, value=stored, source="store", updated_at=rows.get(kind))
    return ConfigOut(kind=kind, value=_file_value(kind), source="file", updated_at=None)


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


@config_router.get("/config", operation_id="listConfig")
def list_configuration(conn: Conn) -> list[ConfigOut]:
    from harrier.userconfig import KINDS

    return [_config_out(conn, kind) for kind in KINDS]


@config_router.get("/config/{kind}", operation_id="getConfig", responses={404: CONFIG_ERRORS[404]})
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
    app.include_router(config_router)
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
