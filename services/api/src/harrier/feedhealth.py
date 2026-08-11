"""Which configured boards are still there (spec 025).

A watchlist accumulates dead boards: a company closes its board, moves off a
provider, or renames it, and the entry stays in the configuration answering
404 forever. Every run pays for those entries and prints failures that make a
working system look broken.

This probes each configured board once and classifies it, so the operator can
see the dead ones and decide. Two properties are load-bearing:

- **Only `dead` is prunable.** A 403, a 500, a timeout and an unparseable
  body all classify as `unreachable`, because an outage must never delete
  somebody's watchlist. The asymmetry is the whole point: being wrong about
  `dead` loses configuration, being wrong about `unreachable` costs one more
  run.
- **The probe is a probe, not a fetch.** One request per board with no retry,
  a budget covering the whole thing, and a page size of one where the
  provider takes one. `harrier discover` is what fetches.

What is probed is the API endpoint discovery actually uses, not the board's
web page. A board whose marketing page loads while its API is gone is dead
for our purposes, and the reverse would report health the pipeline does not
have.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from harrier.demo import http_fixtures_dir
from harrier.screening.http import USER_AGENT, fixture_body
from harrier.sources.ashby import extract_ashby_board
from harrier.sources.greenhouse import extract_greenhouse_token
from harrier.sources.lever import extract_lever_api_base, extract_lever_company
from harrier.userconfig import DEFAULT_SCOPE, FEEDS, load_ats_feeds, set_config

# One hung host must not stall the report, and eight in flight is enough to
# make a watchlist of any realistic size finish while staying polite.
MAX_IN_FLIGHT = 8

# Covers the whole probe including any redirects, not each hop: a chain of
# slow hops is exactly the case a per-hop timeout fails to bound.
PROBE_TIMEOUT_SECONDS = 15

# The sixth redirect reports unreachable rather than dead, because a redirect
# loop says nothing about whether the board exists.
MAX_REDIRECTS = 5

LIVE = "live"
DEAD = "dead"
UNREACHABLE = "unreachable"

# Only these two mean the board is gone. Every other refusal is the server
# declining to answer, which is not the same claim.
DEAD_STATUSES = frozenset({404, 410})

# Stable tokens for failures that never produced an HTTP status, so the
# output is assertable rather than implementation-defined.
TIMEOUT = "timeout"
DNS = "dns"
CONNECTION = "connection"
INVALID_BODY = "invalid-body"
INVALID_URL = "invalid-url"


class FeedHealthError(RuntimeError):
    """Raised when a request cannot be honoured, never when a board is dead."""


@dataclass(frozen=True)
class BoardHealth:
    """One configured entry and what probing it showed."""

    url: str
    source: str
    verdict: str
    status: str

    @property
    def prunable(self) -> bool:
        return self.verdict == DEAD


@dataclass(frozen=True)
class FeedHealthReport:
    """The result of one probe run.

    Pruning takes this object rather than a list of URLs, so pruning without
    probing in the same invocation is not expressible. Spec 025 asks for that
    to be refused; making it unrepresentable is the same guarantee without a
    check that could be forgotten.
    """

    results: tuple[BoardHealth, ...]

    @property
    def dead(self) -> tuple[BoardHealth, ...]:
        return tuple(item for item in self.results if item.prunable)

    def counts(self) -> dict[str, int]:
        counts = {LIVE: 0, DEAD: 0, UNREACHABLE: 0}
        for item in self.results:
            counts[item.verdict] += 1
        return counts


class _CappedRedirectHandler(HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS


def probe_url_for(source: str, board_url: str) -> str:
    """The endpoint discovery would fetch, or "" when the entry names none.

    Kept next to the classification rather than inside each source module so
    that the probe and the fetch are visibly the same endpoint: if they drift,
    a report saying `live` stops meaning discovery will work.
    """
    if source == "greenhouse":
        token = extract_greenhouse_token(board_url)
        if not token:
            return ""
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    if source == "ashby":
        board = extract_ashby_board(board_url)
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
            if board
            else ""
        )
    if source == "lever":
        company = extract_lever_company(board_url)
        # limit=1 keeps this a probe: existence is the question, not content.
        return (
            f"{extract_lever_api_base(board_url)}/v0/postings/{company}?mode=json&skip=0&limit=1"
            if company
            else ""
        )
    return ""


def parses_as_board(source: str, body: str) -> bool:
    """Whether a 2xx body is this provider's board document.

    A 200 carrying something else is a provider error page or a captive
    portal, and reporting that as `dead` would delete a live entry. It
    classifies as unreachable instead.

    An empty board is live: a company with no open roles still has a board,
    and treating `{"jobs": []}` as absence would prune every quiet employer.
    """
    try:
        payload: object = json.loads(body)
    except json.JSONDecodeError:
        return False
    if source == "lever":
        return isinstance(payload, list)
    if not isinstance(payload, dict):
        return False
    typed = cast("dict[str, Any]", payload)
    keys = ("jobs",) if source == "greenhouse" else ("jobs", "jobPostings", "postings")
    return any(isinstance(typed.get(key), list) for key in keys)


def _same_provider(requested: str, final: str) -> bool:
    """Whether a followed redirect stayed on the provider we asked about.

    A redirect within the provider is the provider reorganising itself. One
    that leaves it means the answer is about some other host, and no verdict
    about this board can be read from it.
    """
    requested_host = (urlparse(requested).hostname or "").lower()
    final_host = (urlparse(final).hostname or "").lower()
    if requested_host == final_host:
        return True
    root = ".".join(requested_host.split(".")[-2:])
    return bool(root) and (final_host == root or final_host.endswith(f".{root}"))


def _classify_response(source: str, requested: str, final_url: str, body: str) -> tuple[str, str]:
    if not _same_provider(requested, final_url):
        return UNREACHABLE, "200"
    if not parses_as_board(source, body):
        return UNREACHABLE, INVALID_BODY
    return LIVE, "200"


def probe_board(
    board_url: str, source: str, *, timeout: int = PROBE_TIMEOUT_SECONDS
) -> BoardHealth:
    """One board, one request, no retry."""
    endpoint = probe_url_for(source, board_url)
    if not endpoint:
        # The host routed to a provider but the path names no board. Not a
        # network state and not prunable: it is a typo in the configuration,
        # and saying so is more use than any verdict about reachability.
        return BoardHealth(board_url, source, UNREACHABLE, INVALID_URL)

    if http_fixtures_dir() is not None:
        # Demo mode promises no network. Going through the fixture layer
        # keeps that true here rather than making check-feeds the one command
        # that quietly dials out (spec 021).
        body = fixture_body(endpoint)
        verdict, status = _classify_response(source, endpoint, endpoint, body or "")
        return BoardHealth(board_url, source, verdict, status)

    opener = build_opener(_CappedRedirectHandler())
    request = Request(endpoint, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            verdict, status = _classify_response(source, endpoint, response.url, body)
            return BoardHealth(board_url, source, verdict, status)
    except HTTPError as error:
        verdict = DEAD if error.code in DEAD_STATUSES else UNREACHABLE
        return BoardHealth(board_url, source, verdict, str(error.code))
    except TimeoutError:
        return BoardHealth(board_url, source, UNREACHABLE, TIMEOUT)
    except URLError as error:
        # A DNS failure is worth distinguishing: it is the shape a renamed
        # host takes, and the operator reads it differently from a refusal.
        reason = error.reason
        if isinstance(reason, TimeoutError | socket.timeout):
            return BoardHealth(board_url, source, UNREACHABLE, TIMEOUT)
        status = DNS if isinstance(reason, socket.gaierror) else CONNECTION
        return BoardHealth(board_url, source, UNREACHABLE, status)
    except OSError:
        return BoardHealth(board_url, source, UNREACHABLE, CONNECTION)


def check_feeds(
    feeds: dict[str, list[str]],
    *,
    probe: Callable[[str, str], BoardHealth] | None = None,
    max_in_flight: int = MAX_IN_FLIGHT,
) -> FeedHealthReport:
    """Probe every configured board, at most `max_in_flight` at a time.

    Ordered by source then by the operator's own ordering within it, so two
    runs over an unchanged watchlist print the same thing and a diff between
    them is about the boards.
    """
    run_probe = probe if probe is not None else probe_board
    entries: list[tuple[str, str]] = [
        (url, source) for source in sorted(feeds) for url in feeds[source]
    ]
    if not entries:
        return FeedHealthReport(())
    workers = max(1, min(max_in_flight, len(entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Submitted together, read in submission order: every board is in
        # flight (up to the cap) before any result is waited on, so a hung
        # host delays its own row and nothing else.
        futures = [pool.submit(run_probe, url, source) for url, source in entries]
        results = [future.result() for future in futures]
    return FeedHealthReport(tuple(results))


def prune_dead(
    conn: sqlite3.Connection,
    report: FeedHealthReport,
    *,
    scope: str = DEFAULT_SCOPE,
) -> tuple[BoardHealth, ...]:
    """Remove the boards this report probed as dead. Returns what went.

    Writes nothing when nothing is dead. That is not an optimisation: the
    watchlist may still be coming from `config/feeds.txt`, and storing a
    "pruned" copy that removed nothing would shadow the file from then on,
    silently freezing a configuration the operator still edits by hand
    (spec 023 reads the store first and the file second).
    """
    dead = report.dead
    if not dead:
        return ()
    removed = {item.url for item in dead}
    remaining = [item.url for item in report.results if item.url not in removed]
    set_config(conn, FEEDS, remaining, scope=scope)
    return dead


def load_feeds_for_check(
    conn: sqlite3.Connection | None = None, *, scope: str = DEFAULT_SCOPE
) -> dict[str, list[str]]:
    """The watchlist as discovery sees it, grouped by provider."""
    return load_ats_feeds(conn, scope=scope)
