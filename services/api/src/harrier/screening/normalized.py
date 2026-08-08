"""The shared normalized job shape (spec 007).

Every source module produces this shape and nothing else; filtering, scoring,
and tracker append happen once, in the shared pipeline. Port of the old
repo's make_normalized_job and dedupe_normalized_jobs.
"""

from __future__ import annotations

import hashlib
import re
from typing import TypedDict


class NormalizedJob(TypedDict):
    job_key: str
    source: str
    source_label: str
    external_id: str
    external_job_id: str
    company: str
    title: str
    location: str
    url: str
    description: str
    created_at: str
    posted_at: str
    raw_compensation: str
    remote_signal: str
    metadata: dict[str, object]
    raw_payload: object


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def stable_key(*parts: str) -> str:
    raw = "||".join(part.strip() for part in parts if part and part.strip())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def make_normalized_job(
    *,
    source: str,
    company: str,
    title: str,
    location: str,
    url: str,
    description: str = "",
    created_at: str = "",
    external_id: str = "",
    raw_compensation: str = "",
    remote_signal: str = "",
    metadata: dict[str, object] | None = None,
    raw_payload: object | None = None,
    board_key: str = "",
    source_label: str = "",
) -> NormalizedJob:
    external_id = str(external_id or "").strip()
    url = str(url or "").strip()
    company = str(company or "").strip()
    title = str(title or "").strip()
    location = str(location or "").strip()
    description = str(description or "").strip()
    created_at = str(created_at or "").strip()
    board_key = normalize(board_key or company or source)
    source_label = source_label or f"{source}:{board_key or normalize(company or source)}"
    identity_key = external_id or url or stable_key(company, title, location)
    return NormalizedJob(
        job_key=stable_key(source, board_key, identity_key),
        source=source,
        source_label=source_label,
        external_id=external_id,
        external_job_id=external_id,
        company=company,
        title=title,
        location=location,
        url=url,
        description=description,
        created_at=created_at,
        posted_at=created_at,
        raw_compensation=str(raw_compensation or "").strip(),
        remote_signal=str(remote_signal or "").strip(),
        metadata=metadata or {},
        raw_payload=raw_payload if raw_payload is not None else {},
    )


def dedupe_normalized_jobs(jobs: list[NormalizedJob]) -> list[NormalizedJob]:
    """In-batch dedupe: external_id first, then url (old repo semantics)."""
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    unique: list[NormalizedJob] = []
    for job in jobs:
        external_id = normalize(job["external_id"] or job["external_job_id"])
        url = normalize(job["url"])
        if external_id and external_id in seen_ids:
            continue
        if url and url in seen_urls:
            continue
        if external_id:
            seen_ids.add(external_id)
        if url:
            seen_urls.add(url)
        unique.append(job)
    return unique
