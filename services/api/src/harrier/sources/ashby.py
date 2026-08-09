"""Ashby board importer (spec 008 port). Ingestion only.

The posting API is tried first; when it fails (some boards 404 it), the
public board HTML carries the same data in window.__appData, walked
tolerantly for job-shaped nodes. The fallback is pinned by a fixture test.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, cast
from urllib.parse import urlparse

from harrier.screening.http import request_json, request_text, strip_html
from harrier.screening.normalized import NormalizedJob, make_normalized_job

logger = logging.getLogger(__name__)

SOURCE_NAME = "ashby"
_APP_DATA_RE = re.compile(r"window\.__appData\s*=\s*(\{.*?\});", re.DOTALL)


def extract_ashby_board(board_url: str) -> str:
    parsed = urlparse(board_url)
    if "jobs.ashbyhq.com" not in parsed.netloc.lower():
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else ""


def walk_ashby_jobs(node: object, found: list[dict[str, Any]]) -> None:
    """Collect job-shaped dict nodes (title-ish and id-ish keys) anywhere in
    the payload; Ashby's app data nests them differently per board."""
    if isinstance(node, dict):
        typed = cast("dict[str, Any]", node)
        lowered = {str(key).lower() for key in typed}
        if lowered & {"title", "jobtitle", "postingtitle"} and lowered & {
            "id",
            "_id",
            "jobid",
            "jobidstring",
        }:
            found.append(typed)
        for value in typed.values():
            walk_ashby_jobs(value, found)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            walk_ashby_jobs(item, found)


def _coalesce_location(item: dict[str, Any]) -> str:
    locations: list[str] = []
    for candidate in (
        item.get("location"),
        item.get("locationName"),
        item.get("locationNameOverride"),
        item.get("employmentLocation"),
        item.get("jobLocation"),
    ):
        if isinstance(candidate, dict):
            entry = cast("dict[str, Any]", candidate)
            value = str(
                entry.get("name") or entry.get("location") or entry.get("description") or ""
            ).strip()
            if value:
                locations.append(value)
        elif isinstance(candidate, list):
            for raw in cast("list[object]", candidate):
                if isinstance(raw, dict):
                    entry = cast("dict[str, Any]", raw)
                    value = str(entry.get("name") or entry.get("location") or "").strip()
                else:
                    value = str(raw).strip()
                if value:
                    locations.append(value)
        else:
            value = str(candidate or "").strip()
            if value:
                locations.append(value)
    return " | ".join(dict.fromkeys(locations))


def normalize_ashby_job(item: dict[str, Any], board_url: str) -> NormalizedJob:
    board = extract_ashby_board(board_url)
    location = _coalesce_location(item)

    description_parts: list[str] = []
    for key in (
        "descriptionPlain",
        "descriptionText",
        "description",
        "summary",
        "teamName",
        "departmentName",
        "mission",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            description_parts.append(strip_html(value.strip()))

    compensation = ""
    compensation_data: object = item.get("compensation") or item.get("salary") or {}
    if isinstance(compensation_data, dict):
        comp = cast("dict[str, Any]", compensation_data)
        parts = [
            str(comp[key]).strip()
            for key in ("summary", "currencyCode", "interval", "minValue", "maxValue")
            if comp.get(key) not in (None, "")
        ]
        compensation = " ".join(parts).strip()

    job_id = str(item.get("id") or item.get("_id") or item.get("jobId") or "").strip()
    url = (
        str(
            item.get("jobUrl")
            or item.get("absoluteUrl")
            or item.get("url")
            or item.get("applyUrl")
            or ""
        ).strip()
        or f"{board_url.rstrip('/')}/{job_id}"
    )
    return make_normalized_job(
        source=SOURCE_NAME,
        company=str(item.get("companyName") or item.get("organizationName") or board).strip(),
        title=str(
            item.get("title") or item.get("jobTitle") or item.get("postingTitle") or ""
        ).strip(),
        location=location,
        url=url,
        description=" | ".join(description_parts)[:4000],
        created_at=str(
            item.get("postedAt") or item.get("publishedAt") or item.get("createdAt") or ""
        ).strip(),
        external_id=job_id,
        raw_compensation=compensation,
        board_key=board,
        metadata={"board_url": board_url, "source_type": "ashby_public_api"},
        raw_payload=item,
    )


def fetch_ashby_jobs_via_api(board_url: str) -> list[NormalizedJob]:
    board = extract_ashby_board(board_url)
    if not board:
        return []
    try:
        payload = request_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
        )
    except Exception as exc:
        logger.warning("Ashby API fetch failed for %s: %s", board_url, exc)
        return []
    if not isinstance(payload, dict):
        return []
    typed = cast("dict[str, Any]", payload)
    raw_jobs: object = typed.get("jobs") or typed.get("jobPostings") or typed.get("postings") or []
    if not isinstance(raw_jobs, list):
        return []
    return [
        normalize_ashby_job(cast("dict[str, Any]", item), board_url)
        for item in cast("list[object]", raw_jobs)
        if isinstance(item, dict)
    ]


def fetch_ashby_jobs(board_url: str) -> list[NormalizedJob]:
    jobs = fetch_ashby_jobs_via_api(board_url)
    if jobs:
        return jobs
    try:
        html = request_text(board_url)
    except Exception as exc:
        logger.warning("Ashby HTML fetch failed for %s: %s", board_url, exc)
        return []
    match = _APP_DATA_RE.search(html)
    if not match:
        return []
    try:
        payload: object = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        logger.warning("Ashby HTML payload parse failed for %s: %s", board_url, exc)
        return []
    raw_jobs: list[dict[str, Any]] = []
    walk_ashby_jobs(payload, raw_jobs)
    return [normalize_ashby_job(item, board_url) for item in raw_jobs if item]
