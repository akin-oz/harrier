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
from typing import IO
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; harrier-job-discovery/1.0)"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_HTTP_RETRIES = 3


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
