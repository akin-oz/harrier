"""Feed health probing and pruning (spec 025).

The asymmetry these tests exist to hold: being wrong about `dead` deletes
somebody's watchlist, being wrong about `unreachable` costs one more run. So
every row of the spec's status table gets a case, and each non-404 refusal is
asserted to be unprunable rather than merely classified.
"""

from __future__ import annotations

import io
import json
import socket
import sqlite3
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest

from harrier import feedhealth
from harrier.db import connect
from harrier.demo import OfflineFixtureError
from harrier.feedhealth import (
    CONNECTION,
    DEAD,
    DNS,
    INVALID_BODY,
    INVALID_URL,
    LIVE,
    TIMEOUT,
    UNREACHABLE,
    BoardHealth,
    FeedHealthReport,
    check_feeds,
    parses_as_board,
    probe_board,
    probe_url_for,
    prune_dead,
)
from harrier.userconfig import FEEDS, get_config, set_config
from harrier_cli.main import main

GREENHOUSE = "https://boards.greenhouse.io/acme"
ASHBY = "https://jobs.ashbyhq.com/acme"
LEVER = "https://jobs.lever.co/acme"


class _Response:
    """The subset of an HTTP response the probe reads."""

    def __init__(self, body: str, url: str) -> None:
        self._body = body.encode("utf-8")
        self.url = url

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Opener:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome

    def open(self, request: Any, timeout: int | None = None) -> _Response:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        assert isinstance(self._outcome, _Response)
        return self._outcome


def probe_with(
    outcome: object, *, url: str = GREENHOUSE, source: str = "greenhouse"
) -> BoardHealth:
    with patch.object(feedhealth, "build_opener", return_value=_Opener(outcome)):
        return probe_board(url, source)


def http_error(code: int) -> HTTPError:
    return HTTPError("https://example.invalid", code, "refused", {}, io.BytesIO(b""))  # pyright: ignore[reportArgumentType]


def board_body(source: str) -> str:
    return json.dumps([]) if source == "lever" else json.dumps({"jobs": []})


# --- the status table, one case per row ------------------------------------


@pytest.mark.parametrize(
    ("source", "url"),
    [("greenhouse", GREENHOUSE), ("ashby", ASHBY), ("lever", LEVER)],
)
def test_a_2xx_with_a_parseable_body_is_live(source: str, url: str) -> None:
    endpoint = probe_url_for(source, url)
    result = probe_with(_Response(board_body(source), endpoint), url=url, source=source)
    assert (result.verdict, result.status) == (LIVE, "200")


def test_a_board_with_no_open_roles_is_live_not_dead() -> None:
    """An empty board is a board. Reading absence of jobs as absence of the
    board would prune every quiet employer on the watchlist."""
    endpoint = probe_url_for("greenhouse", GREENHOUSE)
    result = probe_with(_Response('{"jobs": []}', endpoint))
    assert result.verdict == LIVE


def test_a_redirect_within_the_provider_is_live() -> None:
    """urllib follows it; the verdict is about where it landed."""
    result = probe_with(
        _Response('{"jobs": []}', "https://eu.boards-api.greenhouse.io/v1/boards/acme/jobs")
    )
    assert result.verdict == LIVE


def test_a_redirect_off_the_provider_is_unreachable() -> None:
    """The answer came from some other host, so it is not about this board."""
    result = probe_with(_Response('{"jobs": []}', "https://parking.example.com/acme"))
    assert result.verdict == UNREACHABLE


@pytest.mark.parametrize("code", [404, 410])
def test_a_gone_status_is_dead(code: int) -> None:
    result = probe_with(http_error(code))
    assert (result.verdict, result.status) == (DEAD, str(code))


@pytest.mark.parametrize("code", [401, 403])
def test_an_auth_refusal_is_unreachable_because_the_board_may_exist(code: int) -> None:
    result = probe_with(http_error(code))
    assert (result.verdict, result.status) == (UNREACHABLE, str(code))


@pytest.mark.parametrize("code", [408, 429, 500, 503, 520])
def test_a_transient_status_is_unreachable(code: int) -> None:
    result = probe_with(http_error(code))
    assert (result.verdict, result.status) == (UNREACHABLE, str(code))


def test_a_timeout_is_unreachable() -> None:
    result = probe_with(TimeoutError("read timed out"))
    assert (result.verdict, result.status) == (UNREACHABLE, TIMEOUT)


def test_a_timeout_wrapped_in_a_url_error_is_still_a_timeout() -> None:
    """urllib reports a socket timeout both ways depending on where it fires,
    and a report that said `connection` for one and `timeout` for the other
    would make the two indistinguishable causes look like different ones."""
    result = probe_with(URLError(TimeoutError("handshake timed out")))
    assert result.status == TIMEOUT


