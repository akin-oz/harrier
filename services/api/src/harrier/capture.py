"""Browser-capture manual add (spec 010 port of the old jobs.py command_add).

A captured job goes through the exact same score_job plus build_tracker_row
pipeline as automated discovery: no special-casing. Manual adds skip the
score cutoff by design: a human clicked add, so it lands as a prospect
regardless of score.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from harrier.screening.config import load_candidate_config
from harrier.screening.descriptions import save_description_cache
from harrier.screening.normalized import make_normalized_job, normalize
from harrier.screening.pipeline import build_tracker_row
from harrier.screening.rules import score_job
from harrier.tracker import DuplicateJobError, add_job

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 4000

CaptureStatus = Literal["added", "duplicate", "invalid"]


@dataclass
class CaptureResult:
    status: CaptureStatus
    message: str


def add_captured_job(
    conn: sqlite3.Connection,
    *,
    company: str,
    title: str,
    location: str = "",
    url: str = "",
    source: str = "manual",
    description: str = "",
) -> CaptureResult:
    company = company.strip()
    title = title.strip()
    if not company or not title:
        return CaptureResult(status="invalid", message="company and title are required")

    description = description.strip()[:MAX_DESCRIPTION_CHARS]
    source = source.strip() or "manual"
    candidate_cfg = load_candidate_config(conn)
    job = make_normalized_job(
        source=source,
        company=company,
        title=title,
        location=location.strip(),
        url=url.strip(),
        description=description,
        source_label=f"manual:{normalize(company) or 'manual'}",
    )
    score, reasons = score_job(job, candidate_cfg)
    row = build_tracker_row(job, score, reasons)
    row["notes"] = f"{row['notes']}; manual_added={datetime.now(UTC).date().isoformat()}"

    try:
        add_job(conn, row)
    except DuplicateJobError:
        return CaptureResult(status="duplicate", message=f"Already in tracker: {company}: {title}")

    if job["url"] and description:
        try:
            save_description_cache(job["url"], description)
        except OSError as error:
            # The tracker insert already committed; a cache failure must not
            # turn a successful capture into a 500 (which would 409 on retry).
            # Log without job content.
            logger.warning("description cache write failed: %s", error)
    return CaptureResult(status="added", message=f"Added: {company}: {title}")
