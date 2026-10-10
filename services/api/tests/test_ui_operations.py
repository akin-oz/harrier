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
