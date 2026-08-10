"""HTTP fetch with retry and HTML stripping (spec 007 port).

The optional url_allowed hook validates the initial URL and every redirect
target before a request is made. Job URLs come from external feeds, so a
crafted posting URL must not be able to point a fetch at internal endpoints
(PR #4 review finding). Proof: tests/test_screening.py
(test_request_text_refuses_disallowed_initial_url,
test_redirect_to_disallowed_host_is_blocked,
test_enrich_url_allowed_blocks_spoofed_hosts).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from email.message import Message
from http.client import HTTPResponse
from pathlib import Path
from typing import IO, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from harrier.demo import FIXTURE_INDEX_NAME, OfflineFixtureError, http_fixtures_dir

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; harrier-job-discovery/1.0)"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_HTTP_RETRIES = 3


def _offline_body(url: str) -> str | None:
    """The fixture body for this URL, or None when fixtures are off.

    An uncovered URL raises instead of returning None: falling through to a
    real request would make demo mode silently network-dependent, which is
    the one property the demo promises (spec 021).
    """
    directory = http_fixtures_dir()
    if directory is None:
        return None
    index_path = directory / FIXTURE_INDEX_NAME
    if not index_path.is_file():
        raise OfflineFixtureError(f"offline HTTP fixtures are enabled but {index_path} is missing")
    try:
        parsed: object = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # One exception type for every fixture configuration failure, so a
        # caller never has to catch two (review finding on PR #18).
        raise OfflineFixtureError(f"{index_path} could not be read as JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise OfflineFixtureError(f"{index_path} must be a JSON object of url -> filename")
    index = cast("dict[str, object]", parsed)
    entry: object = index.get(url)
    if not isinstance(entry, str) or not entry:
        raise OfflineFixtureError(f"no offline HTTP fixture for {url}; add one to {index_path}")
    # A plain filename only: the index is data, and a traversing entry
    # would let it read outside the fixture directory.
    if entry != Path(entry).name:
        raise OfflineFixtureError(
            f"fixture entry for {url} must be a plain filename, got {entry!r}"
        )
    body_path = directory / entry
    if not body_path.is_file():
        raise OfflineFixtureError(f"fixture {body_path} named by {index_path} does not exist")
    return body_path.read_text(encoding="utf-8")


class DisallowedUrlError(RuntimeError):
    pass


class ValidatingRedirectHandler(HTTPRedirectHandler):
    def __init__(self, url_allowed: Callable[[str], bool]) -> None:
        super().__init__()
        self._url_allowed = url_allowed

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes] | HTTPResponse,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> Request | None:
        if not self._url_allowed(newurl):
            raise DisallowedUrlError(f"redirect to disallowed URL blocked: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # pyright: ignore[reportArgumentType]


def request_text(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    retries: int = DEFAULT_HTTP_RETRIES,
    url_allowed: Callable[[str], bool] | None = None,
) -> str:
    if url_allowed is not None and not url_allowed(url):
        raise DisallowedUrlError(f"request to disallowed URL blocked: {url}")
    # After the allowlist, so demo mode cannot widen what a URL may reach.
    offline = _offline_body(url)
    if offline is not None:
        return offline
    request = Request(url, headers={"User-Agent": USER_AGENT})
    opener_open = (
        build_opener(ValidatingRedirectHandler(url_allowed)).open
        if url_allowed is not None
        else urlopen
    )
    last_error: Exception | None = None
    per_request_timeout = max(5, timeout_seconds)
    for attempt in range(1, retries + 1):
        try:
            with opener_open(request, timeout=per_request_timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except DisallowedUrlError:
            raise
        except (TimeoutError, URLError, HTTPError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            logger.warning(
                "HTTP request retry %d/%d for %s after error: %s", attempt, retries - 1, url, exc
            )
            time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"HTTP request failed after {retries} attempts for {url}: {last_error}")


def request_json(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> object:
    return json.loads(request_text(url, timeout_seconds=timeout_seconds, retries=retries))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()