def test_a_dns_failure_is_unreachable_and_says_so() -> None:
    """The shape a renamed host takes. Distinct from a refusal because the
    operator acts on it differently."""
    result = probe_with(URLError(socket.gaierror(-2, "Name or service not known")))
    assert (result.verdict, result.status) == (UNREACHABLE, DNS)


def test_a_connection_failure_is_unreachable() -> None:
    result = probe_with(URLError(ConnectionRefusedError(61, "Connection refused")))
    assert (result.verdict, result.status) == (UNREACHABLE, CONNECTION)


def test_a_2xx_that_is_not_a_board_is_unreachable_not_dead() -> None:
    """A provider error page or a captive portal answers 200. Pruning on it
    would delete a live entry on the strength of somebody else's HTML."""
    endpoint = probe_url_for("greenhouse", GREENHOUSE)
    result = probe_with(_Response("<html>maintenance</html>", endpoint))
    assert (result.verdict, result.status) == (UNREACHABLE, INVALID_BODY)


def test_a_2xx_of_valid_json_that_is_not_a_board_is_unreachable() -> None:
    endpoint = probe_url_for("greenhouse", GREENHOUSE)
    result = probe_with(_Response('{"error": "not found"}', endpoint))
    assert (result.verdict, result.status) == (UNREACHABLE, INVALID_BODY)


def test_an_entry_naming_no_board_is_unreachable_and_never_prunable() -> None:
    """A provider host with no board in the path is a typo, not a network
    state. The spec's token list does not cover it, so it gets its own rather
    than being folded into a token that would misdescribe it."""
    result = probe_board("https://boards.greenhouse.io/", "greenhouse")
    assert (result.verdict, result.status) == (UNREACHABLE, INVALID_URL)
    assert not result.prunable


# --- nothing but 404 and 410 may ever be pruned ----------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        http_error(403),
        http_error(429),
        http_error(500),
        TimeoutError("read timed out"),
        URLError(socket.gaierror(-2, "Name or service not known")),
        _Response("<html>maintenance</html>", probe_url_for("greenhouse", GREENHOUSE)),
    ],
)
def test_no_refusal_other_than_gone_is_prunable(outcome: object) -> None:
    """The one invariant that cannot be got wrong: an outage must never
    delete a watchlist."""
    assert not probe_with(outcome).prunable


# --- body parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "body", "expected"),
    [
        ("greenhouse", '{"jobs": []}', True),
        ("greenhouse", '{"postings": []}', False),
        ("ashby", '{"jobs": []}', True),
        ("ashby", '{"jobPostings": []}', True),
        ("ashby", '{"postings": []}', True),
        ("lever", "[]", True),
        ("lever", '{"jobs": []}', False),
        ("greenhouse", "not json", False),
        ("greenhouse", '{"jobs": "some"}', False),
    ],
)
def test_parses_as_board(source: str, body: str, expected: bool) -> None:
    assert parses_as_board(source, body) is expected


def test_the_probe_endpoint_is_the_one_discovery_fetches() -> None:
    """If the probe and the fetch drift apart, `live` stops meaning that
    discovery will work, which is the only thing the report is for."""
    assert probe_url_for("greenhouse", GREENHOUSE).startswith(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    )
    assert probe_url_for("ashby", ASHBY).startswith(
        "https://api.ashbyhq.com/posting-api/job-board/acme"
    )
    assert probe_url_for("lever", LEVER).startswith("https://api.lever.co/v0/postings/acme")


def test_a_lever_probe_asks_for_one_posting() -> None:
    """A probe, not a fetch: existence is the question."""
    assert "limit=1" in probe_url_for("lever", LEVER)


def test_a_lever_eu_board_probes_the_eu_api() -> None:
    assert probe_url_for("lever", "https://jobs.eu.lever.co/acme").startswith(
        "https://api.eu.lever.co/"
    )


# --- concurrency -----------------------------------------------------------


def test_at_most_eight_probes_run_concurrently() -> None:
    lock = threading.Lock()
    live = 0
    peak = 0

    def probe(url: str, source: str) -> BoardHealth:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        # Long enough that later submissions pile up behind the cap.
        threading.Event().wait(0.02)
        with lock:
            live -= 1
        return BoardHealth(url, source, LIVE, "200")

    feeds = {"greenhouse": [f"https://boards.greenhouse.io/c{index}" for index in range(30)]}
    report = check_feeds(feeds, probe=probe)
    assert len(report.results) == 30
    assert peak <= feedhealth.MAX_IN_FLIGHT


