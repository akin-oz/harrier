"""FastAPI application: the read path over the tracker (spec 005).

The OpenAPI document generated from this app is the API contract (ADR-005).
Routes speak Pydantic models only; the web app speaks generated types only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Query
from pydantic import BaseModel

from harrier.db import connect, default_db_path
from harrier.tracker import list_jobs
from harrier_api.demo import demo_db_path, is_demo_mode, seed_demo_db

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


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect(demo_db_path() if is_demo_mode() else None)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]

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


def create_app() -> FastAPI:
    if is_demo_mode():
        seed_demo_db()
    app = FastAPI(
        title="harrier",
        version=API_VERSION,
        description="Local-first job search automation API.",
    )
    app.include_router(router)
    return app


app = create_app()
