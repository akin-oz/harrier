"""Lever hosted postings importer (spec 008 port). Ingestion only.

EU-hosted boards (jobs.eu.lever.co) use the EU API base; pagination at 100
per page. Both pinned by tests.
"""

from __future__ import annotations

from typing import Any, cast
from urllib.parse import urlparse

from harrier.screening.http import request_json, strip_html
from harrier.screening.normalized import NormalizedJob, make_normalized_job

SOURCE_NAME = "lever"
PAGE_SIZE = 100


def extract_lever_company(value: str) -> str:
    if "://" not in value:
        return value.strip().strip("/")
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    # Label-suffix match, not substring: cleverlever.co.example must not pass.
    if not (hostname == "lever.co" or hostname.endswith(".lever.co")):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else ""


def extract_lever_api_base(value: str) -> str:
    if "://" not in value:
        return "https://api.lever.co"
    parsed = urlparse(value)
    if parsed.netloc.lower().startswith("jobs.eu.lever.co"):
        return "https://api.eu.lever.co"
    return "https://api.lever.co"


def normalize_lever_job(item: dict[str, Any], company: str) -> NormalizedJob:
    categories_raw: object = item.get("categories") or {}
    categories = cast("dict[str, Any]", categories_raw) if isinstance(categories_raw, dict) else {}
    location = str(categories.get("location") or item.get("workplaceType") or "").strip()
    compensation = ""
    salary_range: object = item.get("salaryRange") or {}
    if isinstance(salary_range, dict):
        salary = cast("dict[str, Any]", salary_range)
        compensation = " ".join(
            str(salary.get(key) or "").strip() for key in ("currency", "interval")
        ).strip()
    return make_normalized_job(
        source=SOURCE_NAME,
        company=str(item.get("company") or company).strip(),
        title=str(item.get("text") or "").strip(),
        location=location,
        url=str(item.get("hostedUrl") or "").strip(),
        description=strip_html(str(item.get("description") or item.get("descriptionPlain") or ""))[
            :4000
        ],
        created_at=str(item.get("createdAt") or "").strip(),
        external_id=str(item.get("id") or "").strip(),
        raw_compensation=compensation,
        board_key=company,
        metadata={"categories": categories},
        raw_payload=item,
    )


def fetch_lever_jobs(board_url_or_company: str) -> list[NormalizedJob]:
    company = extract_lever_company(board_url_or_company)
    if not company:
        return []
    api_base = extract_lever_api_base(board_url_or_company)
    jobs: list[NormalizedJob] = []
    skip = 0
    while True:
        payload = request_json(
            f"{api_base}/v0/postings/{company}?mode=json&skip={skip}&limit={PAGE_SIZE}"
        )
        if not isinstance(payload, list) or not payload:
            break
        page = cast("list[object]", payload)
        jobs.extend(
            normalize_lever_job(cast("dict[str, Any]", item), company)
            for item in page
            if isinstance(item, dict)
        )
        if len(page) < PAGE_SIZE:
            break
        skip += PAGE_SIZE
    return jobs
