"""RemoteOK public feed importer (spec 008 port). Ingestion only.

RemoteOK's TOS requires a follow-link back to the source; the canonical url
stored on each tracker row covers that when applications cite the source.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from harrier.screening.http import request_json, strip_html
from harrier.screening.normalized import NormalizedJob, make_normalized_job

logger = logging.getLogger(__name__)

SOURCE_NAME = "remoteok"
API_URL = "https://remoteok.com/api"


def normalize_remoteok_job(item: dict[str, Any]) -> NormalizedJob:
    tags_raw: object = item.get("tags") or []
    tags = cast("list[object]", tags_raw) if isinstance(tags_raw, list) else []
    description = strip_html(str(item.get("description") or ""))[:4000]
    location = str(item.get("location") or "").strip() or "Remote"
    salary_min = item.get("salary_min") or 0
    salary_max = item.get("salary_max") or 0
    raw_compensation = ""
    if salary_min or salary_max:
        raw_compensation = f"{salary_min}-{salary_max} USD".strip("- ")
    return make_normalized_job(
        source=SOURCE_NAME,
        company=str(item.get("company") or "").strip(),
        title=str(item.get("position") or "").strip(),
        location=location,
        url=str(item.get("url") or item.get("apply_url") or "").strip(),
        description=description,
        created_at=str(item.get("date") or "").strip(),
        external_id=str(item.get("id") or item.get("slug") or "").strip(),
        raw_compensation=raw_compensation,
        remote_signal="remote_only_board",
        board_key="remoteok",
        metadata={"tags": tags},
        raw_payload=item,
    )


def fetch_remoteok_jobs() -> list[NormalizedJob]:
    payload = request_json(API_URL)
    if not isinstance(payload, list):
        raise RuntimeError("RemoteOK API did not return a list")
    # First element is a metadata/legal stub; skip it.
    raw_items: list[dict[str, Any]] = [
        cast("dict[str, Any]", item)
        for item in cast("list[object]", payload)[1:]
        if isinstance(item, dict)
    ]
    logger.info("RemoteOK fetch: %d raw items", len(raw_items))
    normalized = [normalize_remoteok_job(item) for item in raw_items]
    return [job for job in normalized if job["url"] and job["title"]]
