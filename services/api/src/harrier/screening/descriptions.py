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
SCORING_ENRICH_HOSTS: frozenset[str] = frozenset(
    {
        "greenhouse.io",
        "ashbyhq.com",
        "lever.co",
        "workable.com",
    }
)


def enrich_url_allowed(url: str) -> bool:
    """Strict allow-list for enrichment fetches (PR #4 review finding).

    Job URLs come from external feeds. The old substring hint check accepted
    userinfo tricks (https://greenhouse.io@169.254.169.254/) and suffix
    spoofs (greenhouse.io.attacker.example). Requires an http(s) scheme, no
    userinfo, and a hostname that is an approved ATS host or a dot-prefixed
    subdomain of one. Redirect targets are validated with the same rule
    (http.request_text's url_allowed hook).
    """
    # Malformed netlocs (https://[) raise ValueError on attribute access; a
    # bad feed URL must not stop a screening batch.
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if not hostname:
        return False
    return any(
        hostname == allowed or hostname.endswith(f".{allowed}") for allowed in SCORING_ENRICH_HOSTS
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


def cache_job_descriptions(jobs: list[NormalizedJob]) -> int:
    """Cache descriptions for every job carrying one (spec 009).

    The Apify cost-saver: everything fetched is cached, including jobs that
    screening will reject, so re-evaluation and artifact generation never
    re-run the actor for the same URLs. Called by the run path."""
    cached = 0
    for job in jobs:
        url = job["url"].strip()
        description = job["description"].strip()
        if url and description:
            save_description_cache(url, description)
            cached += 1
    return cached


def should_enrich_description_for_scoring(job: NormalizedJob) -> bool:
    if len(job["description"].strip()) >= MIN_DESCRIPTION_LENGTH_FOR_SCORING:
        return False
    url = job["url"].strip()
    if not url:
        return False
    return enrich_url_allowed(url)


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

    # 3. Fetch from a supported ATS URL (Greenhouse, Ashby, Lever, Workable),
    #    validating the URL and every redirect target against the allow-list.
    try:
        html = http.request_text(url, url_allowed=enrich_url_allowed)
    except Exception as exc:
        logger.warning("Could not enrich scoring description for %s: %s", url, exc)
        return job
    description = http.strip_html(html)[:8000]
    if not description:
        return job
    enriched = job.copy()
    enriched["description"] = description
    return enriched
