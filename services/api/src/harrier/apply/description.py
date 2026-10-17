"""A job's stored description, read, written and required (spec 149).

The store is the URL-keyed description cache that discovery fills
(`harrier.screening.descriptions`). Nothing here adds a store: the operator
can see what discovery captured, and paste the text once when it captured
none, and every application operation reads that one entry.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from harrier.connection import Connection
from harrier.screening.descriptions import (
    MIN_DESCRIPTION_LENGTH_FOR_SCORING,
    enrich_url_allowed,
    load_cached_description,
    save_description_cache,
)
from harrier.screening.http import request_text, strip_html
from harrier.screening.linkedin import guest_description, is_linkedin_job_url

# A choice, not a measured need: far above any posting seen, and small
# enough that a mistaken paste of a whole file is refused (spec 149).
MAX_DESCRIPTION_BYTES = 65_536


class DescriptionError(ValueError):
    """A description the store refuses, in words that never quote it."""


class JobHasNoUrlError(DescriptionError):
    """The job has no URL, and a description is stored by URL."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"job {job_id} has no URL; a description is stored by URL")


class NoDescriptionError(ValueError):
    """An operation that needs a description found none, given or stored.

    Its message is the CLI's; the browser route words its own refusal.
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            f"no job description is stored for job {job_id}; "
            f"store one with harrier description set {job_id} --file PATH"
        )


def stored_description(url: str) -> str:
    """The job's stored description, or "" when none is stored or the entry
    is damaged (the cache already reads damage as missing)."""
    return load_cached_description(url.strip()).strip() if url.strip() else ""


def store_description(job_id: int, url: str, text: str) -> str:
    """Store `text` as the job's description, replacing any entry, and
    return what was stored. Refuses before anything is written."""
    if not url.strip():
        raise JobHasNoUrlError(job_id)
    stripped = text.strip()
    if not stripped:
        raise DescriptionError("the description is empty")
    if "\x00" in stripped:
        raise DescriptionError("the description holds a NUL character")
    try:
        encoded = stripped.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DescriptionError("the description is not text UTF-8 can encode") from error
    if len(encoded) > MAX_DESCRIPTION_BYTES:
        raise DescriptionError(
            f"the description is longer than {MAX_DESCRIPTION_BYTES} bytes in UTF-8"
        )
    save_description_cache(url.strip(), stripped)
    return stripped


def require_description(job_id: int, url: str, given: str | None = None) -> str:
    """The text to use: what the caller was given, else the stored one.

    Raises NoDescriptionError when there is neither. Tailor, cover letter
    and answers call this before generating, from the CLI and the browser
    alike (spec 149).
    """
    if given and given.strip():
        return given
    stored = stored_description(url)
    if stored:
        return stored
    raise NoDescriptionError(job_id)


# --- fetching a missing description from its posting (spec 151) ---

FETCH_TIMEOUT_SECONDS = 30
FETCHED_TEXT_LIMIT = 8_000
# A pause between two requests to one host, so a backfill never bursts at
# LinkedIn's guest endpoint. A choice, not a measured limit (spec 151).
BACKFILL_PAUSE_SECONDS = 2.0


class FetchRefused(ValueError):
    """A fetch refused before any request: the job is not one to fetch for."""


class AlreadyStoredError(FetchRefused):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"a description is already stored for job {job_id}")


class NoFetcherError(FetchRefused):
    def __init__(self, host: str) -> None:
        self.host = host
        super().__init__(f"no description fetcher for {host or 'this URL'}")


class PostingUnread(ValueError):
    """The posting page was asked and gave no description to store."""


class PageUnreadableError(PostingUnread):
    def __init__(self, error: Exception) -> None:
        # The class only: the error's text names the URL.
        super().__init__(f"the posting page could not be read ({type(error).__name__})")


class EmptyPageError(PostingUnread):
    def __init__(self) -> None:
        super().__init__("the posting page held no description")


def _ats_text(url: str) -> str:
    """An allow-listed ATS page stripped of HTML, every redirect checked
    against the same allow-list (spec 007's enrichment reader). One attempt,
    so no retry log line names the URL."""
    html = request_text(
        url, timeout_seconds=FETCH_TIMEOUT_SECONDS, retries=1, url_allowed=enrich_url_allowed
    )
    return strip_html(html)[:FETCHED_TEXT_LIMIT]


def _linkedin_text(url: str) -> str:
    return guest_description(url, timeout_seconds=FETCH_TIMEOUT_SECONDS)


def posting_reader(url: str) -> Callable[[str], str] | None:
    """The reader for this URL's page, or None when harrier has none."""
    if is_linkedin_job_url(url):
        return _linkedin_text
    if enrich_url_allowed(url):
        return _ats_text
    return None


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def fetch_description(job_id: int, url: str) -> str:
    """Fetch a job's missing description from its posting page, store it, and
    return what was stored. Every refusal that needs no request comes first."""
    url = url.strip()
    if not url:
        raise JobHasNoUrlError(job_id)
    if stored_description(url):
        raise AlreadyStoredError(job_id)
    reader = posting_reader(url)
    if reader is None:
        raise NoFetcherError(_host(url))
    try:
        text = reader(url).strip()
    except Exception as error:
        raise PageUnreadableError(error) from error
    if len(text) < MIN_DESCRIPTION_LENGTH_FOR_SCORING:
        raise EmptyPageError
    return store_description(job_id, url, text)


@dataclass
class BackfillSummary:
    """What a backfill did, counted by outcome."""

    checked: int = 0
    fetched: int = 0
    would_fetch: int = 0
    already_stored: int = 0
    no_url: int = 0
    no_fetcher: int = 0
    empty_page: int = 0
    failed: int = 0
    rejected: int = 0
    lines: list[str] = field(default_factory=list[str])

    def closing(self, dry_run: bool) -> list[str]:
        fetched = f"would fetch: {self.would_fetch}" if dry_run else f"fetched: {self.fetched}"
        return [
            f"checked: {self.checked}",
            fetched,
            f"already stored: {self.already_stored}",
            f"no URL: {self.no_url}",
            f"no fetcher for the host: {self.no_fetcher}",
            f"page held no description: {self.empty_page}",
            f"fetch failed: {self.failed}",
            f"skipped as rejected: {self.rejected}",
        ]


def backfill_descriptions(
    conn: Connection,
    *,
    dry_run: bool = False,
    limit: int = 0,
    emit: Callable[[str], None] = lambda line: None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> BackfillSummary:
    """Fetch every missing description in the account, one at a time, in job
    id order across every track (spec 151). A line names a job by id only.
    Imported here because the tracks module imports the tracker."""
    from harrier.tracker import list_jobs
    from harrier.tracks import Scope, list_tracks

    rows: list[dict[str, str]] = []
    for track in list_tracks(conn):
        rows.extend(list_jobs(conn, Scope(track=track)))
    rows.sort(key=lambda row: int(row["id"]))

    summary = BackfillSummary()
    last_request: dict[str, float] = {}
    attempts = 0
    for row in rows:
        summary.checked += 1
        job_id = int(row["id"])
        url = (row.get("url") or "").strip()
        if (row.get("status") or "").strip().lower() == "rejected":
            summary.rejected += 1
            continue
        if not url:
            summary.no_url += 1
            continue
        if stored_description(url):
            summary.already_stored += 1
            continue
        if posting_reader(url) is None:
            summary.no_fetcher += 1
            continue
        if limit > 0 and attempts >= limit:
            break
        attempts += 1
        if dry_run:
            summary.would_fetch += 1
            line = f"job {job_id}: would fetch"
            summary.lines.append(line)
            emit(line)
            continue
        host = _host(url)
        if host in last_request:
            wait = BACKFILL_PAUSE_SECONDS - (clock() - last_request[host])
            if wait > 0:
                sleep(wait)
        try:
            fetch_description(job_id, url)
        except EmptyPageError as refusal:
            summary.empty_page += 1
            line = f"job {job_id}: {refusal}"
        except (PostingUnread, DescriptionError) as refusal:
            summary.failed += 1
            line = f"job {job_id}: {refusal}"
        else:
            summary.fetched += 1
            line = f"job {job_id}: fetched"
        finally:
            last_request[host] = clock()
        summary.lines.append(line)
        emit(line)
    return summary
