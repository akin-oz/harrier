"""Job sources: ingestion only (product invariant; spec 008).

Every module here produces NormalizedJob values and nothing else. Filtering,
scoring, and tracker writes happen once, in harrier.screening and the
orchestrator. The import-linter contract "sources are ingestion only"
enforces the module boundary; normalization-only behavior inside these
modules stays a review and test concern.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import urlparse, urlunparse

from harrier.screening.normalized import NormalizedJob

logger = logging.getLogger(__name__)


def redact_url(url: str) -> str:
    """Board URL safe for logs and summaries: scheme, host, path only.

    Userinfo and query strings are dropped (they can carry credentials or
    identity values); the host and path stay because knowing which board
    failed is the point of the error message."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    except ValueError:
        return "<unparseable url>"


def fetch_many(
    board_urls: list[str],
    fetch: Callable[[str], list[NormalizedJob]],
    source_name: str,
) -> tuple[list[NormalizedJob], list[str]]:
    """Fetch every board, isolating failures: one bad board never stops the
    rest. Returns (jobs, error descriptions) for the run summary."""
    jobs: list[NormalizedJob] = []
    errors: list[str] = []
    for board_url in board_urls:
        try:
            jobs.extend(fetch(board_url))
        except Exception as exc:
            message = f"{redact_url(board_url)}: {exc}"
            errors.append(message)
            logger.warning("%s board import failed: %s", source_name, message)
    return jobs, errors