def test_one_hung_host_does_not_prevent_the_others_being_classified() -> None:
    """The failure this bound exists for. The hung board is submitted first,
    so an implementation that waited on results in order before submitting
    the rest would deadlock here rather than merely be slow."""
    released = threading.Event()
    finished = threading.Barrier(3, timeout=5)

    def probe(url: str, source: str) -> BoardHealth:
        if url.endswith("hung"):
            # Waits until the other two have finished their work.
            assert released.wait(timeout=5)
            return BoardHealth(url, source, UNREACHABLE, TIMEOUT)
        finished.wait()
        return BoardHealth(url, source, LIVE, "200")

    feeds = {
        "greenhouse": [
            "https://boards.greenhouse.io/hung",
            "https://boards.greenhouse.io/one",
            "https://boards.greenhouse.io/two",
        ]
    }

    def release_when_others_are_done() -> None:
        finished.wait()
        released.set()

    waiter = threading.Thread(target=release_when_others_are_done)
    waiter.start()
    report = check_feeds(feeds, probe=probe)
    waiter.join(timeout=5)

    by_url = {item.url: item.verdict for item in report.results}
    assert by_url["https://boards.greenhouse.io/one"] == LIVE
    assert by_url["https://boards.greenhouse.io/two"] == LIVE
    assert by_url["https://boards.greenhouse.io/hung"] == UNREACHABLE


def test_an_empty_watchlist_probes_nothing() -> None:
    assert check_feeds({"greenhouse": [], "ashby": [], "lever": []}).results == ()


def test_the_report_is_ordered_the_same_way_twice() -> None:
    """Two runs over an unchanged watchlist print the same thing, so a diff
    between them is about the boards rather than about thread scheduling."""
    feeds = {
        "lever": ["https://jobs.lever.co/b", "https://jobs.lever.co/a"],
        "greenhouse": ["https://boards.greenhouse.io/z"],
    }

    def probe(url: str, source: str) -> BoardHealth:
        return BoardHealth(url, source, LIVE, "200")

    first = [item.url for item in check_feeds(feeds, probe=probe).results]
    second = [item.url for item in check_feeds(feeds, probe=probe).results]
    assert first == second
    assert first == [
        "https://boards.greenhouse.io/z",
        "https://jobs.lever.co/b",
        "https://jobs.lever.co/a",
    ]


# --- pruning ---------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return connect()


def report_of(*rows: tuple[str, str]) -> FeedHealthReport:
    return FeedHealthReport(
        tuple(BoardHealth(url, "greenhouse", verdict, "200") for url, verdict in rows)
    )


def test_prune_removes_only_the_dead_entries(conn: sqlite3.Connection) -> None:
    report = report_of(
        ("https://boards.greenhouse.io/live", LIVE),
        ("https://boards.greenhouse.io/gone", DEAD),
        ("https://boards.greenhouse.io/down", UNREACHABLE),
    )
    removed = prune_dead(conn, report)

    assert [item.url for item in removed] == ["https://boards.greenhouse.io/gone"]
    assert get_config(conn, FEEDS) == [
        "https://boards.greenhouse.io/live",
        "https://boards.greenhouse.io/down",
    ]


def test_prune_reports_every_entry_it_removed(conn: sqlite3.Connection) -> None:
    report = report_of(
        ("https://boards.greenhouse.io/gone", DEAD),
        ("https://boards.greenhouse.io/also-gone", DEAD),
    )
    assert len(prune_dead(conn, report)) == 2


def test_prune_writes_nothing_when_nothing_is_dead(conn: sqlite3.Connection) -> None:
    """Not an optimisation. The watchlist may still be coming from
    config/feeds.txt, and storing a copy that removed nothing would shadow
    the file from then on, freezing a configuration still edited by hand."""
    prune_dead(conn, report_of(("https://boards.greenhouse.io/live", LIVE)))
    assert get_config(conn, FEEDS) is None


def test_prune_does_not_resurrect_an_entry_that_was_never_probed(
    conn: sqlite3.Connection,
) -> None:
    """The pruned list is built from this report, so a board added to the
    store after the probe began is not written back by the prune."""
    set_config(conn, FEEDS, ["https://boards.greenhouse.io/added-later"])
    prune_dead(conn, report_of(("https://boards.greenhouse.io/gone", DEAD)))
    assert get_config(conn, FEEDS) == []


def test_counts_cover_every_verdict() -> None:
    report = report_of(
        ("https://boards.greenhouse.io/a", LIVE),
        ("https://boards.greenhouse.io/b", DEAD),
        ("https://boards.greenhouse.io/c", UNREACHABLE),
        ("https://boards.greenhouse.io/d", LIVE),
    )
    assert report.counts() == {LIVE: 2, DEAD: 1, UNREACHABLE: 1}


