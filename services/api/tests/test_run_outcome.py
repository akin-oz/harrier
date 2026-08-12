"""A failed run does not look like a quiet one (spec 029).

The property under test is the one whose absence caused the outage this
project was built in reaction to: a scheduled job that stops working must
say so. Three mechanisms, tested separately because each fails alone.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.notify import build_telegram_message
from harrier.runoutcome import (
    DIGEST_JOB,
    DISCOVERY_JOB,
    EXIT_OK,
    EXIT_RUN_FAILED,
    RunOutcome,
    all_last_success,
    classify_run,
    describe_age,
    last_success,
    record_success,
    source_failed,
)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return connect()


def ok_source(name: str) -> dict[str, object]:
    return {"source": name, "fetched_count": 3, "new_prospects": 1}


def failed_source(name: str) -> dict[str, object]:
    return {"source": name, "fetched_count": 0, "new_prospects": 0, "errors": ["boom"]}


def aggregate(
    summaries: list[dict[str, object]], skipped: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {"source_summaries": summaries, "skipped_sources": skipped or []}


# --- the exit-status table, one case per row --------------------------------


def test_a_run_that_found_things_exits_zero() -> None:
    outcome = classify_run(aggregate([ok_source("greenhouse")]))
    assert outcome.exit_code == EXIT_OK


def test_a_partial_failure_exits_zero() -> None:
    """The row that matters most. A day where one source is down and the
    others are fine is a normal day, and an exit status that fails a run the
    operator would call fine is one they learn to ignore."""
    outcome = classify_run(aggregate([ok_source("greenhouse"), failed_source("lever")]))
    assert outcome.exit_code == EXIT_OK
    assert outcome.failed == ("lever",)
    assert "failed: lever" in outcome.describe()


def test_a_run_where_every_source_failed_exits_non_zero() -> None:
    outcome = classify_run(aggregate([failed_source("greenhouse"), failed_source("lever")]))
    assert outcome.exit_code == EXIT_RUN_FAILED
    assert outcome.total_failure


def test_a_run_that_attempted_nothing_exits_non_zero() -> None:
    """The founding outage's shape: a job that runs every four hours and
    tries nothing looks identical to a quiet week in every other signal."""
    outcome = classify_run(aggregate([]))
    assert outcome.exit_code == EXIT_RUN_FAILED
    assert "no source was attempted" in outcome.describe()


def test_a_run_where_everything_was_skipped_exits_non_zero() -> None:
    outcome = classify_run(aggregate([], [{"source": "apify_linkedin", "reason": "cost gate"}]))
    assert outcome.exit_code == EXIT_RUN_FAILED
    assert outcome.skipped == ("apify_linkedin",)
    assert "apify_linkedin" in outcome.describe()


def test_a_skip_is_neither_an_attempt_nor_a_failure() -> None:
    """A deliberately skipped paid source must not fail the run, and must not
    silently count as a success either."""
    outcome = classify_run(
        aggregate([ok_source("greenhouse")], [{"source": "apify_linkedin", "reason": "cost gate"}])
    )
    assert outcome.attempted == ("greenhouse",)
    assert outcome.failed == ()
    assert outcome.skipped == ("apify_linkedin",)
    assert outcome.exit_code == EXIT_OK


# --- what counts as a failed source -----------------------------------------


def test_a_source_that_raised_is_failed() -> None:
    assert source_failed({"source": "remoteok", "errors": ["connection reset"]})


def test_a_source_whose_every_board_errored_is_failed() -> None:
    assert source_failed(
        {
            "source": "greenhouse",
            "board_urls": ["a", "b"],
            "board_errors": ["a: 404", "b: 404"],
        }
    )


def test_one_dead_board_among_several_is_not_a_failed_source() -> None:
    """A dead board is spec 025's problem. Treating it as a failed run would
    make the exit status fire on a watchlist that needs pruning rather than
    on a system that is broken."""
    assert not source_failed(
        {"source": "greenhouse", "board_urls": ["a", "b", "c"], "board_errors": ["a: 404"]}
    )


def test_a_source_that_found_nothing_is_not_failed() -> None:
    """Zero results is a quiet day. Only an inability to look is a failure."""
    assert not source_failed({"source": "lever", "fetched_count": 0, "new_prospects": 0})


# --- the notification -------------------------------------------------------


def test_the_notification_is_sent_when_nothing_was_found() -> None:
    """The gate this replaces meant the run that found nothing was the run
    that said nothing, which is the shape both a quiet week and a total
    outage take."""
    message = build_telegram_message([], outcome=RunOutcome(("greenhouse",), (), ()))
    assert "0 new prospects" in message
    assert "Discovery ok" in message


def test_a_total_failure_says_so_first() -> None:
    message = build_telegram_message([], outcome=RunOutcome(("a", "b"), ("a", "b"), ()))
    assert message.splitlines()[0].startswith("DISCOVERY FAILED")


def test_a_partial_failure_names_the_failed_source() -> None:
    message = build_telegram_message([], outcome=RunOutcome(("a", "b"), ("b",), ()))
    assert "failed: b" in message


def test_the_message_without_an_outcome_is_unchanged() -> None:
    """Callers that predate spec 029 keep working."""
    assert build_telegram_message([]).startswith("Job imports: 0 new prospects")


# --- last success -----------------------------------------------------------


def test_a_success_is_recorded_and_read_back(db: sqlite3.Connection) -> None:
    record_success(db, DISCOVERY_JOB, at="2026-08-01T09:00:00Z")
    assert last_success(db, DISCOVERY_JOB) == "2026-08-01T09:00:00Z"


def test_a_job_that_has_never_succeeded_has_no_row(db: sqlite3.Connection) -> None:
    assert last_success(db, DISCOVERY_JOB) is None


def test_recording_again_replaces_the_previous_value(db: sqlite3.Connection) -> None:
    record_success(db, DISCOVERY_JOB, at="2026-08-01T09:00:00Z")
    record_success(db, DISCOVERY_JOB, at="2026-08-02T09:00:00Z")
    assert last_success(db, DISCOVERY_JOB) == "2026-08-02T09:00:00Z"
    assert len(all_last_success(db)) == 1


def test_each_job_keeps_its_own_timestamp(db: sqlite3.Connection) -> None:
    record_success(db, DISCOVERY_JOB, at="2026-08-01T09:00:00Z")
    record_success(db, DIGEST_JOB, at="2026-08-08T20:30:00Z")
    assert all_last_success(db) == {
        DISCOVERY_JOB: "2026-08-01T09:00:00Z",
        DIGEST_JOB: "2026-08-08T20:30:00Z",
    }


# --- how the age reads ------------------------------------------------------


def test_an_old_success_is_reported_in_days() -> None:
    now = datetime(2026, 8, 11, tzinfo=UTC)
    line = describe_age(DISCOVERY_JOB, (now - timedelta(days=61)).isoformat(), now=now)
    assert line == "discovery: last succeeded 61 days ago"


def test_a_job_that_never_succeeded_says_so() -> None:
    """Not "0 days ago", which reads as healthy."""
    assert "never recorded a success" in describe_age(DISCOVERY_JOB, None)


def test_an_unreadable_timestamp_is_not_reported_as_today() -> None:
    """0 reads as "succeeded today", so a corrupt value must never produce
    it: a broken clock must not look like a working job."""
    line = describe_age(DISCOVERY_JOB, "not-a-time")
    assert "unreadable" in line
    assert "today" not in line


def test_a_success_today_reads_as_today() -> None:
    now = datetime(2026, 8, 11, 12, tzinfo=UTC)
    assert "today" in describe_age(DISCOVERY_JOB, now.isoformat(), now=now)


# --- the digest reports the schedule ----------------------------------------


def test_the_digest_leads_with_every_scheduled_job(db: sqlite3.Connection) -> None:
    """Every job is named even when it has no row, because a job missing from
    the list is exactly the job nobody notices has stopped."""
    from harrier.digest import SCHEDULED_JOBS, schedule_health_lines

    lines = schedule_health_lines(db)
    assert len(lines) == len(SCHEDULED_JOBS)
    for job in SCHEDULED_JOBS:
        assert any(line.startswith(f"{job}:") for line in lines)


def test_a_two_month_outage_is_legible_in_the_digest(db: sqlite3.Connection) -> None:
    """The whole point. `New prospects today: 0` reads the same on a quiet
    Tuesday and in the second month of a silent failure."""
    from harrier.digest import build_digest

    stale = (datetime.now(UTC) - timedelta(days=61)).isoformat()
    record_success(db, DISCOVERY_JOB, at=stale)
    digest = build_digest(db, datetime.now(UTC).date())
    assert "discovery: last succeeded 61 days ago" in digest
    assert digest.index("last succeeded") < digest.index("New prospects today")


def test_a_job_with_no_history_is_named_in_the_digest(db: sqlite3.Connection) -> None:
    from harrier.digest import build_digest

    assert "has never recorded a success" in build_digest(db, datetime.now(UTC).date())


# --- logging ----------------------------------------------------------------


def test_logging_emits_a_level_and_a_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this, every logger.info was discarded and every warning went
    out bare through the last-resort handler."""
    import logging

    from harrier.logsetup import configure_logging, log_path

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    configure_logging(force=True)
    logging.getLogger("harrier.test").warning("a decision that changed the run")

    for handler in logging.getLogger().handlers:
        handler.flush()
    written = log_path().read_text(encoding="utf-8")
    assert "WARNING" in written
    assert "harrier.test" in written
    assert "a decision that changed the run" in written


