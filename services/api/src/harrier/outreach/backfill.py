"""Guest poster backfill as staged discovery (spec 016).

Stated change from the old backfill_linkedin_posters_guest.py, which
wrote contacts directly: discovered posters are STAGED into each job's
candidates artifact with review_status pending, honoring the
approval-only contact-write invariant. Approval happens through the
normal harrier contacts approve path. Safe to re-run; dry runs write
nothing at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from harrier.connection import Connection
from harrier.outreach.contacts import normalize
from harrier.outreach.posters import contact_url_keys, profile_url, stage_poster
from harrier.screening.linkedin import fetch_linkedin_job_details, linkedin_job_id
from harrier.tracker import list_jobs
from harrier.tracks import Scope

BATCH = 25


@dataclass
class BackfillSummary:
    checked: int = 0
    staged: int = 0
    skipped_existing: int = 0
    no_poster: int = 0
    errors: list[str] = field(default_factory=list[str])
    lines: list[str] = field(default_factory=list[str])


def _linkedin_rows(conn: Connection, scope: Scope) -> list[tuple[dict[str, str], str]]:
    rows: list[tuple[dict[str, str], str]] = []
    for row in list_jobs(conn, scope):
        url = (row.get("url") or "").strip()
        if not url or "linkedin.com/jobs" not in url.lower():
            continue
        if (row.get("status") or "").strip().lower() == "rejected":
            continue
        if not linkedin_job_id(url):
            continue
        rows.append((row, url))
    return rows


def backfill_posters(
    conn: Connection, scope: Scope, *, limit: int = 0, dry_run: bool = False
) -> BackfillSummary:
    summary = BackfillSummary()
    rows = _linkedin_rows(conn, scope)
    if limit > 0:
        rows = rows[:limit]
    summary.checked = len(rows)
    if not rows:
        return summary

    existing = contact_url_keys(conn)
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        details = fetch_linkedin_job_details([url for _, url in batch])
        for row, url in batch:
            info = details.get(url) or {}
            poster_raw: object = info.get("poster")
            poster: dict[str, str] = (
                cast("dict[str, str]", poster_raw) if isinstance(poster_raw, dict) else {}
            )
            poster_url = str(poster.get("linkedin_url") or "").strip()
            poster_name = str(poster.get("name") or "").strip()
            if not poster_url and not poster_name:
                summary.no_poster += 1
                continue
            # Canonical, so a contact stored under www. matches a poster the
            # guest page links on a country subdomain.
            key = normalize(profile_url(poster_url) or poster_url or poster_name)
            if key in existing:
                summary.skipped_existing += 1
                continue
            if dry_run:
                summary.staged += 1
                summary.lines.append(
                    f"[dry-run] would stage: {poster_name} ({poster_url}) "
                    f"-> {row.get('company', '')}: {row.get('title', '')}"
                )
                continue
            try:
                if stage_poster(
                    row.get("company", ""),
                    row.get("title", ""),
                    url,
                    poster,
                    source="linkedin_guest",
                    raw_query="guest_poster_backfill",
                ):
                    summary.staged += 1
                    summary.lines.append(
                        f"staged for review: {poster_name} -> {row.get('company', '')}"
                    )
                else:
                    summary.skipped_existing += 1
            except Exception as exc:
                summary.errors.append(f"{row.get('company', '?')}: {exc}")
    return summary
