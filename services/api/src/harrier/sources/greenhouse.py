"""Greenhouse public board importer (spec 008 port). Ingestion only."""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

from harrier.screening.http import request_json, strip_html
from harrier.screening.normalized import NormalizedJob, make_normalized_job

SOURCE_NAME = "greenhouse"


def extract_greenhouse_token(board_url: str) -> str:
    parsed = urlparse(board_url)
    parts = [part for part in parsed.path.split("/") if part]
    hostname = (parsed.hostname or "").lower()
    # Label-suffix match, not string suffix: badgreenhouse.io must not pass.
    if (hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io")) and parts:
        return parts[0]
    return ""


def normalize_greenhouse_job(item: dict[str, Any], board_url: str) -> NormalizedJob:
    token = extract_greenhouse_token(board_url)
    content = strip_html(str(item.get("content") or ""))
    location_raw: object = item.get("location") or {}
    location = (
        str(cast("dict[str, Any]", location_raw).get("name") or "").strip()
        if isinstance(location_raw, dict)
        else ""
    )
    return make_normalized_job(
        source=SOURCE_NAME,
        company=str(item.get("company_name") or token).strip(),
        title=str(item.get("title") or "").strip(),
        location=location,
        url=str(item.get("absolute_url") or "").strip(),
        description=content[:4000],
        created_at=str(item.get("updated_at") or "").strip(),
        external_id=str(item.get("id") or "").strip(),
        board_key=token,
        metadata={"board_url": board_url},
        raw_payload=item,
    )


def fetch_greenhouse_jobs(board_url: str) -> list[NormalizedJob]:
    token = extract_greenhouse_token(board_url)
    if not token:
        return []
    payload = request_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    raw_jobs: object = (
        cast("dict[str, Any]", payload).get("jobs", []) if isinstance(payload, dict) else []
    )
    if not isinstance(raw_jobs, list):
        return []
    return [
        normalize_greenhouse_job(cast("dict[str, Any]", item), board_url)
        for item in cast("list[object]", raw_jobs)
        if isinstance(item, dict)
    ]
