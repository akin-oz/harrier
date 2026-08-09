"""Guest poster backfill as staged discovery (spec 016).

Stated change from the old backfill_linkedin_posters_guest.py, which
wrote contacts directly: discovered posters are STAGED into each job's
candidates artifact with review_status pending, honoring the
approval-only contact-write invariant. Approval happens through the
normal harrier contacts approve path. Safe to re-run; dry runs write
nothing at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import cast

from harrier.outreach.contacts import infer_relevance, normalize
from harrier.outreach.discovery import (
    load_candidates_artifact,
    write_candidates_artifact,
)
from harrier.screening.linkedin import fetch_linkedin_job_details, linkedin_job_id
from harrier.tracker import list_contacts, list_jobs

BATCH = 25


@dataclass
class BackfillSummary:
    checked: int = 0
    staged: int = 0
    skipped_existing: int = 0
    no_poster: int = 0
    errors: list[str] = field(default_factory=list[str])
    lines: list[str] = field(default_factory=list[str])


def _existing_contact_keys(conn: sqlite3.Connection) -> set[str]:
    keys: set[str] = set()
    for contact in list_contacts(conn):
        url = (contact.get("linkedin_url") or "").strip()
        if url:
            keys.add(normalize(url))
    return keys


def _linkedin_rows(conn: sqlite3.Connection) -> list[tuple[dict[str, str], str]]:
    rows: list[tuple[dict[str, str], str]] = []
    for row in list_jobs(conn):
        url = (row.get("url") or "").strip()
        if not url or "linkedin.com/jobs" not in url.lower():
            continue
        if (row.get("status") or "").strip().lower() == "rejected":
            continue
        if not linkedin_job_id(url):
            continue
        rows.append((row, url))
    return rows


def _stage_poster(row: dict[str, str], job_url: str, poster: dict[str, str]) -> bool:
    """Merge the poster into the job's candidates artifact as a pending
    candidate. Returns False when it is already staged."""
    company = row.get("company", "")
    role = row.get("title", "")
    poster_url = str(poster.get("linkedin_url") or "").strip()
    poster_name = str(poster.get("name") or "").strip()
    payload = load_candidates_artifact(company, role) or {
        "company": company,
        "role": role,
        "job_url": job_url,
        "candidates": [],
    }
    candidates_raw = payload.get("candidates")
    candidates: list[object] = (
        cast("list[object]", candidates_raw) if isinstance(candidates_raw, list) else []
    )
    key = normalize(poster_url or poster_name)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        existing_url = str(item.get("linkedin_url") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        existing_name = str(item.get("person_name") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if normalize(existing_url or existing_name) == key:
            return False
    candidates.append(
        {
            "person_name": poster_name or "Job poster",
            "company": company,
            "applied_job_title": role,
            "person_title": str(poster.get("title", "")),
            "relevance": infer_relevance(str(poster.get("title", ""))),
            "fit_score": "",
            "fit_reason": "poster of the tracked job posting",
            "location": "",
            "source": "linkedin_guest",
            "linkedin_url": poster_url,
            "job_url": job_url,
            "contact_status": "candidate",
            "reply_status": "",
            "review_status": "pending",
            "raw_query": "guest_poster_backfill",
        }
    )
    payload["candidates"] = candidates
    write_candidates_artifact(company, role, payload)
    return True


def backfill_posters(
    conn: sqlite3.Connection, *, limit: int = 0, dry_run: bool = False
) -> BackfillSummary:
    summary = BackfillSummary()
    rows = _linkedin_rows(conn)
    if limit > 0:
        rows = rows[:limit]
    summary.checked = len(rows)
    if not rows:
        return summary

    existing = _existing_contact_keys(conn)
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
            key = normalize(poster_url or poster_name)
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
                if _stage_poster(row, url, poster):
                    summary.staged += 1
                    summary.lines.append(
                        f"staged for review: {poster_name} -> {row.get('company', '')}"
                    )
                else:
                    summary.skipped_existing += 1
            except Exception as exc:
                summary.errors.append(f"{row.get('company', '?')}: {exc}")
    return summary
