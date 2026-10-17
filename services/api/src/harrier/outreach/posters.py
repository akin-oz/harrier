"""Staging a job's poster as a pending contact candidate (specs 016, 150).

Two callers find a poster: the guest backfill (spec 016), which fetches
LinkedIn's job page, and discovery (spec 150), which reads the poster the
Apify actor already returned. Both stage through `stage_poster`, so a
poster is a pending candidate in the job's candidates artifact and only an
approval writes a contact.
"""

from __future__ import annotations

import re
from typing import cast
from urllib.parse import urlparse

from harrier.connection import Connection
from harrier.outreach.contacts import infer_relevance, normalize
from harrier.outreach.discovery import load_candidates_artifact, write_candidates_artifact
from harrier.tracker import list_contacts

_SCHEME = re.compile(r"([a-z][a-z0-9+.-]*)://", re.IGNORECASE)
_IN_SEGMENT = re.compile(r"^(/*)in(?=/|$)", re.IGNORECASE)


def profile_url(raw: str) -> str:
    """`https://www.linkedin.com/in/<slug>` for a usable profile link, else "".

    Usable: it parses, its scheme is https, it carries no userinfo, its host
    is linkedin.com or a subdomain of it, and its path starts with /in/. The
    actor links profiles on country subdomains (uk.linkedin.com), so one form
    lets the same person compare equal whichever subdomain named them
    (spec 150).
    """
    try:
        parsed = urlparse((raw or "").strip())
        host = (parsed.hostname or "").lower()
        has_userinfo = parsed.username is not None or parsed.password is not None
    except ValueError:
        return ""
    if parsed.scheme != "https" or has_userinfo:
        return ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "in":
        return ""
    return f"https://www.linkedin.com/in/{parts[1]}"


def _key(url: str, name: str) -> str:
    return normalize(profile_url(url) or url or name)


def _stored_profile_url(url: str) -> str:
    """The canonical form of a contact's stored link, read leniently.

    A contact typed by hand can lack the scheme or use http. That is not a
    link to stage, but it still names the person, so for comparison it is
    read as https (review of PR #381). A scheme counts only at the start,
    so a `://` in a query is not one; a link starting with `//` has none;
    and `/IN/` is `/in/` (post-merge review of spec 150)."""
    match = _SCHEME.match(url)
    if match:
        if match.group(1).lower() not in ("http", "https"):
            return ""
        url = url[match.end() :]
    elif url.startswith("//"):
        url = url[2:]
    host, slash, path = url.partition("/")
    path = _IN_SEGMENT.sub(r"\1in", path)
    return profile_url(f"https://{host}{slash}{path}")


def contact_url_keys(conn: Connection) -> set[str]:
    """The profile URLs already in the contacts table, in both their stored
    and their canonical form."""
    keys: set[str] = set()
    for contact in list_contacts(conn):
        url = (contact.get("linkedin_url") or "").strip()
        if url:
            keys.add(normalize(url))
            canonical = _stored_profile_url(url)
            if canonical:
                keys.add(normalize(canonical))
    return keys


def stage_poster(
    company: str,
    role: str,
    job_url: str,
    poster: dict[str, str],
    *,
    source: str,
    raw_query: str,
) -> bool:
    """Merge the poster into the job's candidates artifact as a pending
    candidate. Returns False when it is already staged."""
    poster_url = str(poster.get("linkedin_url") or "").strip()
    poster_name = str(poster.get("name") or "").strip()
    # Strict: a Storage error while reading must end this poster, not read
    # as "nothing staged" and upload a fresh list over the candidates and
    # their reviews (data-integrity review of PR #368).
    payload = load_candidates_artifact(company, role, strict=True) or {
        "company": company,
        "role": role,
        "job_url": job_url,
        "candidates": [],
    }
    candidates_raw = payload.get("candidates")
    candidates: list[object] = (
        cast("list[object]", candidates_raw) if isinstance(candidates_raw, list) else []
    )
    key = _key(poster_url, poster_name)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        existing_url = str(item.get("linkedin_url") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        existing_name = str(item.get("person_name") or "")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if _key(existing_url, existing_name) == key:
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
            "source": source,
            "linkedin_url": poster_url,
            "job_url": job_url,
            "contact_status": "candidate",
            "reply_status": "",
            "review_status": "pending",
            "raw_query": raw_query,
        }
    )
    payload["candidates"] = candidates
    write_candidates_artifact(company, role, payload)
    return True
