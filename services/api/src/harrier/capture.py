"""Browser-capture manual add (spec 010 port of the old jobs.py command_add).

A captured job goes through the exact same scoring seam plus build_tracker_row
pipeline as automated discovery: no special-casing (`fit_score_for`, spec 077). Manual adds skip the
score cutoff by design: a human clicked add, so it lands as a prospect
regardless of score.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from harrier.connection import Connection
from harrier.scoring.model import fit_score_for
from harrier.screening.config import load_candidate_config
from harrier.screening.descriptions import (
    enrich_job_description_for_scoring,
    save_description_cache,
)
from harrier.screening.eligibility import assess_eligibility
from harrier.screening.focused import (
    ScreeningDocument,
    failed_gate,
    focused_score,
    load_screening,
)
from harrier.screening.normalized import make_normalized_job, normalize
from harrier.screening.pipeline import build_tracker_row
from harrier.tracker import DuplicateJobError, add_job
from harrier.tracks import Scope

logger = logging.getLogger(__name__)

MAX_DESCRIPTION_CHARS = 4000

# `unconfigured`: a focused track with no screening document (spec 137).
CaptureStatus = Literal["added", "duplicate", "invalid", "unconfigured"]


@dataclass
class CaptureResult:
    status: CaptureStatus
    message: str


def add_captured_job(
    conn: Connection,
    scope: Scope,
    *,
    company: str,
    title: str,
    location: str = "",
    url: str = "",
    source: str = "manual",
    description: str = "",
    enrich: bool = True,
    deadline: str = "",
) -> CaptureResult:
    company = company.strip()
    title = title.strip()
    if not company or not title:
        return CaptureResult(status="invalid", message="company and title are required")

    description = description.strip()[:MAX_DESCRIPTION_CHARS]
    source = source.strip() or "manual"
    if scope.track.kind == "academic":
        return _add_academic(
            conn,
            scope,
            company=company,
            title=title,
            location=location.strip(),
            url=url.strip(),
            source=source,
            description=description,
            deadline=deadline.strip(),
        )
    document: ScreeningDocument | None = None
    if scope.track.kind == "focused":
        # Never the committed example: a demo weight would rank real rows.
        document = load_screening(conn, scope)
        if document is None:
            slug = scope.track.slug
            return CaptureResult(
                status="unconfigured",
                message=(
                    f"track {slug} has no screening configuration; "
                    f"harrier --track {slug} profile put screening --file PATH"
                ),
            )
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
    # A person pasting a job URL is the posting that most needs enrichment and
    # was the one path that never got it. The discovery pipeline enriches a
    # thin ATS posting before scoring; capture did not, so a manual add with a
    # URL and no description was scored on its title alone, and left no cached
    # description, so `reevaluate` could not fix it later either (spec 033).
    #
    # `enrich_job_description_for_scoring` reaches the network only for a URL
    # on the ATS allow-list, and consults the local cache first. `enrich=False`
    # is for callers that must not make a request.
    if enrich:
        job = enrich_job_description_for_scoring(job)
        description = job["description"]
    if document is None:
        fit = fit_score_for(job, candidate_cfg)
        row = build_tracker_row(job, fit.score, fit.reasons, fit.version)
    else:
        # The track's own score (spec 137). A manual add lands whatever its
        # gates say, as on the industry track; a failed gate is named.
        focused = focused_score(job, document)
        reasons = list(focused.reasons)
        gate = failed_gate(job, document, candidate_cfg)
        if gate is not None:
            reasons.append(f"gate={gate}")
        row = build_tracker_row(job, focused.score, reasons, focused.version)
    row["notes"] = f"{row['notes']}; manual_added={datetime.now(UTC).date().isoformat()}"
    if deadline.strip():
        row["deadline"] = deadline.strip()

    try:
        # Judged from whatever text was given; with no description that is
        # usually `unknown` (spec 122).
        add_job(conn, row, scope=scope, eligibility=assess_eligibility(job))
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


def _add_academic(
    conn: Connection,
    scope: Scope,
    *,
    company: str,
    title: str,
    location: str,
    url: str,
    source: str,
    description: str,
    deadline: str,
) -> CaptureResult:
    """A position on an academic track, entered by hand (spec 093).

    No industry candidate configuration, no fit score, no network enrichment
    and no remote filter: the screening and scoring rules are the industry
    kind's, and a score with no basis is worse than no score. The row is a
    prospect holding what the operator typed. The pasted description is cached
    as for any manual add, so the `created` event can pin its hash.
    """
    today = datetime.now(UTC).date().isoformat()
    row = {
        "company": company,
        "title": title,
        "location": location,
        "url": url,
        "source": source,
        "added_at": today,
        "status": "prospect",
        "notes": f"manual_added={today}",
        "deadline": deadline,
    }
    try:
        add_job(conn, row, scope=scope)
    except DuplicateJobError:
        return CaptureResult(status="duplicate", message=f"Already in tracker: {company}: {title}")
    if url and description:
        try:
            save_description_cache(url, description)
        except OSError as error:
            logger.warning("description cache write failed: %s", error)
    return CaptureResult(status="added", message=f"Added: {company}: {title}")
