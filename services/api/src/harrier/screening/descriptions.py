"""URL-keyed description cache and scoring enrichment (spec 007 port).

The cache saves Apify re-billing and re-fetches: once a description is seen
for a URL, reevaluation and artifact generation never fetch it again. Lives
under the data directory (never-in-git).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from harrier.db import data_dir
from harrier.screening import http
from harrier.screening.normalized import NormalizedJob

logger = logging.getLogger(__name__)

MIN_DESCRIPTION_LENGTH_FOR_SCORING = 120
SCORING_ENRICH_HOST_HINTS: tuple[str, ...] = (
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "workable.com",
)


def _cache_dir() -> Path:
    return data_dir() / "descriptions"


def _description_cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    return _cache_dir() / f"{key}.json"


def save_description_cache(url: str, description: str) -> None:
    if not url or not description:
        return
    _cache_dir().mkdir(parents=True, exist_ok=True)
    _description_cache_path(url).write_text(
        json.dumps({"url": url, "description": description}, ensure_ascii=False),
        encoding="utf-8",
    )


def load_cached_description(url: str) -> str:
    if not url:
        return ""
    path = _description_cache_path(url)
    if not path.is_file():
        return ""
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(parsed, dict):
        return str(parsed.get("description", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    return ""


def should_enrich_description_for_scoring(job: NormalizedJob) -> bool:
    if len(job["description"].strip()) >= MIN_DESCRIPTION_LENGTH_FOR_SCORING:
        return False
    url = job["url"].strip()
    if not url:
        return False
    host = urlparse(url).netloc.lower()
    return any(hint in host for hint in SCORING_ENRICH_HOST_HINTS)


def enrich_job_description_for_scoring(job: NormalizedJob) -> NormalizedJob:
    url = job["url"].strip()

    # 1. Already has enough description: nothing to do. Still check the cache
    #    in case a richer version was stored manually.
    if not should_enrich_description_for_scoring(job):
        if url and len(job["description"]) < MIN_DESCRIPTION_LENGTH_FOR_SCORING:
            cached = load_cached_description(url)
            if cached:
                enriched = job.copy()
                enriched["description"] = cached
                return enriched
        return job

    # 2. The local cache first (covers WTTJ and LinkedIn manual adds).
    if url:
        cached = load_cached_description(url)
        if cached:
            enriched = job.copy()
            enriched["description"] = cached
            return enriched

    # 3. Fetch from a supported ATS URL (Greenhouse, Ashby, Lever, Workable).
    try:
        html = http.request_text(url)
    except Exception as exc:
        logger.warning("Could not enrich scoring description for %s: %s", url, exc)
        return job
    description = http.strip_html(html)[:8000]
    if not description:
        return job
    enriched = job.copy()
    enriched["description"] = description
    return enriched
