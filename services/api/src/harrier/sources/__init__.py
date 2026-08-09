"""Job sources: ingestion only (product invariant; spec 008).

Every module here produces NormalizedJob values and nothing else. Filtering,
scoring, and tracker writes happen once, in harrier.screening and the
orchestrator. The import-linter contract "sources are ingestion only" makes
per-source scoring structurally impossible.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from harrier.screening.normalized import NormalizedJob

logger = logging.getLogger(__name__)


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
            message = f"{board_url}: {exc}"
            errors.append(message)
            logger.warning("%s board import failed: %s", source_name, message)
    return jobs, errors
