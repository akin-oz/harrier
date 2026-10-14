"""The deploy's checks that need no store (spec 110).

`harrier deploy check-auth-settings`, `check-machine-count`,
`check-scheduler-tick`, `fill-fly-config` and `project-cost`. Every decision
is a function of data the test supplies: a settings object a local server
answers with, a machine list, an event list and a fake clock, a few typed
figures. Nothing reaches Supabase, Fly or any network but this machine's
loopback. Every key and URL here is synthetic.
"""

from __future__ import annotations

import base64
import http.server
import json
import socket
import threading
import tomllib
import urllib.request
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest

import harrier_cli.main as cli
from harrier import deploychecks
from harrier.demo import repo_root
from harrier.deploychecks import (
    CostLine,
    DeployCheckFailed,
    check_auth_settings,
    check_machine_count,
    fill_fly_config,
    project_cost,
    ticked_after,
    wait_for_tick,
)
from harrier_cli.main import main

# A marker no real key could equal, and no output may hold.
SYNTHETIC_KEY = "not-a-real-key-not-a-real-key"

INVITE_ONLY_SETTINGS: dict[str, object] = {
    "disable_signup": True,
    "external": {"email": True, "phone": False, "anonymous_users": False, "google": False},
}


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the Auth settings, against a server on this machine's loopback ---