def test_an_info_line_is_emitted_rather_than_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from harrier.logsetup import configure_logging, log_path

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    configure_logging(force=True)
    logging.getLogger("harrier.test").info("apify skipped")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "apify skipped" in log_path().read_text(encoding="utf-8")


def test_configure_logging_twice_does_not_double_the_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second handler is how one line becomes two and a log reads as two
    runs."""
    import logging

    from harrier.logsetup import configure_logging

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    configure_logging(force=True)
    before = len(logging.getLogger().handlers)
    configure_logging(force=True)
    assert len(logging.getLogger().handlers) == before


# --- the command's exit status ----------------------------------------------


def run_discover(monkeypatch: pytest.MonkeyPatch, aggregate_value: dict[str, object]) -> int:
    """Run the command with discovery replaced by a fixed aggregate."""
    from harrier import discovery as discovery_module
    from harrier_cli.main import main

    def fake_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        return aggregate_value

    monkeypatch.setattr(discovery_module, "run_discovery", fake_run)
    return main(["discover", "--dry-run", "--no-notify"])


def test_the_command_exits_non_zero_when_every_source_failed(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_discover(
        monkeypatch, aggregate([failed_source("greenhouse"), failed_source("lever")])
    )
    assert code == EXIT_RUN_FAILED
    assert "discovery failed" in capsys.readouterr().err


def test_the_command_exits_zero_on_a_partial_failure(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reported on stderr, but not fatal: this is a normal day."""
    code = run_discover(monkeypatch, aggregate([ok_source("greenhouse"), failed_source("lever")]))
    assert code == EXIT_OK
    assert "failed: lever" in capsys.readouterr().err


def test_the_command_exits_non_zero_when_nothing_was_attempted(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_discover(monkeypatch, aggregate([])) == EXIT_RUN_FAILED


def test_the_command_exits_zero_on_a_healthy_run(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_discover(monkeypatch, aggregate([ok_source("greenhouse")])) == EXIT_OK