def test_the_report_names_only_boards_the_operator_configured() -> None:
    """The report is a projection of the watchlist, so it cannot introduce a
    board the operator does not already have (ADR-009)."""
    feeds = {"greenhouse": ["https://boards.greenhouse.io/only"]}

    def probe(url: str, source: str) -> BoardHealth:
        return BoardHealth(url, source, LIVE, "200")

    report = check_feeds(feeds, probe=probe)
    assert {item.url for item in report.results} == {"https://boards.greenhouse.io/only"}


# --- demo mode --------------------------------------------------------------


def test_demo_mode_probes_the_fixtures_and_never_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo promises it reaches no network. A probe that opened a socket
    would make check-feeds the one command that quietly dials out, and the
    promise is only as good as its least-checked command (spec 021)."""
    fixtures = tmp_path / "http"
    fixtures.mkdir()
    endpoint = probe_url_for("greenhouse", GREENHOUSE)
    (fixtures / "board.json").write_text('{"jobs": []}', encoding="utf-8")
    (fixtures / "index.json").write_text(json.dumps({endpoint: "board.json"}), encoding="utf-8")
    monkeypatch.setenv("HARRIER_HTTP_FIXTURES", str(fixtures))

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("demo mode opened a network connection")

    with patch.object(feedhealth, "build_opener", refuse):
        result = probe_board(GREENHOUSE, "greenhouse")
    assert (result.verdict, result.status) == (LIVE, "200")


def test_demo_mode_refuses_a_url_the_fixtures_do_not_cover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falling through to the network for an uncovered URL is the exact way
    an offline guarantee decays into a usually-offline one."""
    fixtures = tmp_path / "http"
    fixtures.mkdir()
    (fixtures / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HARRIER_HTTP_FIXTURES", str(fixtures))
    with pytest.raises(OfflineFixtureError):
        probe_board(GREENHOUSE, "greenhouse")


# --- the command ------------------------------------------------------------


def run_cli(argv: list[str], results: tuple[BoardHealth, ...]) -> int:
    """Run the command with the network replaced by a fixed report."""
    with patch.object(feedhealth, "check_feeds", return_value=FeedHealthReport(results)):
        return main(argv)


def test_the_command_prints_a_row_per_board(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_config(conn, FEEDS, [GREENHOUSE, ASHBY])
    code = run_cli(
        ["config", "check-feeds"],
        (
            BoardHealth(GREENHOUSE, "greenhouse", LIVE, "200"),
            BoardHealth(ASHBY, "ashby", DEAD, "404"),
        ),
    )
    out = capsys.readouterr().out
    assert code == 0
    assert GREENHOUSE in out
    assert "404" in out
    assert "1 live, 1 dead, 0 unreachable" in out
    assert "re-run with --prune" in out


def test_the_command_reports_an_empty_watchlist_rather_than_succeeding_silently(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run that probed nothing must not look like a run that found
    everything healthy."""
    assert run_cli(["config", "check-feeds"], ()) == 1
    assert "no boards configured" in capsys.readouterr().err


def test_prune_always_probes_in_the_same_invocation(conn: sqlite3.Connection) -> None:
    """Spec 025: --prune acts only on a probe performed in this invocation,
    so there is no stored report that could go stale. The command has no path
    that prunes without calling check_feeds first."""
    set_config(conn, FEEDS, [GREENHOUSE, ASHBY])
    report = FeedHealthReport(
        (
            BoardHealth(GREENHOUSE, "greenhouse", LIVE, "200"),
            BoardHealth(ASHBY, "ashby", DEAD, "404"),
        )
    )
    with patch.object(feedhealth, "check_feeds", return_value=report) as probe:
        assert main(["config", "check-feeds", "--prune"]) == 0
    probe.assert_called_once()
    assert get_config(connect(), FEEDS) == [GREENHOUSE]


def test_prune_says_so_when_nothing_was_dead(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_config(conn, FEEDS, [GREENHOUSE])
    code = run_cli(
        ["config", "check-feeds", "--prune"],
        (BoardHealth(GREENHOUSE, "greenhouse", UNREACHABLE, "503"),),
    )
    assert code == 0
    assert "nothing pruned" in capsys.readouterr().out
    assert get_config(connect(), FEEDS) == [GREENHOUSE]


def test_prune_names_every_board_it_removed(
    conn: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_config(conn, FEEDS, [GREENHOUSE, ASHBY])
    run_cli(
        ["config", "check-feeds", "--prune"],
        (
            BoardHealth(GREENHOUSE, "greenhouse", DEAD, "404"),
            BoardHealth(ASHBY, "ashby", DEAD, "410"),
        ),
    )
    out = capsys.readouterr().out
    assert f"removed {GREENHOUSE} (404)" in out
    assert f"removed {ASHBY} (410)" in out