class _Settings(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b"{}"
    seen: ClassVar[list[tuple[str, str | None]]] = []

    def do_GET(self) -> None:
        type(self).seen.append((self.path, self.headers.get("apikey")))
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def settings_server(
    status: int, body: object
) -> Generator[tuple[str, list[tuple[str, str | None]]]]:
    """A server answering `GET` with `status` and `body` (bytes are sent as
    they are, anything else as JSON). Yields its base URL and the requests it
    has seen, each the path and the `apikey` header."""
    handler = type("Handler", (_Settings,), {})
    handler.status = status
    handler.body = body if isinstance(body, bytes) else json.dumps(body).encode()
    handler.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", handler.seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def set_project(monkeypatch: pytest.MonkeyPatch, url: str, key: str = SYNTHETIC_KEY) -> None:
    monkeypatch.setenv("HARRIER_SUPABASE_URL", url)
    monkeypatch.setenv("HARRIER_SUPABASE_PUBLISHABLE_KEY", key)


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (INVITE_ONLY_SETTINGS, None),
        # email and phone may be on: invite only still holds.
        ({"disable_signup": True, "external": {"email": True, "phone": True}}, None),
        ({"disable_signup": True}, "external is missing"),
        ({"disable_signup": True, "external": "garbage"}, "external is not an object"),
        ({"disable_signup": True, "external": None}, "external is missing"),
        (
            {"disable_signup": True, "external": {"google": "true"}},
            "external.google is not a boolean",
        ),
        (
            {"disable_signup": True, "external": {"anonymous_users": 1}},
            "external.anonymous_users is not a boolean",
        ),
        ({"disable_signup": False}, "disable_signup is false"),
        ({"external": {}}, "disable_signup is missing"),
        ({"disable_signup": True, "external": {}}, None),
        ({"disable_signup": "true"}, "disable_signup is not a boolean"),
        (
            {"disable_signup": True, "external": {"anonymous_users": True}},
            "external.anonymous_users is true",
        ),
        (
            {"disable_signup": True, "external": {"email": True, "google": True}},
            "external.google is true",
        ),
        (
            {"disable_signup": True, "external": {"github": True, "apple": True}},
            "external.apple is true",
        ),
        # The first that fails, in the spec's order: sign-up before anonymous
        # before a provider.
        (
            {
                "disable_signup": False,
                "external": {"anonymous_users": True, "google": True},
            },
            "disable_signup is false",
        ),
        (
            {"disable_signup": True, "external": {"anonymous_users": True, "google": True}},
            "external.anonymous_users is true",
        ),
    ],
)
def test_the_deploy_refuses_open_sign_up(
    settings: dict[str, object],
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Against a fake settings response, through the CLI: the exit status and
    the line it prints, and the key it sent as `apikey`, never printed."""
    with settings_server(200, settings) as (url, seen):
        set_project(monkeypatch, url)
        code, out, err = run(["deploy", "check-auth-settings"], capsys)
        sent = list(seen)
    if expected is None:
        assert (code, out, err) == (0, "auth settings: invite only\n", "")
    else:
        assert (code, out, err) == (1, "", f"error: auth settings: {expected}\n")
    assert sent == [("/auth/v1/settings", SYNTHETIC_KEY)]
    assert SYNTHETIC_KEY not in out + err


def test_the_decision_is_a_function_of_the_parsed_settings() -> None:
    """No network: `check_auth_settings` with a fake fetch answers from the
    parsed object alone."""

    def fake(_url: str, _key: str) -> tuple[int, bytes]:
        return 200, json.dumps(INVITE_ONLY_SETTINGS).encode()

    environ = {
        "HARRIER_SUPABASE_URL": "https://project.example.invalid",
        "HARRIER_SUPABASE_PUBLISHABLE_KEY": SYNTHETIC_KEY,
    }
    assert check_auth_settings(environ, fake) == "auth settings: invite only"


@pytest.mark.parametrize(
    ("setup", "status", "body", "expected"),
    [
        ("no url", 200, {}, "harrier deploy check-auth-settings needs HARRIER_SUPABASE_URL"),
        (
            "no key",
            200,
            {},
            "harrier deploy check-auth-settings needs HARRIER_SUPABASE_PUBLISHABLE_KEY",
        ),
        ("blank key", 200, {}, "needs HARRIER_SUPABASE_PUBLISHABLE_KEY"),
        ("served", 401, {}, "auth settings: the project answered 401"),
        ("served", 500, {}, "auth settings: the project answered 500"),
        ("served", 200, [], "auth settings: the answer is not a JSON object"),
        ("served", 200, b"<html>", "auth settings: the answer is not a JSON object"),
        ("served", 200, b"", "auth settings: the answer is not a JSON object"),
    ],
)
def test_check_auth_settings_refuses_what_it_cannot_read(
    setup: str,
    status: int,
    body: object,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("HARRIER_SUPABASE_URL", "HARRIER_SUPABASE_PUBLISHABLE_KEY"):
        monkeypatch.delenv(name, raising=False)
    with settings_server(status, body) as (url, _seen):
        if setup == "no url":
            monkeypatch.setenv("HARRIER_SUPABASE_PUBLISHABLE_KEY", SYNTHETIC_KEY)
        elif setup == "no key":
            monkeypatch.setenv("HARRIER_SUPABASE_URL", url)
        elif setup == "blank key":
            set_project(monkeypatch, url, key="   ")
        else:
            set_project(monkeypatch, url)
        code, out, err = run(["deploy", "check-auth-settings"], capsys)
    assert code == 1
    assert out == ""
    assert err.startswith("error: ") and expected in err
    # Never the key, the URL or the response body.
    assert SYNTHETIC_KEY not in out + err
    assert "http://127.0.0.1" not in out + err
    assert "<html>" not in out + err


def test_check_auth_settings_names_no_answer_and_a_plain_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A port nothing listens on: bound, then closed.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    set_project(monkeypatch, f"http://127.0.0.1:{closed}")
    code, out, err = run(["deploy", "check-auth-settings"], capsys)
    assert (code, out) == (1, "")
    assert err.startswith("error: auth settings: no answer (")
    assert SYNTHETIC_KEY not in err

    # The key travels in a header: not over http to a host that is not this
    # machine, and nothing is sent.
    sent: list[object] = []

    def record(*args: object, **_kwargs: object) -> None:
        sent.append(args)

    monkeypatch.setattr(urllib.request, "urlopen", record)
    set_project(monkeypatch, "http://project.example.invalid")
    code, out, err = run(["deploy", "check-auth-settings"], capsys)
    assert code == 1
    assert "needs HARRIER_SUPABASE_URL to be an https:// URL" in err
    assert sent == []


# --- the API app's machines ---


def machine(state: str = "started", group: str | None = "app") -> dict[str, object]:
    config: dict[str, object] = {}
    if group is not None:
        config["metadata"] = {"fly_process_group": group}
    return {"id": "m" + state, "state": state, "config": config}


@pytest.mark.parametrize(
    ("machines", "expected"),
    [
        ([machine("started")], "api machines: 1"),
        # Stopped counts: the API stops when idle (hosting budget).
        ([machine("stopped")], "api machines: 1"),
        # A machine whose config names no group is the default group's.
        ([machine("started", group=None)], "api machines: 1"),
        # Another group's machine is not the API's.
        ([machine("started"), machine("started", group="worker")], "api machines: 1"),
        # A destroyed machine is gone.
        ([machine("started"), machine("destroyed")], "api machines: 1"),
        ([], "the API app has 0 machines in the app process group; exactly one is expected"),
        (
            [machine("started"), machine("stopped")],
            "the API app has 2 machines in the app process group; exactly one is expected",
        ),
    ],
)
def test_the_api_app_holds_exactly_one_machine(
    machines: list[dict[str, object]],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps(machines)))
    code, out, err = run(["deploy", "check-machine-count"], capsys)
    if expected == "api machines: 1":
        assert (code, out, err) == (0, "api machines: 1\n", "")
    else:
        assert (code, out, err) == (1, "", f"error: {expected}\n")


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ("not json", "machine list: not JSON"),
        ("{}", "machine list: not a JSON list"),
        ("[1]", "machine list: an entry is not a JSON object"),
    ],
)
def test_the_machine_list_must_be_a_list_of_objects(listing: str, expected: str) -> None:
    with pytest.raises(DeployCheckFailed, match=expected):
        check_machine_count(listing)


# --- the scheduler's first tick after an update ---

UPDATED = 1_760_000_000.0  # epoch seconds, a fixed instant


def event(kind: str, source: str, at: float | str) -> dict[str, object]:
    return {"type": kind, "status": "x", "source": source, "timestamp": at}


@pytest.mark.parametrize(
    ("events", "ticked"),
    [
        # A scheduled start after the update, in Fly's epoch milliseconds.
        (
            [
                event("update", "user", UPDATED * 1000),
                event("start", "flyd", (UPDATED + 60) * 1000),
            ],
            True,
        ),
        # The same in seconds, and as an ISO time.
        ([event("start", "flyd", UPDATED + 60)], True),
        ([event("start", "flyd", "2025-10-09T08:55:00Z")], True),
        # Only the update.
        ([event("update", "user", UPDATED * 1000)], False),
        # A start from before the update: the last tick, not the next.
        ([event("start", "flyd", (UPDATED - 3600) * 1000)], False),
        # A start by hand (source `user`) is not the schedule.
        ([event("start", "user", (UPDATED + 60) * 1000)], False),
        # An exit or a launch after it is not a start.
        ([event("exit", "flyd", (UPDATED + 60) * 1000)], False),
        # Exactly at the update is not later.
        ([event("start", "flyd", UPDATED * 1000)], False),
        ([], False),
        (None, False),
        ("not a list", False),
        ([{"type": "start", "source": "flyd", "timestamp": "garbage"}], False),
    ],
)
def test_the_scheduler_ticks_after_a_deploy(events: object, ticked: bool) -> None:
    assert ticked_after(events, UPDATED) is ticked


class FakeClock:
    """A clock the wait advances itself: `sleep` moves `now`."""

    def __init__(self) -> None:
        self.at = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.at += seconds
        # A wait that never gives up fails here, not by hanging the suite.
        assert len(self.sleeps) <= 1000, "the wait never ended"


def test_the_wait_passes_when_a_tick_appears_and_names_the_machine_when_none_does() -> None:
    # A tick after 30 minutes of waiting.
    clock = FakeClock()
    tick = event("start", "flyd", (UPDATED + 1800) * 1000)
    reads: list[float] = []

    def events() -> object:
        reads.append(clock.at)
        return [tick] if clock.at >= 30 * 60 else [event("update", "user", UPDATED * 1000)]

    assert (
        wait_for_tick(events, UPDATED, "m-synthetic", now=clock.now, sleep=clock.sleep)
        == "scheduler ticked after the update"
    )
    assert reads[0] == 0 and reads[-1] == 30 * 60

    # None, ever: it gives up at 70 minutes, no sooner and no later, and
    # names the machine.
    clock = FakeClock()
    only_the_update = [event("update", "user", UPDATED * 1000)]
    with pytest.raises(DeployCheckFailed) as failed:
        wait_for_tick(
            lambda: only_the_update, UPDATED, "m-synthetic", now=clock.now, sleep=clock.sleep
        )
    assert clock.at == 70 * 60
    assert "m-synthetic" in str(failed.value)
    assert "70 minutes" in str(failed.value)


def test_the_wait_sees_a_tick_that_already_happened_without_waiting() -> None:
    clock = FakeClock()
    ticked = [event("start", "flyd", (UPDATED + 5) * 1000)]
    assert (
        wait_for_tick(lambda: ticked, UPDATED, "m", now=clock.now, sleep=clock.sleep)
        == "scheduler ticked after the update"
    )
    assert clock.sleeps == []


def test_the_tick_check_through_the_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI reads the machine's events with `fly machine status`, which
    these tests replace, and prints the line or the failure."""
    asked: list[tuple[str, str]] = []

    def fly_events(app: str, machine_id: str) -> object:
        asked.append((app, machine_id))
        return [event("start", "flyd", (UPDATED + 60) * 1000)]

    monkeypatch.setattr(cli, "machine_events", fly_events)
    code, out, err = run(
        [
            "deploy",
            "check-scheduler-tick",
            "--app",
            "app-x",
            "--machine",
            "m-1",
            "--since",
            str(UPDATED),
        ],
        capsys,
    )
    assert (code, out, err) == (0, "scheduler ticked after the update\n", "")
    assert asked == [("app-x", "m-1")]

    clock = FakeClock()
    monkeypatch.setattr("time.time", clock.now)
    monkeypatch.setattr("time.sleep", clock.sleep)

    def no_events(_app: str, _machine: str) -> object:
        return []

    monkeypatch.setattr(cli, "machine_events", no_events)
    code, out, err = run(
        [
            "deploy",
            "check-scheduler-tick",
            "--app",
            "app-x",
            "--machine",
            "m-1",
            "--since",
            str(UPDATED),
        ],
        capsys,
    )
    assert (code, out) == (1, "")
    assert err.startswith("error: the scheduler machine m-1 did not tick within 70 minutes")


# --- the Fly configuration's project values ---

FLY_FILES = sorted((repo_root() / "deploy").glob("fly.*.toml"))
PROJECT_ENV = {
    "HARRIER_SUPABASE_URL": "https://synthetic-project.example.invalid",
    "HARRIER_SUPABASE_PUBLISHABLE_KEY": SYNTHETIC_KEY,
}


def test_the_fly_files_exist() -> None:
    assert [path.name for path in FLY_FILES] == ["fly.production.toml", "fly.staging.toml"]


@pytest.mark.parametrize("path", FLY_FILES, ids=lambda path: path.name)
def test_the_fly_files_are_filled_with_the_projects_values(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in PROJECT_ENV.items():
        monkeypatch.setenv(name, value)
    out = tmp_path / "filled.toml"
    code, printed, err = run(
        ["deploy", "fill-fly-config", "--config", str(path), "--out", str(out)], capsys
    )
    assert (code, printed, err) == (0, f"wrote {out}\n", "")
    filled = tomllib.loads(out.read_text(encoding="utf-8"))
    assert filled["env"]["HARRIER_SUPABASE_URL"] == PROJECT_ENV["HARRIER_SUPABASE_URL"]
    assert filled["env"]["HARRIER_SUPABASE_PUBLISHABLE_KEY"] == SYNTHETIC_KEY
    # What the file named stays.
    assert filled["env"]["HARRIER_AUTH"] == "supabase"
    assert "__HARRIER_" not in out.read_text(encoding="utf-8")


def service_role_jwt() -> str:
    def part(claims: Mapping[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    return ".".join([part({"alg": "none"}), part({"role": "service_role"}), "signature"])


@pytest.mark.parametrize(
    ("environ", "refusal"),
    [
        ({}, "harrier deploy fill-fly-config needs HARRIER_SUPABASE_URL"),
        (
            {"HARRIER_SUPABASE_URL": PROJECT_ENV["HARRIER_SUPABASE_URL"]},
            "harrier deploy fill-fly-config needs HARRIER_SUPABASE_PUBLISHABLE_KEY",
        ),
        (
            {**PROJECT_ENV, "HARRIER_SUPABASE_URL": 'https://a"b'},
            "HARRIER_SUPABASE_URL holds a quote, a backslash or a control character",
        ),
        (
            {**PROJECT_ENV, "HARRIER_SUPABASE_PUBLISHABLE_KEY": "a\\b"},
            "HARRIER_SUPABASE_PUBLISHABLE_KEY holds a quote, a backslash or a control character",
        ),
        (
            {**PROJECT_ENV, "HARRIER_SUPABASE_PUBLISHABLE_KEY": "line\nbreak"},
            "HARRIER_SUPABASE_PUBLISHABLE_KEY holds a quote, a backslash or a control character",
        ),
        (
            {**PROJECT_ENV, "HARRIER_SUPABASE_PUBLISHABLE_KEY": "sb_secret_synthetic"},
            "HARRIER_SUPABASE_PUBLISHABLE_KEY holds a secret key; it takes the publishable key",
        ),
        (
            {**PROJECT_ENV, "HARRIER_SUPABASE_PUBLISHABLE_KEY": service_role_jwt()},
            "HARRIER_SUPABASE_PUBLISHABLE_KEY holds a secret key; it takes the publishable key",
        ),
    ],
)
def test_the_fly_configuration_is_not_filled_from_a_bad_value(
    environ: dict[str, str], refusal: str
) -> None:
    text = FLY_FILES[0].read_text(encoding="utf-8")
    with pytest.raises(DeployCheckFailed) as failed:
        fill_fly_config(text, environ)
    assert str(failed.value) == refusal
    # Never a value.
    for value in environ.values():
        assert value not in str(failed.value) or value == ""


def test_a_placeholder_left_in_the_configuration_fails_the_deploy() -> None:
    text = 'HARRIER_NEW = "__HARRIER_NEW_THING__"\n'
    with pytest.raises(DeployCheckFailed, match="__HARRIER_NEW_THING__"):
        fill_fly_config(text, PROJECT_ENV)
    # A file with none passes through as it is.
    assert fill_fly_config('A = "b"\n', {}) == 'A = "b"\n'


# --- the monthly cost projection ---


def lines(**figures: str) -> list[CostLine]:
    return [CostLine(name, Decimal(amount)) for name, amount in figures.items()]


@pytest.mark.parametrize(
    ("as_of", "figures", "code", "report"),
    [
        # 2.00 + 0.50 over 12 of 31 days: 2.50 * 31 / 12 = 6.4583, over 5.
        (
            "2026-10-12",
            ["fly=2.00", "vercel=0.50"],
            1,
            "projected 6.46 USD for 2026-10, from 2.50 USD over 12 of 31 days; "
            "largest line: fly (2.00 USD)\nover the 5 USD budget\n",
        ),
        # 1.00 over the same 12 days: 31 / 12 = 2.5833, under 5.
        (
            "2026-10-12",
            ["fly=0.40", "vercel=0.00", "egress=0.60"],
            0,
            "projected 2.58 USD for 2026-10, from 1.00 USD over 12 of 31 days; "
            "largest line: egress (0.60 USD)\n",
        ),
        # Exactly 5.00 is not over: spent 5.00 on the last day of a 31 day month.
        (
            "2026-10-31",
            ["fly=5.00"],
            0,
            "projected 5.00 USD for 2026-10, from 5.00 USD over 31 of 31 days; "
            "largest line: fly (5.00 USD)\n",
        ),
        # 5.0013 prints as 5.00 and is still over: compared before rounding.
        (
            "2026-10-30",
            ["fly=4.84"],
            1,
            "projected 5.00 USD for 2026-10, from 4.84 USD over 30 of 31 days; "
            "largest line: fly (4.84 USD)\nover the 5 USD budget\n",
        ),
        # A month's length is the month's: a leap February.
        (
            "2028-02-10",
            ["fly=1.00"],
            0,
            "projected 2.90 USD for 2028-02, from 1.00 USD over 10 of 29 days; "
            "largest line: fly (1.00 USD)\n",
        ),
        # Equal lines: the first given is the largest named.
        (
            "2026-10-10",
            ["vercel=0.10", "fly=0.10"],
            0,
            "projected 0.62 USD for 2026-10, from 0.20 USD over 10 of 31 days; "
            "largest line: vercel (0.10 USD)\n",
        ),
        # Nothing billed yet.
        (
            "2026-10-01",
            ["fly=0", "vercel=0.00"],
            0,
            "projected 0.00 USD for 2026-10, from 0.00 USD over 1 of 31 days; "
            "largest line: fly (0.00 USD)\n",
        ),
    ],
)
def test_the_cost_projection_reports_a_month_over_budget(
    as_of: str,
    figures: list[str],
    code: int,
    report: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["deploy", "project-cost", "--as-of", as_of]
    for figure in figures:
        argv += ["--line", figure]
    assert run(argv, capsys) == (code, report, "")


def test_the_projection_reads_no_network_and_no_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("project-cost reached for the network or a process")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr("subprocess.run", refuse)
    monkeypatch.setattr("socket.socket.connect", refuse)
    for name in ("FLY_API_TOKEN", "VERCEL_TOKEN"):
        monkeypatch.setenv(name, "must-not-be-read")
    code, out, _ = run(
        ["deploy", "project-cost", "--as-of", "2026-10-12", "--line", "fly=1"], capsys
    )
    assert code == 0
    assert "must-not-be-read" not in out


def test_the_projection_is_the_spec_formula() -> None:
    projection = project_cost(date(2026, 10, 12), lines(fly="3.10", vercel="0.00"))
    assert projection.spent == Decimal("3.10")
    assert projection.projected == Decimal("3.10") * 31 / 12
    assert (projection.days_elapsed, projection.days_in_month) == (12, 31)
    assert projection.largest.name == "fly"
    assert projection.over_budget is True  # 8.008 > 5
    assert deploychecks.BUDGET_USD == 5


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["--as-of", "2026-10-12"], "--line"),
        (["--line", "fly=1"], "--as-of"),
        (["--as-of", "2026-10-12", "--line", "fly"], "--line"),
        (["--as-of", "2026-10-12", "--line", "=1"], "--line"),
        (["--as-of", "2026-10-12", "--line", "fly=abc"], "--line"),
        (["--as-of", "2026-10-12", "--line", "fly=-1"], "--line"),
        (["--as-of", "2026-10-12", "--line", "fly=nan"], "--line"),
        (["--as-of", "2026-10-12", "--line", "fly=inf"], "--line"),
        (["--as-of", "2026-10-12", "--line", "fly=1", "--line", "fly=2"], "--line"),
        (["--as-of", "2026-02-30", "--line", "fly=1"], "--as-of"),
        (["--as-of", "12 October", "--line", "fly=1"], "--as-of"),
        (["--as-of", "2026-13-01", "--line", "fly=1"], "--as-of"),
    ],
)
def test_project_cost_refuses_a_malformed_line(
    argv: list[str], named: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refused:
        main(["deploy", "project-cost", *argv])
    assert refused.value.code == 2
    err = capsys.readouterr().err
    assert f"argument {named}:" in err or f"arguments are required: {named}" in err


def test_a_name_given_twice_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        main(
            [
                "deploy",
                "project-cost",
                "--as-of",
                "2026-10-12",
                "--line",
                "fly=1",
                "--line",
                "fly=2",
            ]
        )
    assert refused.value.code == 2
    assert "fly is given twice" in capsys.readouterr().err


def test_a_deploy_command_opens_no_database(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`harrier deploy` never configures logging, which opens the tracker, and
    creates no local database. Logging is reset to unconfigured first, as in a
    fresh process, so a command that set it up would open and create the file
    here."""
    import harrier.logsetup as logsetup
    from harrier.db import default_db_path

    monkeypatch.setattr(logsetup, "_configured", False)
    code, _, _ = run(["deploy", "project-cost", "--as-of", "2026-10-12", "--line", "fly=1"], capsys)
    assert code == 0
    assert not default_db_path().exists()


def test_a_deploy_command_runs_whatever_store_the_environment_names(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The deploy job exports no store URL for these checks, but a shell that
    has one is not refused as a command that needs the store: they open none."""
    # Nothing listens on port 1.
    monkeypatch.setenv("HARRIER_DATABASE_URL", "postgresql://someone@127.0.0.1:1/nowhere")
    code, out, err = run(
        ["deploy", "project-cost", "--as-of", "2026-10-12", "--line", "fly=1"], capsys
    )
    assert (code, err) == (0, "")
    assert out.startswith("projected ")


# --- reading Fly's machine events ---


class _Completed:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_themachine_events_come_from_fly_machine_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`fly machine status <id> --app <app> --json`, its `events`; a failed
    command, output that is not a machine object, or no `fly` at all is
    nothing yet, reported by class and never by text."""
    import subprocess

    asked: list[list[str]] = []
    events = [event("start", "flyd", (UPDATED + 60) * 1000)]
    reply: dict[str, object] = {"out": _Completed(0, json.dumps({"id": "m1", "events": events}))}

    def fake_run(command: list[str], **_kwargs: object) -> object:
        asked.append(command)
        outcome = reply["out"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.machine_events("app-x", "m1") == events
    assert asked == [["fly", "machine", "status", "m1", "--app", "app-x", "--json"]]

    # A machine object with no events field has none yet.
    reply["out"] = _Completed(0, json.dumps({"id": "m1"}))
    assert cli.machine_events("app-x", "m1") is None

    # fly failing is not an answer, whatever it printed.
    reply["out"] = _Completed(1, json.dumps({"id": "m1", "events": events}))
    assert cli.machine_events("app-x", "m1") is None
    capsys.readouterr()

    reply["out"] = _Completed(0, "not json")
    assert cli.machine_events("app-x", "m1") is None
    reply["out"] = _Completed(0, "[]")
    assert cli.machine_events("app-x", "m1") is None
    reply["out"] = FileNotFoundError("synthetic-secret-path")
    assert cli.machine_events("app-x", "m1") is None
    err = capsys.readouterr().err
    assert "FileNotFoundError" in err
    assert "synthetic-secret-path" not in err and "app-x" not in err


# --- what the Auth settings request may do ---


def test_a_redirect_is_an_answer_and_the_key_is_not_sent_after_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Redirecting(_Settings):
        def do_GET(self) -> None:
            type(self).seen.append((self.path, self.headers.get("apikey")))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirecting)
    Redirecting.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        set_project(monkeypatch, f"http://127.0.0.1:{server.server_address[1]}")
        code, out, err = run(["deploy", "check-auth-settings"], capsys)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert (code, out) == (1, "")
    assert err == "error: auth settings: the project answered 302\n"
    assert Redirecting.seen == [("/auth/v1/settings", SYNTHETIC_KEY)]


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://project.example.invalid:notaport", SYNTHETIC_KEY),
        ("https://project.example.invalid", "key-with\nnewline"),
    ],
    ids=["a port that is not a number", "a key with a newline"],
)
def test_a_request_that_cannot_be_formed_is_named_by_class_only(
    url: str, key: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_project(monkeypatch, url, key=key)
    code, out, err = run(["deploy", "check-auth-settings"], capsys)
    assert (code, out) == (1, "")
    assert err.startswith("error: auth settings: no answer (")
    assert "notaport" not in err and "newline" not in err


def test_a_delete_character_cannot_be_filled_into_toml() -> None:
    """TOML forbids U+007F in a string as it forbids the other control
    characters."""
    environ = {**PROJECT_ENV, "HARRIER_SUPABASE_PUBLISHABLE_KEY": "key\x7fvalue"}
    with pytest.raises(DeployCheckFailed, match="control character"):
        fill_fly_config(FLY_FILES[0].read_text(encoding="utf-8"), environ)
