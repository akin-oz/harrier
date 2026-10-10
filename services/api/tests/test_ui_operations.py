"""The Operations page's routes (spec 050, as amended by spec 096).

Three properties carry this file.

**One implementation.** Each write is a run of the CLI verb the terminal
runs. The tests take the argv a route actually started and run it as the CLI,
so a route that grew its own copy of a report would not reach the domain
function these tests watch.

**An empty body changes nothing.** A dropped body or a client that forgot a
field must not apply a reconsideration, send a message, prune the watchlist,
or delete an archive. Each is a separate, explicit request.

**The schedule never reads healthy on a guess.** The container cannot ask
launchd, so the schedule read reports cadences and last successes, marks a
job with no recent success as overdue, and carries no installed or loaded
field at all.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from conftest import auth
from fastapi.testclient import TestClient

from harrier.backup import ARCHIVE_PREFIX, ARCHIVE_SUFFIX
from harrier.db import connect
from harrier.runoutcome import DIGEST_JOB, DISCOVERY_JOB, record_success
from harrier.schedule import (
    CalendarTime,
    ScheduleConfigError,
    ScheduleJob,
    describe_cadence,
    longest_gap,
)
from harrier.screening.config import load_candidate_config
from harrier.screening.policy import policy_version
from harrier.screening.seen import REJECTED, SeenDecision, load_seen, save_seen
from harrier.tracker.store import add_job
from harrier.tracks import default_scope
from harrier_api.app import create_app
from harrier_api.runs import PARAMETERIZED_KINDS, Run, RunManager
from harrier_cli.main import main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARRIER_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    add_job(
        conn,
        {
            "company": "Northwind Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/northwind/1",
            "source": "greenhouse",
            "location": "Remote, Europe",
        },
        scope=default_scope(conn),
    )
    conn.close()
    return tmp_path


async def _idle(run: object) -> None:
    """A run that never executes, so the test reads the argv it was given."""


@pytest.fixture
def manager(env: Path) -> Iterator[RunManager]:
    runs = RunManager(journal_path=env / "journal.jsonl")
    with patch.object(runs, "_execute", side_effect=_idle):
        yield runs


@pytest.fixture
def client(manager: RunManager) -> TestClient:
    return TestClient(create_app(run_manager=manager))


def ended(manager: RunManager) -> None:
    """Every idle run ends, as a report has ended before its write is asked
    for. One run of a kind at a time, so the write would otherwise be
    refused as a request with other options (review of PR #208)."""
    for run in manager.list_runs():
        run.state = "succeeded"


def started(manager: RunManager, response: Any) -> Run:
    assert response.status_code == 200, response.text
    run = manager.get(response.json()["id"])
    assert run is not None
    return run


def as_cli(run: Run) -> list[str]:
    """The argv after `python -m harrier_cli.main`, as the CLI receives it."""
    assert run.command[1:3] == ["-m", "harrier_cli.main"]
    return run.command[3:]


# --- one implementation, two callers -----------------------------------------

# Route, body, and the domain function the CLI verb calls.
RUN_PAIRINGS: dict[str, tuple[str, dict[str, object], str]] = {
    "check-feeds": ("/ops/feeds", {}, "harrier.feedhealth.check_feeds"),
    "prune": ("/ops/feeds/prune", {"confirm": True}, "harrier.feedhealth.prune_dead"),
    "reconsider": ("/ops/reconsider", {}, "harrier.screening.reconsider.reconsider_source"),
    "backup": ("/ops/backup", {}, "harrier.backup.create_backup"),
    "digest": ("/ops/digest", {}, "harrier.digest.run_digest"),
}


@pytest.mark.parametrize("name", sorted(RUN_PAIRINGS))
def test_every_operations_route_calls_the_cli_verbs_function(
    name: str, client: TestClient, manager: RunManager
) -> None:
    """The argv the route started, run as the CLI, reaches the function the
    verb calls. A route with its own copy of the report would not arrive."""
    path, body, function = RUN_PAIRINGS[name]
    run = started(manager, client.post(path, json=body, headers=auth()))

    with (
        patch("harrier.feedhealth.load_feeds_for_check", return_value=["https://x.example"]),
        patch(function) as domain,
        suppress(Exception),
    ):
        if name == "prune":
            from harrier.feedhealth import DEAD, BoardHealth, FeedHealthReport

            dead = BoardHealth("https://x.example", "greenhouse", DEAD, "404")
            report = FeedHealthReport((dead,))
            domain.return_value = ()
            with patch("harrier.feedhealth.check_feeds", return_value=report):
                main(as_cli(run))
        else:
            domain.return_value = MagicMock()
            main(as_cli(run))
    assert domain.call_args is not None, f"{path} did not reach {function}"


def test_the_schedule_reads_the_definition_the_cli_installs(client: TestClient) -> None:
    from harrier.schedule import load_schedule

    with patch("harrier.schedule.load_schedule", wraps=load_schedule) as loader:
        assert client.get("/ops/schedule").status_code == 200
    assert loader.call_args is not None
    assert str(loader.call_args.args[0]).endswith("config/schedule.json")


def test_the_profile_list_is_the_one_the_cli_prints(client: TestClient) -> None:
    documents = [{"kind": "truth", "name": "invented", "format": "markdown", "updated_at": "t"}]
    with (
        patch("harrier.profile.list_documents", return_value=documents) as route_side,
        patch("harrier_cli.main.list_documents", return_value=documents) as cli_side,
    ):
        body = client.get("/ops/profile").json()
        assert main(["profile", "list"]) == 0
    assert route_side.call_args is not None and cli_side.call_args is not None
    assert body == documents


def test_every_operations_route_is_in_the_contract(env: Path) -> None:
    paths = create_app().openapi()["paths"]
    for path, method in (
        ("/ops/feeds", "post"),
        ("/ops/feeds/prune", "post"),
        ("/ops/reconsider", "post"),
        ("/ops/backup", "post"),
        ("/ops/digest", "post"),
        ("/ops/schedule", "get"),
        ("/ops/profile", "get"),
    ):
        assert method in paths.get(path, {}), (method, path)
    # Removed by spec 096 before spec 050 was built: the container cannot
    # load a launchd job, parity is repository upkeep, and the export is a
    # download spec 096 builds.
    for gone in ("/ops/schedule/install", "/ops/schedule/uninstall", "/ops/parity"):
        assert gone not in paths
    assert "post" not in paths.get("/ops/export", {})


# --- an empty body changes nothing --------------------------------------------


@pytest.mark.parametrize("send_body", [True, False], ids=["empty object", "no body"])
def test_an_empty_body_applies_sends_or_prunes_nothing(
    send_body: bool, client: TestClient, manager: RunManager
) -> None:
    def post(path: str) -> Any:
        if send_body:
            return client.post(path, json={}, headers=auth())
        return client.post(path, headers=auth())

    reconsider = as_cli(started(manager, post("/ops/reconsider")))
    assert "--apply" not in reconsider

    digest = as_cli(started(manager, post("/ops/digest")))
    assert "--dry-run" in digest

    backup = as_cli(started(manager, post("/ops/backup")))
    assert "--no-prune" in backup

    before = len(manager.list_runs())
    refused = post("/ops/feeds/prune")
    assert refused.status_code == 409
    assert "confirm" in refused.json()["detail"]
    assert len(manager.list_runs()) == before, "a refused prune still started a run"


def test_reconsideration_reports_by_default_and_applies_on_request(
    env: Path, client: TestClient, manager: RunManager, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default clears nothing, run end to end through the CLI; applying
    is a second request with its own field."""
    save_seen("greenhouse", {"stale-key": SeenDecision(REJECTED, "low_score", "older", "t")})

    report = as_cli(started(manager, client.post("/ops/reconsider", json={}, headers=auth())))
    assert main(report) == 0
    assert "1 would be cleared; re-run with --apply" in capsys.readouterr().out
    assert "stale-key" in load_seen("greenhouse"), "the default cleared a rejection"

    ended(manager)
    apply = as_cli(
        started(manager, client.post("/ops/reconsider", json={"apply": True}, headers=auth()))
    )
    assert "--apply" in apply
    assert main(apply) == 0
    assert "stale-key" not in load_seen("greenhouse")


def test_nothing_eligible_keeps_the_domain_s_words(
    env: Path, client: TestClient, manager: RunManager, capsys: pytest.CaptureFixture[str]
) -> None:
    """The route's run prints the CLI's own words, which the page shows
    verbatim, so the two outcomes the CLI tells apart stay apart."""
    current = SeenDecision(REJECTED, "low_score", policy_version(load_candidate_config()), "t")
    save_seen("greenhouse", {"current-key": current})
    body = {"source": "greenhouse"}
    argv = as_cli(started(manager, client.post("/ops/reconsider", json=body, headers=auth())))
    assert "--source=greenhouse" in argv
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "0 under older rules" in out
    assert "nothing is eligible to clear" in out


def test_reconsidering_another_track_names_it_to_the_cli(
    client: TestClient, manager: RunManager
) -> None:
    """The CLI reconsiders an academic track's own seen state (spec 097);
    the route hands it the track the request named, and only that."""
    from harrier.tracks import add_track

    conn = connect()
    add_track(conn, "second", "academic", "Second")
    conn.close()
    response = client.post("/ops/reconsider", params={"track": "second"}, json={}, headers=auth())
    assert as_cli(started(manager, response))[:2] == ["--track=second", "reconsider"]
    default = client.post("/ops/reconsider", json={}, headers=auth())
    assert as_cli(started(manager, default))[0] == "reconsider"


def test_an_unknown_source_is_refused_before_a_run(client: TestClient, manager: RunManager) -> None:
    response = client.post("/ops/reconsider", json={"source": "--apply"}, headers=auth())
    assert response.status_code == 422
    assert manager.list_runs() == []


# --- backup -----------------------------------------------------------------------


def _archives(directory: Path, count: int) -> list[Path]:
    """Old archives in one ISO week, enough that the CLI's default retention
    would delete some of them."""
    directory.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for index in range(count):
        stamp = f"2020-01-06-{index:02d}0000"
        path = directory / f"{ARCHIVE_PREFIX}{stamp}{ARCHIVE_SUFFIX}"
        path.write_text("an old archive", encoding="utf-8")
        made.append(path)
    return made


def test_an_empty_backup_writes_an_archive_and_deletes_nothing(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    from harrier.backup import DEFAULT_KEEP

    backups = env / "backups"
    old = _archives(backups, DEFAULT_KEEP + 2)
    argv = as_cli(started(manager, client.post("/ops/backup", headers=auth())))

    with patch("harrier.backup.prune") as retention:
        assert main(argv) == 0
    assert retention.call_args is None, "the retention prune ran on an empty body"
    assert all(path.exists() for path in old)
    assert len(list(backups.glob(f"{ARCHIVE_PREFIX}*"))) == len(old) + 1


def test_pruning_is_the_cli_s_own_retention_when_asked(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    from harrier.backup import DEFAULT_KEEP

    _archives(env / "backups", DEFAULT_KEEP + 2)
    response = client.post("/ops/backup", json={"prune": True}, headers=auth())
    argv = as_cli(started(manager, response))
    assert "--no-prune" not in argv
    with patch("harrier.backup.prune", return_value=()) as retention:
        assert main(argv) == 0
    assert retention.call_args is not None
    assert retention.call_args.args[1] == DEFAULT_KEEP


def test_a_backup_that_fails_verification_leaves_no_archive(
    env: Path, client: TestClient, manager: RunManager, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = as_cli(started(manager, client.post("/ops/backup", headers=auth())))
    with patch("harrier.backup.verify_archive", return_value=-1):
        assert main(argv) == 1
    assert "backup failed: archive verification disagreed" in capsys.readouterr().err
    assert list((env / "backups").glob(f"{ARCHIVE_PREFIX}*")) == []


# --- the digest -------------------------------------------------------------------


class _Delivered:
    """A Telegram API that answers 200 and counts what it was sent."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: object, timeout: float = 0) -> Any:
        self.calls += 1
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        return response


class _InProcess(RunManager):
    """A manager that runs the CLI in this process, so a patched network
    reaches the run and the record it writes is the one the next request
    reads."""

    async def _run_process(self, run: Run) -> None:
        await self._set_state(run, "running")
        run.exit_code = main(run.command[3:])
        await self._set_state(run, "succeeded" if run.exit_code == 0 else "failed")


def _finished(manager: RunManager, run_id: str) -> Run:
    for _ in range(200):
        run = manager.get(run_id)
        if run is not None and run.state in ("succeeded", "failed"):
            return run
        time.sleep(0.02)
    raise AssertionError("the run did not finish")


def test_a_second_digest_for_the_same_day_is_refused_and_one_is_delivered(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "invented-not-a-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "invented-chat")
    telegram = _Delivered()
    runs = _InProcess(journal_path=env / "journal.jsonl")
    body = {"dry_run": False, "date": "2026-10-09"}

    with (
        patch("harrier.notify.urllib.request.urlopen", telegram),
        TestClient(create_app(run_manager=runs)) as client,
    ):
        first = client.post("/ops/digest", json=body, headers=auth())
        assert first.status_code == 200, first.text
        assert _finished(runs, first.json()["id"]).state == "succeeded"

        second = client.post("/ops/digest", json=body, headers=auth())

    assert telegram.calls == 1, "the same day's digest was delivered twice"
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert "2026-10-09 was already sent at 20" in detail, detail
    assert "resend" in detail


def test_resending_is_its_own_explicit_request(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    conn = connect()
    record_success(conn, f"{DIGEST_JOB}:2026-10-09")
    conn.close()
    body = {"dry_run": False, "date": "2026-10-09", "resend": True}
    argv = as_cli(started(manager, client.post("/ops/digest", json=body, headers=auth())))
    assert "--dry-run" not in argv
    assert "--date=2026-10-09" in argv


def test_a_double_click_joins_the_send_already_in_flight(
    client: TestClient, manager: RunManager
) -> None:
    body = {"dry_run": False, "date": "2026-10-09"}
    first = client.post("/ops/digest", json=body, headers=auth()).json()["id"]
    again = client.post("/ops/digest", json=body, headers=auth()).json()["id"]
    dry = client.post("/ops/digest", json={"date": "2026-10-09"}, headers=auth()).json()["id"]
    assert again == first
    # A preview of the same day is a different run: joining the send would
    # tell the operator nothing had been sent, and the reverse would claim a
    # send that never happened.
    assert dry != first


def test_a_digest_produced_and_not_delivered_says_so(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 1 means produced, step 2 means delivered; the exit status alone
    cannot tell a failed send from a crash, since both are 1."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "invented-not-a-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "invented-chat")
    refused = MagicMock(side_effect=OSError("network unreachable"))
    with patch("harrier.notify.urllib.request.urlopen", refused):
        assert main(["digest", "--date=2026-10-09"]) == 1
    assert refused.call_count == 1
    out = capsys.readouterr().out
    assert '"step": 1' in out and "digest produced" in out
    assert "digest delivered" not in out

    with (
        patch("harrier.digest.build_digest", side_effect=RuntimeError("broken")),
        pytest.raises(RuntimeError),
    ):
        main(["digest", "--date=2026-10-09"])
    assert "digest produced" not in capsys.readouterr().out


def test_the_digest_refuses_a_non_default_track(client: TestClient) -> None:
    from harrier.tracks import add_track

    conn = connect()
    add_track(conn, "second", "academic", "Second")
    conn.close()
    response = client.post("/ops/digest", params={"track": "second"}, json={}, headers=auth())
    assert response.status_code == 409
    assert response.json()["detail"] == "digest is not available on track second"


# --- the schedule, as the container can see it -----------------------------------


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_the_schedule_marks_a_job_with_no_recent_success_and_claims_no_install_state(
    env: Path, client: TestClient
) -> None:
    now = datetime.now(UTC)
    conn = connect()
    record_success(conn, DISCOVERY_JOB, at=_iso(now - timedelta(hours=1)))
    record_success(conn, DIGEST_JOB, at=_iso(now - timedelta(days=10)))
    conn.close()

    body = client.get("/ops/schedule").json()
    jobs = {job["name"]: job for job in body["jobs"]}
    assert body["error"] is None
    assert body["host_command"] == "harrier schedule status"
    assert "host" in body["installed_state"]

    discovery = jobs["discovery"]["records"]
    assert [record["overdue"] for record in discovery] == [False]
    assert jobs["discovery"]["cadence"] == "daily at 09:00, 13:00, 16:00, 20:00"

    digest = jobs["digest"]["records"]
    assert [record["overdue"] for record in digest] == [True]
    assert digest[0]["summary"] == "digest: last succeeded 10 days ago"

    watch = jobs["gmail-watch"]["records"]
    assert watch[0]["last_success_at"] is None and watch[0]["overdue"] is True

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            found = set(value)
            for item in value.values():
                found |= keys(item)
            return found
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value)) if value else set()
        return set()

    # The container cannot know these, so the response has no field to
    # fill with a guess.
    assert not keys(body) & {"installed", "loaded", "drifted", "last_exit_status", "next_run"}


def test_an_unreadable_schedule_is_reported_in_the_loader_s_words(client: TestClient) -> None:
    with patch("harrier.schedule.load_schedule", side_effect=ScheduleConfigError("no jobs")):
        body = client.get("/ops/schedule").json()
    assert body["jobs"] == []
    assert body["error"] == "no jobs"


def test_overdue_is_twice_the_longest_gap() -> None:
    daily = ScheduleJob(
        "discovery",
        ("discover",),
        "calendar",
        times=(CalendarTime(9, 0), CalendarTime(20, 0)),
    )
    weekly = ScheduleJob("weekly", ("discover",), "calendar", times=(CalendarTime(9, 30, 1),))
    watch = ScheduleJob("gmail-watch", ("gmail-watch",), "interval", seconds=300)
    assert longest_gap(daily) == timedelta(hours=13)
    assert longest_gap(weekly) == timedelta(days=7)
    assert longest_gap(watch) == timedelta(minutes=5)
    assert describe_cadence(weekly) == "Mondays at 09:30"
    assert describe_cadence(watch) == "every 5 minutes"


def test_a_success_just_inside_the_limit_is_not_overdue(env: Path) -> None:
    from harrier.paths import repo_root
    from harrier.schedule import SCHEDULE_CONFIG_PATH, job_health

    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    conn = connect()
    record_success(conn, DISCOVERY_JOB, at=_iso(now - timedelta(hours=25)))
    record_success(conn, DIGEST_JOB, at=_iso(now - timedelta(hours=49)))
    health = {
        job.name: job
        for job in job_health(conn, config_path=repo_root() / SCHEDULE_CONFIG_PATH, now=now)
    }
    conn.close()
    # Discovery's longest gap is 13 hours, so 25 hours is within two of them
    # and 49 hours of the daily digest is past two days.
    assert health["discovery"].records[0].overdue is False
    assert health["digest"].records[0].overdue is True


# --- the token ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/ops/feeds", "/ops/feeds/prune", "/ops/reconsider", "/ops/backup", "/ops/digest"]
)
def test_every_operations_write_requires_the_token(client: TestClient, path: str) -> None:
    assert client.post(path, json={}).status_code == 403


@pytest.mark.parametrize("path", ["/ops/schedule", "/ops/profile"])
def test_the_schedule_and_the_profile_list_are_tokenless_reads(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 200


def test_every_new_kind_takes_no_job() -> None:
    for kind in ("check-feeds", "reconsider", "backup", "digest"):
        assert PARAMETERIZED_KINDS[kind].takes_job is False


def test_a_date_reaches_argv_only_as_a_calendar_date() -> None:
    from harrier_api.runs import RunParams, build_command

    argv = build_command("digest", RunParams(dates={"--date": date(2026, 10, 9)}))
    assert argv[-1] == "--date=2026-10-09"
    with pytest.raises(ValueError, match="calendar date"):
        RunParams(dates={"--date": datetime(2026, 10, 9, 1, 2)})
    with pytest.raises(ValueError, match="must be one of"):
        build_command("reconsider", RunParams(choices={"--source": "--apply"}))


# --- spec 095: every command that works on the operator's data ------------------


def _second_track() -> None:
    from harrier.tracks import add_track

    conn = connect()
    add_track(conn, "second", "academic", "Second")
    conn.close()


def _job_id() -> int:
    conn = connect()
    try:
        return int(conn.execute("SELECT id FROM jobs ORDER BY id LIMIT 1").fetchone()[0])
    finally:
        conn.close()


NEW_RUNS: dict[str, tuple[str, str]] = {
    "events-backfill": ("/ops/events/backfill", "harrier.tracker.store.backfill_events"),
    "evaluate-prospects": ("/ops/evaluate-prospects", "harrier.offers.evaluate_prospects"),
    "scoring-export": ("/ops/scoring/export", "harrier.scoring.export.export_features"),
    "discovery": ("/ops/discover", "harrier.discovery.run_discovery"),
}


@pytest.mark.parametrize("kind", sorted(NEW_RUNS))
def test_every_new_route_calls_the_cli_verbs_function(
    kind: str, client: TestClient, manager: RunManager
) -> None:
    """Runs: the argv the route started, run as the CLI, reaches the
    function the verb calls. The requests are below, driven through both
    surfaces with the function patched."""
    path, function = NEW_RUNS[kind]
    run = started(manager, client.post(path, headers=auth()))
    assert run.kind == kind
    with patch(function) as domain, suppress(Exception):
        main(as_cli(run))
    assert domain.call_args is not None, f"{path} did not reach {function}"


def test_every_new_request_calls_the_cli_verbs_function(
    env: Path, client: TestClient, tmp_path: Path
) -> None:
    job = _job_id()
    brief = {"evidence": ["an invented line of evidence"]}

    # events show
    with patch("harrier.tracker.store.list_events", return_value=[]) as domain:
        main(["events", "show", str(job)])
        client.get(f"/tracker/{job}/events")
    cli_call, api_call = domain.call_args_list
    assert cli_call.args[2] == api_call.args[2] == job

    # brief show
    with patch("harrier.apply.brief.brief_text", return_value=None) as domain:
        main(["brief", "show", str(job)])
        client.get(f"/apply/{job}/brief", headers=auth())
    cli_call, api_call = domain.call_args_list
    assert cli_call.args[1] == api_call.args[1] == job

    # brief set: the CLI reads a file, the route takes the body
    from harrier.apply.brief import parse_brief

    brief_file = tmp_path / "brief.json"
    brief_file.write_text(json.dumps(brief), encoding="utf-8")
    with patch("harrier.apply.brief.store_brief", return_value=parse_brief(brief)) as domain:
        main(["brief", "set", str(job), "--file", str(brief_file)])
        client.put(f"/apply/{job}/brief", json=brief, headers=auth())
    cli_call, api_call = domain.call_args_list
    assert cli_call.args[1] == api_call.args[1] == job
    assert json.loads(cli_call.args[2]) == json.loads(api_call.args[2]) == brief

    # check, and check --link-contacts
    with (
        patch("harrier.tracker.invariants.check_rows", return_value=[]) as rows,
        patch("harrier.outreach.joblink.unresolved_links", return_value=[]) as links,
    ):
        main(["check"])
        client.get("/ops/check", headers=auth())
    assert rows.call_count == 2 and links.call_count == 2
    with patch("harrier.outreach.joblink.backfill_job_ids", return_value=(0, 0)) as domain:
        main(["check", "--link-contacts"])
        client.post("/ops/check/link-contacts", json={"confirm": True}, headers=auth())
    assert domain.call_count == 2


def _legacy_rejection(company: str) -> int:
    """A row decided before history was recorded, so it has no events."""
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO jobs (company, title, url, source, status, rejection_reason, "
            "added_at, created_at, updated_at) VALUES (?, ?, ?, 'greenhouse', 'rejected', "
            "'hybrid', '2026-03-02', '2026-03-02 09:00:00', '2026-03-03 09:00:00')",
            (company, "Platform Engineer", f"https://boards.example.com/{company}/1"),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_history_lists_a_jobs_events_in_order(env: Path, client: TestClient) -> None:
    from harrier.tracker.actions import change_status
    from harrier.tracker.store import backfill_events

    job = _job_id()
    conn = connect()
    change_status(conn, default_scope(conn), str(job), "shortlist")
    change_status(conn, default_scope(conn), str(job), "reject", reason="hybrid after all")
    conn.close()

    events = client.get(f"/tracker/{job}/events").json()
    # The add, then the two moves, in the order they were made.
    assert [(e["from_status"], e["to_status"]) for e in events] == [
        ("", "prospect"),
        ("prospect", "shortlisted"),
        ("shortlisted", "rejected"),
    ]
    assert events[-1]["reason_text"] == "hybrid after all"
    # The code reads in the domain's words, so the browser holds no table.
    from harrier.tracker.reasons import label_of

    assert events[-1]["reason_code"]
    assert events[-1]["reason_label"] == label_of(events[-1]["reason_code"])
    assert not any(e["backfilled"] for e in events)

    legacy = _legacy_rejection("Invented Fjord")
    conn = connect()
    backfill_events(conn, default_scope(conn))
    conn.close()
    reconstructed = client.get(f"/tracker/{legacy}/events").json()
    assert reconstructed and all(e["backfilled"] for e in reconstructed)

    # Any track, and only that track's jobs.
    from harrier.tracks import resolve_scope

    _second_track()
    conn = connect()
    elsewhere = add_job(
        conn,
        {"company": "Invented Lab", "title": "Research Engineer", "source": "manual"},
        scope=resolve_scope(conn, "second"),
    )
    conn.close()
    on_second = client.get(f"/tracker/{elsewhere}/events", params={"track": "second"})
    assert on_second.status_code == 200, on_second.text
    assert client.get(f"/tracker/{job}/events", params={"track": "second"}).status_code == 404


def test_an_empty_body_changes_nothing(client: TestClient, manager: RunManager, env: Path) -> None:
    backfill = as_cli(started(manager, client.post("/ops/events/backfill", headers=auth())))
    assert "--dry-run" in backfill

    evaluation = as_cli(started(manager, client.post("/ops/evaluate-prospects", headers=auth())))
    assert "--apply" not in evaluation

    with patch("harrier.outreach.joblink.backfill_job_ids") as link:
        refused = client.post("/ops/check/link-contacts", headers=auth())
        also = client.post("/ops/check/link-contacts", json={}, headers=auth())
    assert refused.status_code == also.status_code == 409
    assert "confirm" in refused.json()["detail"]
    assert link.call_args is None, "an empty body linked contacts"

    # Each write is its explicit field, asked for once the report has ended.
    ended(manager)
    write = client.post("/ops/events/backfill", json={"dry_run": False}, headers=auth())
    assert "--dry-run" not in as_cli(started(manager, write))
    reject = client.post("/ops/evaluate-prospects", json={"apply": True}, headers=auth())
    assert "--apply" in as_cli(started(manager, reject))


def test_the_brief_round_trips_and_a_bad_brief_is_400(
    env: Path, client: TestClient, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    job = _job_id()
    assert client.get(f"/apply/{job}/brief", headers=auth()).status_code == 404

    brief = {
        "never_name": ["Invented Client Ltd"],
        "guidance_url": "https://careers.example.com/guidance",
        "employer_guidance": "Say what you built.",
        "letter": {"max_words": 250, "paragraphs": 3},
        "answers": {"max_sentences": 4},
        "evidence": ["an invented line of evidence"],
        "views": {"Why us?": "An invented view."},
        "compensation_number": "100",
    }
    stored = client.put(f"/apply/{job}/brief", json=brief, headers=auth())
    assert stored.status_code == 200, stored.text
    read = client.get(f"/apply/{job}/brief", headers=auth()).json()
    assert read["never_name"] == brief["never_name"]
    assert read["letter"] == {"max_words": 250, "max_sentences": None, "paragraphs": 3}
    assert read["views"] == brief["views"]
    assert read["confirmed_skills"] == []

    for bad in ({"unknown": 1}, {"evidence": "not a list"}, {"letter": {"max_words": 0}}):
        refused = client.put(f"/apply/{job}/brief", json=bad, headers=auth())
        assert refused.status_code == 400, refused.text
        # The store's words, as the CLI prints them.
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad), encoding="utf-8")
        assert main(["brief", "set", str(job), "--file", str(path)]) == 1
        assert capsys.readouterr().err.strip() == f"brief failed: {refused.json()['detail']}"
    # Nothing a refusal sent was stored.
    assert client.get(f"/apply/{job}/brief", headers=auth()).json()["evidence"] == brief["evidence"]


def test_discovery_options_reach_argv_only_as_validated_values(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    from harrier.userconfig import DISCOVERY, set_config
    from harrier_cli.main import build_parser

    form = {
        "dry_run": "true",
        "notify": "false",
        "only_source": "greenhouse",
        "apify_count": "20",
    }
    argv = as_cli(started(manager, client.post("/ops/discover", data=form, headers=auth())))
    assert argv[0] == "discover"
    assert {"--dry-run", "--no-notify", "--only-source=greenhouse", "--apify-count=20"} <= set(argv)
    assert "--scheduled" not in argv

    for bad in ({"only_source": "--apply"}, {"apify_count": "501"}, {"apify_count": "0"}):
        assert client.post("/ops/discover", data=bad, headers=auth()).status_code == 422, bad

    # Absent, the count is the configured one, bounded where it is read.
    conn = connect()
    set_config(conn, DISCOVERY, {"apify_scheduled_count": 42})
    conn.close()
    # The first discovery is still active here, so read the shadow argv from
    # a fresh manager rather than join it.
    fresh = RunManager(journal_path=env / "fresh.jsonl")
    with patch.object(fresh, "_execute", side_effect=_idle):
        other = TestClient(create_app(run_manager=fresh))
        shadow_argv = as_cli(
            started(fresh, other.post("/ops/discover", data={"shadow": "true"}, headers=auth()))
        )
    assert "--apify-count=42" in shadow_argv
    assert "--shadow" in shadow_argv and "--dry-run" not in shadow_argv
    # `shadow` implies a dry run by the CLI's own rule, not a second copy here.
    from harrier.discovery import DiscoveryOptions

    parsed = build_parser().parse_args(shadow_argv)
    assert DiscoveryOptions(shadow=parsed.shadow, dry_run=parsed.dry_run).dry_run is True


def test_an_upload_becomes_a_run_input_and_is_removed(env: Path) -> None:
    from harrier_api.runs import run_inputs_dir

    runs = RunManager(journal_path=env / "uploads.jsonl")
    seen: list[Any] = []

    def record(kind: str, params: Any) -> list[str]:
        seen.append(params)
        return ["true"]

    # The run is held until the files are read. Without the hold it could
    # finish, and remove them, before the first stat: CI saw exactly that.
    release = threading.Event()
    run_process = runs._run_process  # pyright: ignore[reportPrivateUsage]

    async def held(run: Run) -> None:
        await asyncio.to_thread(release.wait, 10)
        await run_process(run)

    files = {
        "dataset_file": ("export.json", b'[{"title": "Invented Role"}]', "application/json"),
        "wellfound_file": ("wellfound.csv", b"title,company\nRole,Invented Co\n", "text/csv"),
    }
    with (
        patch("harrier_api.runs.build_command", side_effect=record),
        patch.object(runs, "_run_process", new=held),
        TestClient(create_app(run_manager=runs)) as client,
    ):
        response = client.post("/ops/discover", files=files, headers=auth())
        assert response.status_code == 200, response.text
        params = seen[0]
        written = dict(params.input_files)
        assert set(written) == {"--dataset-file", "--wellfound-file"}
        for path in written.values():
            assert path.parent == run_inputs_dir()
            assert path.stat().st_mode & 0o077 == 0, "an upload is readable by other users"
        assert written["--dataset-file"].read_bytes() == files["dataset_file"][1]
        assert written["--wellfound-file"].suffix == ".csv"
        release.set()
        _finished(runs, response.json()["id"])
    assert not any(path.exists() for path in written.values()), "an upload outlived its run"

    over = {"wttj_file": ("big.json", b"x" * (5 * 1024 * 1024 + 1), "application/json")}
    refused_runs = RunManager(journal_path=env / "refused.jsonl")
    with patch.object(refused_runs, "_execute", side_effect=_idle):
        refused = TestClient(create_app(run_manager=refused_runs)).post(
            "/ops/discover", files=over, headers=auth()
        )
    assert refused.status_code == 413
    assert refused_runs.list_runs() == []
    assert list(run_inputs_dir().glob("*")) == [], "a refused upload was written"


def test_inputs_of_a_refused_attempt_are_all_removed(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    """An attempt made while the discovery is already active never becomes a
    run, so every file it wrote goes, not only the first."""
    from harrier_api.runs import run_inputs_dir

    first = started(manager, client.post("/ops/discover", headers=auth()))
    files = {
        "dataset_file": ("a.json", b"[]", "application/json"),
        "wellfound_file": ("b.json", b"[]", "application/json"),
        "wttj_file": ("c.json", b"[]", "application/json"),
    }
    refused = client.post("/ops/discover", files=files, headers=auth())
    assert refused.status_code == 409
    assert first.id in refused.json()["detail"]
    assert list(run_inputs_dir().glob("*")) == [], "a refused attempt left files behind"


def test_inputs_left_by_a_restart_are_swept_at_startup(env: Path) -> None:
    """The served app removes the inputs of runs its journal shows were cut
    off, and nothing else: not a file no journaled run names, which may be
    another process's, and not a journaled name outside the inputs."""
    from harrier_api.runs import RunParams, run_inputs_dir, write_run_input

    journal = env / "restart.jsonl"
    cut_off = write_run_input(b"[]", ".json")
    stopped = RunManager(journal_path=journal)

    async def begin() -> None:
        # Journaled as queued with its input, and never ended: the server
        # stopped mid-run.
        with patch.object(stopped, "_execute", side_effect=_idle):
            await stopped.start("discovery", RunParams(input_files={"--dataset-file": cut_off}))

    asyncio.run(begin())
    stranger = write_run_input("a file no journaled run names")
    outside = env / "outside.json"
    outside.write_text("[]", encoding="utf-8")
    _journal_a_cut_off_run(journal, outside)

    restarted = RunManager(journal_path=journal)
    app = create_app(run_manager=restarted)
    assert cut_off.exists(), "building the app removed an input"
    with TestClient(app):
        pass
    assert not cut_off.exists(), "a cut-off run's input outlived the restart"
    assert stranger.exists(), "a file no journaled run names was removed"
    assert outside.exists(), "a journaled name outside the inputs was removed"
    assert run_inputs_dir().is_dir()


def _journal_a_cut_off_run(journal: Path, path: Path) -> None:
    """A run journaled as running, holding `path`, by a server that stopped."""
    record = {"id": path.stem[:12], "kind": "discovery", "state": "running"}
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({**record, "inputs": [str(path)]}) + "\n")


def test_importing_the_app_deletes_no_run_input(env: Path) -> None:
    """`just contract` imports the app on the host, whose data directory is
    the container's. A live run's upload must survive it (review of PR #208)."""
    import os
    import subprocess
    import sys

    from harrier_api.runs import write_run_input

    live = write_run_input(b"[]", ".json")
    _journal_a_cut_off_run(env / "data" / "runs" / "journal.jsonl", live)
    environment = {**os.environ, "HARRIER_DATA_DIR": str(env / "data")}
    for command in (
        [sys.executable, "-c", "import harrier_api.app"],
        [sys.executable, "-m", "harrier_api.export_openapi", str(env / "openapi.json")],
    ):
        subprocess.run(command, env=environment, check=True, capture_output=True)
        assert live.exists(), f"{command[-1]} deleted a run's input"
    create_app()
    assert live.exists(), "create_app() deleted a run's input"


def test_the_data_check_requires_the_token(env: Path, client: TestClient) -> None:
    assert client.get("/ops/check").status_code == 403
    body = client.get("/ops/check", headers=auth()).json()
    assert body == {"breaches": [], "unresolved_links": []}


def test_no_operator_content_reaches_argv(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    job = _job_id()
    words = "an invented note nobody else should read"
    stored = client.put(f"/apply/{job}/brief", json={"employer_guidance": words}, headers=auth())
    assert stored.status_code == 200
    assert manager.list_runs() == [], "storing a brief started a process"

    hostile = "../../outside/evidence.json"
    files = {"dataset_file": (hostile, words.encode(), "application/json")}
    run = started(manager, client.post("/ops/discover", files=files, headers=auth()))
    argv = " ".join(run.command)
    assert words not in argv
    assert "outside" not in argv and hostile not in argv


ELSEWHERE: list[tuple[str, str, dict[str, Any]]] = [
    ("post", "/ops/events/backfill", {"json": {}}),
    ("get", "/apply/1/brief", {}),
    ("put", "/apply/1/brief", {"json": {}}),
    ("get", "/ops/check", {}),
    ("post", "/ops/check/link-contacts", {"json": {"confirm": True}}),
    ("post", "/ops/evaluate-prospects", {"json": {}}),
    ("post", "/ops/scoring/export", {}),
    ("post", "/ops/discover", {}),
]


def test_operations_refuse_a_non_default_track(
    env: Path, client: TestClient, manager: RunManager
) -> None:
    _second_track()
    for method, path, kwargs in ELSEWHERE:
        response = client.request(
            method, path, params={"track": "second"}, headers=auth(), **kwargs
        )
        assert response.status_code == 409, (method, path, response.text)
        assert "not available on track second" in response.json()["detail"]
    assert manager.list_runs() == []
    # History is the one route that works there.
    assert client.get("/tracker/1/events", params={"track": "second"}).status_code == 404


def test_a_second_run_of_a_kind_returns_the_active_one(
    client: TestClient, manager: RunManager
) -> None:
    for path, body in (
        ("/ops/events/backfill", {}),
        ("/ops/evaluate-prospects", {}),
        ("/ops/scoring/export", None),
    ):
        first = client.post(path, json=body, headers=auth()).json()["id"]
        again = client.post(path, json=body, headers=auth()).json()["id"]
        assert again == first, path
    discover = client.post("/ops/discover", headers=auth()).json()["id"]
    assert client.post("/ops/discover", headers=auth()).json()["id"] == discover
    # The no-option start the Runs panel uses is the same discovery lock, and
    # asks for other options (no configured count), so it is refused rather
    # than started beside it or folded into it.
    plain = client.post("/runs", json={"kind": "discovery"}, headers=auth())
    assert plain.status_code == 409
    assert discover in plain.json()["detail"]
    assert len([run for run in manager.list_runs() if run.kind == "discovery"]) == 1


def test_a_request_with_other_options_is_refused_not_joined(
    client: TestClient, manager: RunManager
) -> None:
    """A dry run, a shadow run or one with uploads made while a real
    discovery runs is refused in plain words naming that run; its uploads
    are removed. The same request again joins (review of PR #208)."""
    from harrier_api.runs import run_inputs_dir

    real = started(manager, client.post("/ops/discover", headers=auth()))
    for form, files in (
        ({"dry_run": "true"}, None),
        ({"shadow": "true"}, None),
        ({}, {"dataset_file": ("a.json", b"[]", "application/json")}),
    ):
        refused = client.post("/ops/discover", data=form, files=files, headers=auth())
        assert refused.status_code == 409, (form, refused.text)
        detail = refused.json()["detail"]
        assert real.id in detail and "other options" in detail
    assert list(run_inputs_dir().glob("*")) == []
    assert client.post("/ops/discover", headers=auth()).json()["id"] == real.id
    assert len(manager.list_runs()) == 1


def test_an_upload_joins_only_a_run_given_the_same_bytes(
    client: TestClient, manager: RunManager
) -> None:
    same = {"dataset_file": ("a.json", b'[{"title": "Invented"}]', "application/json")}
    other = {"dataset_file": ("a.json", b"[]", "application/json")}
    first = started(manager, client.post("/ops/discover", files=same, headers=auth()))
    assert client.post("/ops/discover", files=same, headers=auth()).json()["id"] == first.id
    assert client.post("/ops/discover", files=other, headers=auth()).status_code == 409
    assert len(first.input_paths) == 1 and first.input_paths[0].exists()


@pytest.mark.parametrize(
    ("path", "first", "second"),
    [
        ("/ops/evaluate-prospects", {}, {"apply": True}),
        ("/ops/evaluate-prospects", {}, {"threshold": 0.5}),
        ("/ops/evaluate-prospects", {"apply": True}, {}),
        ("/ops/events/backfill", {}, {"dry_run": False}),
        ("/ops/reconsider", {}, {"apply": True}),
        ("/ops/feeds", None, None),
        ("/mail/watch", {}, {"dry_run": True}),
    ],
    ids=[
        "evaluation report then apply",
        "evaluation with another threshold",
        "evaluation apply then report",
        "backfill count then write",
        "reconsider report then apply",
        "feed check then prune",
        "mail watch then a dry one",
    ],
)
def test_one_run_of_a_kind_at_a_time_and_other_options_are_refused(
    client: TestClient,
    manager: RunManager,
    path: str,
    first: dict[str, object] | None,
    second: dict[str, object] | None,
) -> None:
    """A report and its write never run at once, and a second request with
    other options never shows the first one's numbers (review of PR #208)."""
    started_run = started(manager, client.post(path, json=first, headers=auth()))
    if path == "/ops/feeds":
        response = client.post("/ops/feeds/prune", json={"confirm": True}, headers=auth())
    else:
        response = client.post(path, json=second, headers=auth())
    assert response.status_code == 409, response.text
    assert started_run.id in response.json()["detail"]
    assert client.post(path, json=first, headers=auth()).json()["id"] == started_run.id
    assert len(manager.list_runs()) == 1


def test_every_route_that_starts_a_run_declares_the_refusal(env: Path) -> None:
    spec = create_app().openapi()
    error_out = {"$ref": "#/components/schemas/ErrorOut"}
    starters = {
        ("/runs", "post"),
        ("/apply/{selector}/resume", "post"),
        ("/apply/{selector}/cover-letter", "post"),
        ("/apply/{selector}/answers", "post"),
        ("/apply/{selector}/evaluate", "post"),
        ("/outreach/{selector}/find-contacts", "post"),
        ("/outreach/{selector}/draft", "post"),
        ("/outreach/backfill-posters", "post"),
        ("/mail/watch", "post"),
        ("/ops/feeds", "post"),
        ("/ops/feeds/prune", "post"),
        ("/ops/reconsider", "post"),
        ("/ops/backup", "post"),
        ("/ops/digest", "post"),
        ("/ops/events/backfill", "post"),
        ("/ops/evaluate-prospects", "post"),
        ("/ops/scoring/export", "post"),
        ("/ops/discover", "post"),
    }
    returns_a_run = {
        (path, method)
        for path, methods in spec["paths"].items()
        for method, operation in methods.items()
        if operation["responses"]
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
        == {"$ref": "#/components/schemas/RunOut"}
        and method == "post"
    }
    # The list above is every route that starts a run, so a new one fails here.
    assert returns_a_run - {("/runs/{run_id}/cancel", "post")} == starters
    for path, method in starters:
        declared = spec["paths"][path][method]["responses"]["409"]
        assert declared["content"]["application/json"]["schema"] == error_out, path


def test_a_double_click_with_the_same_words_joins_and_other_words_are_refused(
    env: Path,
) -> None:
    """Spec 047's per-job lock, held to the same rule: tailoring one job
    twice with the same description joins; with another, it is refused,
    and the refused attempt's file is removed."""
    from harrier_api.runs import RunConflictError, RunParams, write_run_input

    manager = RunManager(journal_path=env / "tailor.jsonl")

    async def scenario() -> tuple[str, str, Path, Path]:
        with patch.object(manager, "_execute", side_effect=_idle):
            first = await manager.start(
                "tailor", RunParams(job_id=1, input_path=write_run_input("a description"))
            )
            again_path = write_run_input("a description")
            again = await manager.start("tailor", RunParams(job_id=1, input_path=again_path))
            other_path = write_run_input("another description")
            with pytest.raises(RunConflictError):
                await manager.start("tailor", RunParams(job_id=1, input_path=other_path))
        return first.id, again.id, again_path, other_path

    first_id, again_id, again_path, other_path = asyncio.run(scenario())
    assert again_id == first_id
    assert not again_path.exists() and not other_path.exists()


def test_a_damaged_stored_brief_is_refused_in_the_store_s_words(
    env: Path, client: TestClient
) -> None:
    """Written through the store's own document path, as a brief from before
    a rule changed would be (review of PR #208)."""
    from harrier.apply.brief import APPLICATION_BRIEF_KIND
    from harrier.profile.store import put_document

    job = _job_id()
    conn = connect()
    put_document(conn, APPLICATION_BRIEF_KIND, str(job), "json", "{not json")
    conn.close()
    broken = client.get(f"/apply/{job}/brief", headers=auth())
    assert broken.status_code == 409, broken.text
    assert broken.json()["detail"].startswith(f"stored brief for job {job} is not valid JSON")

    conn = connect()
    put_document(conn, APPLICATION_BRIEF_KIND, str(job), "json", '{"retired_key": 1}')
    conn.close()
    unknown = client.get(f"/apply/{job}/brief", headers=auth())
    assert unknown.status_code == 409
    assert unknown.json()["detail"] == "unknown brief keys: retired_key"


def test_the_brief_saved_is_the_one_the_store_parsed(env: Path, client: TestClient) -> None:
    """The response is the store's parse of what it stored, not a second
    parse of the body, so the two cannot disagree."""
    from harrier.apply.brief import Brief

    job = _job_id()
    parsed = Brief(evidence=("as the store parsed it",))
    with patch("harrier.apply.brief.store_brief", return_value=parsed) as store:
        response = client.put(f"/apply/{job}/brief", json={"evidence": ["typed"]}, headers=auth())
    assert store.call_count == 1
    assert response.json()["evidence"] == ["as the store parsed it"]
