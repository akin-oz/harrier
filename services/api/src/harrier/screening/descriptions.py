"""URL-keyed description cache and scoring enrichment (spec 007 port).

The cache saves Apify re-billing and re-fetches: once a description is seen
for a URL, reevaluation and artifact generation never fetch it again. Lives
under the data directory (never-in-git).
"""

from __future__ import annotations

import hashlib
import json
import logging
from urllib.parse import urlparse

from harrier.filestore import files
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


def _description_cache_key(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()[:24]
    return f"descriptions/{digest}.json"


def save_description_cache(url: str, description: str) -> None:
    """Replace the entry whole or not at all (spec 007 amendment).

    Written in place, a save cut off between truncate and write left part of
    an entry, and a description no encoding can write left it empty: either
    way the previous description read as missing. The bytes are encoded
    before any file is touched, written beside the entry and renamed over
    it, so a failed save raises and leaves the previous entry as it was. The
    JSON is unindented, as it always was, which is why this does not go
    through write_json_atomic.
    """
    if not url or not description:
        return
    payload = json.dumps({"url": url, "description": description}, ensure_ascii=False)
    files().put(_description_cache_key(url), payload.encode("utf-8"), atomic=True)


def load_cached_description(url: str) -> str:
    if not url:
        return ""
    try:
        raw = files().read(_description_cache_key(url))
        if raw is None:
            return ""
        parsed: object = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as error:
        # ValueError covers malformed JSON and bytes that are not UTF-8, as a
        # write cut off mid-character leaves them. A damaged entry reads as
        # missing; it must never refuse the status change that hashes it
        # (spec 079 amendment). It is logged, by its class and the family
        # only, never the URL or the key (spec 107, "Callers keep their
        # rules on read failure"; data-integrity review of PR #368).
        logger.warning("description cache read as empty: %s on descriptions", type(error).__name__)
        return ""
    if isinstance(parsed, dict):
        description = str(parsed.get("description", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        try:
            description.encode("utf-8")
        except UnicodeEncodeError:
            # Valid JSON can still carry a lone surrogate, which no text
            # encodes; hashing it raised inside the status writer. Damage of
            # any kind reads as missing (spec 079 amendment).
            return ""
        return description
    return ""


def read_description_strictly(url: str) -> str:
    """The stored description, "" when none is stored or the entry is
    damaged, and the store's own error when it cannot be read.

    For a writer that must not mistake an unreadable store for an empty
    one: `load_cached_description` reads a failure as missing, so a fetch
    that trusted it would write over a pasted description (spec 151)."""
    raw = files().read(_description_cache_key(url))
    if raw is None:
        return ""
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            return ""
        description = str(parsed.get("description", ""))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        description.encode("utf-8")
    except (ValueError, UnicodeError):
        return ""
    return description.strip()


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
