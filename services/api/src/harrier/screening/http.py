"""HTTP fetch with retry and HTML stripping (spec 007 port)."""

from __future__ import annotations

import json
import logging
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; harrier-job-discovery/1.0)"
DEFAULT_HTTP_TIMEOUT_SECONDS = 30
DEFAULT_HTTP_RETRIES = 3


def request_text(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    retries: int = DEFAULT_HTTP_RETRIES,
) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None
    per_request_timeout = max(5, timeout_seconds)
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=per_request_timeout) as response:
                return response.read().decode("utf-8", errors="replace")
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
