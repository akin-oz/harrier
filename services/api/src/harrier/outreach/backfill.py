"""Guest poster backfill (spec 016 port of
backfill_linkedin_posters_guest.py).

For every non-rejected tracker row with a LinkedIn job URL, fetch the
poster from the public guest endpoint and upsert it as a contact unless
one with the same linkedin_url already exists. Safe to re-run; dry runs
write nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import cast

from harrier.outreach.contacts import infer_relevance, normalize, upsert_contact
from harrier.screening.linkedin import fetch_linkedin_job_details, linkedin_job_id
from harrier.tracker import list_contacts, list_jobs

BATCH = 25


@dataclass
class BackfillSummary:
    checked: int = 0
    added: int = 0
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
            poster = cast("dict[str, str]", poster_raw) if isinstance(poster_raw, dict) else {}
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
                summary.added += 1
                existing.add(key)
                summary.lines.append(
                    f"[dry-run] would add: {poster_name} ({poster_url}) "
                    f"-> {row.get('company', '')}: {row.get('title', '')}"
                )
                continue
            try:
                upsert_contact(
                    conn,
                    company=row.get("company", ""),
                    role=row.get("title", ""),
                    job_url=url,
                    person_name=poster_name or "Job poster",
                    person_title=str(poster.get("title", "")),
                    linkedin_url=poster_url,
                    source="linkedin",
                    relevance=infer_relevance(str(poster.get("title", ""))),
                    notes="auto-linked from LinkedIn guest endpoint backfill",
                )
                summary.added += 1
                existing.add(key)
            except Exception as exc:
                summary.errors.append(f"{row.get('company', '?')}: {exc}")
    return summary
