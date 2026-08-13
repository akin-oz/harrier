"""LinkedIn guest-endpoint helpers (spec 009 port).

Description and poster extraction for individual LinkedIn job postings via
the public unauthenticated guest endpoint: no auth, no Apify credits. Lives
in screening (not sources) because it integrates with the description cache
and serves enrichment, backfill, and outreach consumers alike.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any, cast

from harrier.screening.descriptions import load_cached_description, save_description_cache
from harrier.screening.http import request_text, strip_html

logger = logging.getLogger(__name__)

GUEST_JD_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

_DESC_RE = re.compile(
    r'<div[^>]*class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>',
    re.S,
)
_POSTER_SECTION_RE = re.compile(
    r'(?:section|div)[^>]*class="[^"]*message-the-recruiter[^"]*"[^>]*>(.*?)</(?:section|div)>',
    re.S,
)
_POSTER_LINK_RE = re.compile(r'<a[^>]*class="[^"]*base-card__full-link[^"]*"[^>]*href="([^"#?]+)')
_POSTER_NAME_RE = re.compile(
    r'<h3[^>]*class="[^"]*base-main-card__title[^"]*"[^>]*>(.*?)</h3>', re.S
)
_POSTER_TITLE_RE = re.compile(
    r'<h4[^>]*class="[^"]*base-main-card__subtitle[^"]*"[^>]*>(.*?)</h4>', re.S
)

PUBLISHER_NAME_KEYS = (
    "posterFullName",
    "posterName",
    "jobPosterName",
    "publisherName",
    "recruiterName",
    "hiringManagerName",
    "posterDisplayName",
)
PUBLISHER_URL_KEYS = (
    "posterProfileUrl",
    "posterUrl",
    "jobPosterUrl",
    "publisherUrl",
    "recruiterProfileUrl",
    "hiringManagerUrl",
    "posterLinkedinUrl",
)
PUBLISHER_TITLE_KEYS = (
    "posterTitle",
    "posterHeadline",
    "jobPosterTitle",
    "publisherTitle",
    "recruiterTitle",
    "hiringManagerTitle",
)


def linkedin_job_id(url: str) -> str:
    """Job id from either URL shape: /jobs/view/1234567890 or the slugged
    /jobs/view/senior-frontend-engineer-at-x-1234567890, plus currentJobId."""
    if not url:
        return ""
    match = re.search(r"/jobs/view/(?:[^/?#]*?-)?(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]currentJobId=(\d+)", url)
    if match:
        return match.group(1)
    return ""


def extract_publisher_contact(item: dict[str, Any]) -> dict[str, str]:
    """{name, linkedin_url, title} from a raw Apify LinkedIn item, or {}.

    Different LinkedIn actors expose different field names, so a defensive
    key list is probed, including one level of nesting."""

    def _first(keys: tuple[str, ...]) -> str:
        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    name = _first(PUBLISHER_NAME_KEYS)
    linkedin_url = _first(PUBLISHER_URL_KEYS)
    title = _first(PUBLISHER_TITLE_KEYS)

    for nested_key in ("poster", "jobPoster", "recruiter", "hiringManager"):
        nested_raw = item.get(nested_key)
        if isinstance(nested_raw, dict):
            nested = cast("dict[str, Any]", nested_raw)
            if not name:
                name = str(
                    nested.get("name") or nested.get("fullName") or nested.get("displayName") or ""
                ).strip()
            if not linkedin_url:
                linkedin_url = str(
                    nested.get("profileUrl") or nested.get("url") or nested.get("linkedinUrl") or ""
                ).strip()
            if not title:
                title = str(nested.get("title") or nested.get("headline") or "").strip()

    if not name and not linkedin_url:
        return {}
    return {"name": name, "linkedin_url": linkedin_url, "title": title}


def extract_poster_from_html(html: str) -> dict[str, str]:
    """{name, linkedin_url, title} from a guest job-posting HTML page, or {}
    when the posting exposes no named recruiter. The link must be a person
    profile (/in/), never a company page."""
    section_match = _POSTER_SECTION_RE.search(html)
    block = section_match.group(1) if section_match else html
    link_match = _POSTER_LINK_RE.search(block)
    if not link_match:
        return {}
    linkedin_url = html_lib.unescape(link_match.group(1).strip())
    if "/in/" not in linkedin_url:
        return {}
    name_match = _POSTER_NAME_RE.search(block)
    title_match = _POSTER_TITLE_RE.search(block)
    name = html_lib.unescape(strip_html(name_match.group(1) if name_match else "")).strip()
    title = html_lib.unescape(strip_html(title_match.group(1) if title_match else "")).strip()
    if not name and not linkedin_url:
        return {}
    return {"name": name, "linkedin_url": linkedin_url, "title": title}


def fetch_linkedin_job_details(
    urls: list[str], *, timeout_seconds: int = 30
) -> dict[str, dict[str, object]]:
    """{input_url: {description, poster}} via the guest endpoint.

    Descriptions land in the cache on fetch. Callers that only need
    descriptions should use fetch_linkedin_jds, which short-circuits on
    cached URLs without a network call."""
    out: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        job_id = linkedin_job_id(url)
        if not job_id:
            continue
        try:
            html = request_text(
                GUEST_JD_URL.format(job_id=job_id), timeout_seconds=timeout_seconds, retries=2
            )
        except Exception as exc:
            logger.warning("LinkedIn guest fetch failed for %s: %s", job_id, exc)
            continue
        match = _DESC_RE.search(html)
        description = strip_html(match.group(1) if match else "").strip()[:8000]
        if description and not load_cached_description(url):
            save_description_cache(url, description)
        if not description:
            description = load_cached_description(url) or ""
        out[url] = {"description": description, "poster": extract_poster_from_html(html)}
    return out


def fetch_linkedin_jds(urls: list[str], *, timeout_seconds: int = 30) -> dict[str, str]:
    """{input_url: description}; cached URLs short-circuit without a fetch."""
    out: dict[str, str] = {}
    to_fetch: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = (raw or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        cached = load_cached_description(url)
        if cached:
            out[url] = cached
            continue
        to_fetch.append(url)
    if to_fetch:
        details = fetch_linkedin_job_details(to_fetch, timeout_seconds=timeout_seconds)
        for url, info in details.items():
            description = str(info.get("description") or "")
            if description:
                out[url] = description
    return out
