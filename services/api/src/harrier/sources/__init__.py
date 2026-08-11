"""Job sources: ingestion only (product invariant; spec 008).

Every module here produces NormalizedJob values and nothing else. Filtering,
scoring, and tracker writes happen once, in harrier.screening and the
orchestrator. The import-linter contract "sources are ingestion only"
enforces the module boundary; normalization-only behavior inside these
modules stays a review and test concern.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from urllib.parse import urlparse, urlunparse

from harrier.screening.normalized import NormalizedJob

logger = logging.getLogger(__name__)


# Query parameters and path segments whose value is a credential. A new
# exception type is the way these escape: the retry loops catch the errors
# they expect, and anything else carries its message, with the URL in it,
# out to a summary file, stdout, and the unauthenticated event stream
# (spec 035).
_SECRET_PARAM = re.compile(
    r"(?i)\b(token|api[_-]?key|apikey|access[_-]?token|secret|password|auth)=([^&\s\"']+)"
)
_SECRET_PATH = re.compile(r"(?i)/bot(\d+:[A-Za-z0-9_-]+)")


def scrub_secrets(text: str) -> str:
    """Remove credential-shaped values from arbitrary text.

    Applied at the boundary rather than at each call site, because the call
    sites that leak are the ones nobody thought of: `http.client.InvalidURL`,
    raised by a token pasted with a stray character, is not caught by a retry
    loop expecting TimeoutError, URLError and HTTPError, and its message
    embeds the request path.
    """
    scrubbed = _SECRET_PARAM.sub(lambda match: f"{match.group(1)}=REDACTED", text)
    return _SECRET_PATH.sub("/botREDACTED", scrubbed)


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
            # redact_url covers the board URL; the exception text is a
            # separate channel and can carry a provider URL of its own
            # (review finding on PR #39).
            message = scrub_secrets(f"{redact_url(board_url)}: {exc}")
            errors.append(message)
            logger.warning("%s board import failed: %s", source_name, message)
    return jobs, errors
