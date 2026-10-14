"""The hosted schedule's rules (spec 108, the scheduler's pull request).

Which slot a job is due at in its owner's timezone, which slots are due
given what the store holds, how a run's exit becomes an outcome, the queue's
order, and what the operator's report calls a problem. These are the
rules; they run on values, with a fixed clock and no database. The process
that applies them is `tests/test_hosted_scheduler.py`.

Owners are random UUIDs and every value is synthetic.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from harrier.hostedschedule import (
    HOSTED_JOBS,
    OUTCOMES,
    UNCLAIMED_AFTER,
    DueSlot,
    JobState,
    OwnerView,
    due_slots,
    instant_text,
    latest_slot,
    load_hosted_jobs,
    outcome_for_exit,
    parse_instant,
    report_lines,
)
from harrier.schedule import CalendarTime, ScheduleJob
from harrier.tracker.schema import SCHEDULE_OUTCOMES

ZONE_FILES_MISSING = "the system has no zone files"


def instant(text: str) -> datetime:
    parsed = parse_instant(text)
    assert parsed is not None
    return parsed


def jobs() -> dict[str, ScheduleJob]:
    """The hosted jobs as the committed `config/schedule.json` defines them."""
    return load_hosted_jobs(Path(__file__).resolve().parents[3] / "config" / "schedule.json")


def new_owner() -> str:
    return str(uuid.uuid4())


def choice(timezone: str, *names: str) -> str:
    return json.dumps({"timezone": timezone, "jobs": list(names)})


# --- the slot ---


def test_the_hosted_job_set_loads_both_jobs() -> None:
    assert tuple(jobs()) == HOSTED_JOBS


# (job, zone, now, the slot) for the committed cadence: discovery at 09:00,
# 13:00, 16:00 and 20:00 local, and academic discovery on Mondays at 09:30.
COMMITTED_SLOTS: tuple[tuple[str, str, str, str], ...] = (
    # An ordinary Monday, 12:30 in Amsterdam (CEST): the 09:00 slot, 07:00Z.
    ("discovery", "Europe/Amsterdam", "2026-10-12T10:30:00Z", "2026-10-12T07:00:00Z"),
    # The instant of a slot is a slot ("at or before now").
    ("discovery", "Europe/Amsterdam", "2026-10-12T07:00:00Z", "2026-10-12T07:00:00Z"),
    # One second before it, the evening before: 20:00 CEST is 18:00Z.
    ("discovery", "Europe/Amsterdam", "2026-10-12T06:59:59Z", "2026-10-11T18:00:00Z"),
    # A weekday job on a Wednesday: Monday's 09:30 CEST.
    ("academic-discovery", "Europe/Amsterdam", "2026-10-14T12:00:00Z", "2026-10-12T07:30:00Z"),
    # Monday in Tokyo is still Sunday in UTC: 09:30 JST is 00:30Z Monday,
    # and at 23:00Z on Sunday it is Monday 08:00 in Tokyo, before the slot.
    ("academic-discovery", "Asia/Tokyo", "2026-10-11T23:00:00Z", "2026-10-05T00:30:00Z"),
    ("academic-discovery", "Asia/Tokyo", "2026-10-12T00:30:00Z", "2026-10-12T00:30:00Z"),
    # Monday in Auckland (NZDT, +13) is Sunday in UTC: 09:30 is 20:30Z.
    ("academic-discovery", "Pacific/Auckland", "2026-10-11T22:00:00Z", "2026-10-11T20:30:00Z"),
    # The day the clocks go forward in Amsterdam, 2026-03-29: 09:00 is CEST,
    # 07:00Z, an hour earlier in UTC than the day before (08:00Z).
    ("discovery", "Europe/Amsterdam", "2026-03-29T08:00:00Z", "2026-03-29T07:00:00Z"),
    ("discovery", "Europe/Amsterdam", "2026-03-28T10:00:00Z", "2026-03-28T08:00:00Z"),
    # The evening before it, 20:00 is still CET: 19:00Z.
    ("discovery", "Europe/Amsterdam", "2026-03-29T05:00:00Z", "2026-03-28T19:00:00Z"),
    # The day the clocks go back, 2026-10-25: 09:00 is CET, 08:00Z; the
    # evening before it was CEST, 18:00Z.
    ("discovery", "Europe/Amsterdam", "2026-10-25T08:00:00Z", "2026-10-25T08:00:00Z"),
    ("discovery", "Europe/Amsterdam", "2026-10-25T07:59:59Z", "2026-10-24T18:00:00Z"),
)


@pytest.mark.parametrize(("name", "timezone", "now", "expected"), COMMITTED_SLOTS)
def test_the_due_slot_is_local_to_the_owner(
    name: str, timezone: str, now: str, expected: str
) -> None:
    """The slot is the latest cadence instant at or before now, read as
    wall-clock time in the owner's zone and converted to UTC (spec 108, "The
    slot"): a weekday job, a day the clocks go forward and a day they go
    back included."""
    from zoneinfo import ZoneInfo

    found = latest_slot(jobs()[name], ZoneInfo(timezone), instant(now))
    assert found is not None
    assert instant_text(found) == expected


def synthetic_job(hour: int, minute: int) -> ScheduleJob:
    return ScheduleJob(
        name="synthetic",
        command=("discover",),
        kind="calendar",
        times=(CalendarTime(hour=hour, minute=minute),),
    )


def test_a_time_that_does_not_exist_or_occurs_twice_is_one_slot() -> None:
    """A local time in the gap is the instant zoneinfo gives for fold=0; one
    that occurs twice is started once, at the fold=0 instant (spec 108, "The
    slot"). The committed cadence has no time near 02:30, so a synthetic job
    makes the two days."""
    from zoneinfo import ZoneInfo

    amsterdam = ZoneInfo("Europe/Amsterdam")
    job = synthetic_job(2, 30)
    # 2026-03-29: 02:00 CET becomes 03:00 CEST, so 02:30 never happens. With
    # fold=0 it is read at the offset before the change, +01:00: 01:30Z.
    gap = latest_slot(job, amsterdam, instant("2026-03-29T12:00:00Z"))
    assert gap is not None and instant_text(gap) == "2026-03-29T01:30:00Z"
    # 2026-10-25: 03:00 CEST becomes 02:00 CET, so 02:30 happens at 00:30Z
    # (CEST) and again at 01:30Z (CET). The slot is the first, and stays the
    # first after the second has passed.
    for now in ("2026-10-25T00:30:00Z", "2026-10-25T01:45:00Z", "2026-10-25T05:00:00Z"):
        twice = latest_slot(job, amsterdam, instant(now))
        assert twice is not None and instant_text(twice) == "2026-10-25T00:30:00Z", now
    # Before the first occurrence, it is the day before's.
    before = latest_slot(job, amsterdam, instant("2026-10-25T00:29:59Z"))
    assert before is not None and instant_text(before) == "2026-10-24T00:30:00Z"


def test_a_job_that_is_not_a_calendar_job_has_no_slot() -> None:
    interval = ScheduleJob(name="watch", command=("gmail-watch",), kind="interval", seconds=300)
    with pytest.raises(ValueError, match="not a calendar job"):
        latest_slot(interval, UTC, datetime(2026, 10, 12, tzinfo=UTC))


# --- which slots are due ---

NOW = instant("2026-10-12T10:00:00Z")  # a Monday


def view(
    owner: str,
    timezone: str = "UTC",
    names: tuple[str, ...] = ("discovery",),
    *,
    updated_at: str = "2026-01-01 00:00:00",
    states: dict[str, JobState] | None = None,
) -> OwnerView:
    return OwnerView(
        owner, choice(timezone, *names), updated_at, states if states is not None else {}
    )


def ok_state(slot: str, **changes: Any) -> JobState:
    """A finished, successful run of `slot`, with `changes` applied."""
    return replace(JobState(slot, slot, slot, "ok", 0, slot), **changes)


def due(*views: OwnerView, now: datetime = NOW) -> list[tuple[str, str]]:
    return [(slot.job, slot.text) for slot in due_slots(views, jobs(), now)]


def test_a_new_choice_starts_at_the_next_slot() -> None:
    """A slot is due only at or after the row's updated_at. The row's time is
    `store_now()`'s, with a space, and the slot's has a `T`: compared as text
    on one day a space sorts before `T`, so a choice made at 15:00 would
    already own the 13:00 slot (spec 108, Amendment (2026-10-13), "Two
    timestamp formats meet in the due rule")."""
    owner = new_owner()
    # Chosen at 15:00 on the day of the 13:00 and 16:00 slots.
    chosen = "2026-10-12 15:00:00"
    # 15:30: the latest slot is 13:00, which came before the choice.
    assert due(view(owner, updated_at=chosen), now=instant("2026-10-12T15:30:00Z")) == []
    # 16:00: the first slot after the choice.
    assert due(view(owner, updated_at=chosen), now=instant("2026-10-12T16:00:00Z")) == [
        ("discovery", "2026-10-12T16:00:00Z")
    ]
    # A choice made at the slot's own instant owns it ("at or after").
    assert due(
        view(owner, updated_at="2026-10-12 16:00:00"), now=instant("2026-10-12T16:00:00Z")
    ) == [("discovery", "2026-10-12T16:00:00Z")]
    # A choice made a second after does not.
    assert (
        due(view(owner, updated_at="2026-10-12 16:00:01"), now=instant("2026-10-12T16:30:00Z"))
        == []
    )
    # The choice of one job leaves the other's alone.
    both = view(owner, names=("discovery", "academic-discovery"), updated_at="2026-10-12 09:15:00")
    assert due(both) == [("academic-discovery", "2026-10-12T09:30:00Z")]


def test_an_outage_starts_only_the_latest_slot() -> None:
    """After a gap of several slots only the latest slot of each job is due,
    so a scheduler outage never multiplies an owner's runs (spec 108, "The
    slot")."""
    owner = new_owner()
    stale = {
        "discovery": ok_state("2026-10-09T20:00:00Z"),
        "academic-discovery": ok_state("2026-09-28T09:30:00Z"),
    }
    names = ("discovery", "academic-discovery")
    # Three days and eleven discovery slots later, and two Mondays on.
    assert due(view(owner, names=names, states=stale)) == [
        ("discovery", "2026-10-12T09:00:00Z"),
        ("academic-discovery", "2026-10-12T09:30:00Z"),
    ]
    # A slot already claimed is not due again.
    claimed = {**stale, "discovery": ok_state("2026-10-12T09:00:00Z")}
    assert due(view(owner, names=names, states=claimed)) == [
        ("academic-discovery", "2026-10-12T09:30:00Z")
    ]


def test_an_owner_with_a_run_in_progress_has_nothing_due() -> None:
    owner = new_owner()
    running = {"discovery": ok_state("2026-10-11T20:00:00Z", last_outcome="running")}
    assert due(view(owner, states=running)) == []


def test_a_paused_deleted_or_invalid_choice_has_nothing_due(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No row, an empty `jobs` and a row that fails validation start
    nothing. The invalid row is logged with the owner's id and the field,
    never the value (spec 108, "A tick")."""
    owner = new_owner()
    secret = "Mars/Secret_Place"
    bad = OwnerView(
        owner, json.dumps({"timezone": secret, "jobs": ["discovery"]}), "2026-01-01 00:00:00"
    )
    unnamed = OwnerView(new_owner())
    paused = view(new_owner(), names=())
    caplog.set_level("WARNING")
    assert due(bad, unnamed, paused) == []
    assert [record.getMessage() for record in caplog.records] == [
        f"scheduled skip owner={owner} reason=schedule.timezone is not a zone this server can load"
    ]
    assert secret not in caplog.text


def test_a_row_that_is_not_json_or_names_an_unloadable_zone_is_skipped(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever reaches the store another way is skipped and logged with the
    owner's id and what is wrong, not the value (spec 108, "A tick")."""
    from zoneinfo import ZoneInfoNotFoundError

    import harrier.hostedschedule as module

    not_json, unloadable = new_owner(), new_owner()
    secret = "owner wrote this {"
    caplog.set_level("WARNING")
    assert due(OwnerView(not_json, secret, "2026-01-01 00:00:00")) == []

    # The zone loaded when the row was validated and does not now, as when
    # the image's zone files are replaced under a running scheduler.
    def gone(_key: str) -> object:
        raise ZoneInfoNotFoundError("No time zone found")

    monkeypatch.setattr(module, "ZoneInfo", gone)
    assert due(view(unloadable)) == []
    assert [record.getMessage() for record in caplog.records] == [
        f"scheduled skip owner={not_json} reason=schedule is not valid JSON",
        f"scheduled skip owner={unloadable} reason=schedule.timezone is not a zone this "
        "server can load",
    ]
    assert secret not in caplog.text


def test_a_value_the_parser_cannot_hold_is_an_invalid_row() -> None:
    """Nested past the parser's depth, or with a number past its digit limit,
    JSON raises RecursionError or ValueError, not a decode error: each is an
    invalid choice, so one owner's row cannot stop the tick (local review of
    PR 327)."""
    from harrier.hostedschedule import read_choice
    from harrier.userconfig.store import ConfigError

    for text in ("[" * 100_000, '{"timezone": ' + "9" * 5_000 + "}"):
        with pytest.raises(ConfigError, match="schedule is not valid JSON"):
            read_choice(text)


def test_a_row_nothing_foresaw_is_skipped_and_reported(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every per-owner step catches every exception (spec 108,
    "Isolation"): an owner whose row raises something the code did not
    foresee starts nothing and appears in the report, and the next owner is
    served."""
    import harrier.hostedschedule as module

    broken, fine = sorted((new_owner(), new_owner()))
    original = module.read_choice

    def read(text: str) -> object:
        if json.loads(text)["jobs"] == ["academic-discovery"]:
            raise RuntimeError("the secret text of the row")
        return original(text)

    monkeypatch.setattr(module, "read_choice", read)
    views = [
        OwnerView(broken, choice("UTC", "academic-discovery"), "2026-01-01 00:00:00"),
        view(fine),
    ]
    caplog.set_level("WARNING")
    assert [(s.owner, s.job) for s in due_slots(views, jobs(), NOW)] == [(fine, "discovery")]
    lines = report_lines(views, jobs(), REPORT_NOW)
    assert [line.owner for line in lines] == [broken, fine]
    assert lines[0].problem == ""
    assert lines[0].text == f"{broken} - schedule could not be read: RuntimeError"
    assert [r.getMessage() for r in caplog.records] == [
        f"scheduled skip owner={broken} reason=schedule could not be read: RuntimeError"
    ]
    assert "secret" not in caplog.text + lines[0].text


def test_an_unparseable_choice_time_starts_nothing() -> None:
    """A `schedule_updated_at` that is not a time cannot gate a slot, so the
    owner is skipped and logged, not started on every slot (local review of
    PR 327)."""
    owner = new_owner()
    assert due(view(owner, updated_at="garbage")) == []


def test_a_hosted_job_the_committed_file_cannot_run_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hosted job the committed file does not name, or names as an
    interval, is skipped with a log line and the others run (spec 108, "The
    hosted job set")."""
    config = tmp_path / "schedule.json"
    config.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "discovery",
                        "command": ["discover", "--scheduled"],
                        "trigger": {"kind": "calendar", "times": [{"hour": 9, "minute": 0}]},
                    },
                    {
                        "name": "academic-discovery",
                        "command": ["discover"],
                        "trigger": {"kind": "interval", "seconds": 60},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    caplog.set_level("WARNING")
    assert list(load_hosted_jobs(config)) == ["discovery"]
    assert [record.getMessage() for record in caplog.records] == [
        "scheduled skip job=academic-discovery reason=not a calendar job in the schedule"
    ]
    config.write_text(config.read_text(encoding="utf-8").replace("academic-", "other-"))
    caplog.clear()
    assert list(load_hosted_jobs(config)) == ["discovery"]
    assert len(caplog.records) == 1


def test_the_queue_is_ordered_by_slot_then_never_started_then_owner() -> None:
    """Slot time, then never-started first, then the earlier last start,
    then owner id (spec 108, "A tick", step 3)."""
    low, middle, high = sorted(new_owner() for _ in range(3))
    early, late = instant("2026-10-12T09:00:00Z"), instant("2026-10-12T13:00:00Z")

    def slot(owner: str, at: datetime, started: str | None) -> DueSlot:
        return DueSlot(owner, "discovery", at, "UTC", started)

    entries = [
        slot(high, late, None),
        slot(low, late, "2026-10-11T09:00:00Z"),
        slot(middle, late, "2026-10-10T09:00:00Z"),
        slot(high, early, "2026-10-11T09:00:00Z"),
        slot(low, late, None),
    ]
    ordered = sorted(entries, key=DueSlot.order)
    assert [(e.at, e.last_started_at, e.owner) for e in ordered] == [
        (early, "2026-10-11T09:00:00Z", high),
        (late, None, low),
        (late, None, high),
        (late, "2026-10-10T09:00:00Z", middle),
        (late, "2026-10-11T09:00:00Z", low),
    ]


# --- outcomes ---


def test_a_runs_exit_becomes_its_outcome() -> None:
    """0 is ok, 3 is spec 029's total failure, anything else is crashed, a
    signal's negative status included (spec 108, "Finishing a run")."""
    assert outcome_for_exit(0) == ("ok", 0)
    assert outcome_for_exit(3) == ("failed", 3)
    assert outcome_for_exit(1) == ("crashed", 1)
    assert outcome_for_exit(2) == ("crashed", 2)
    assert outcome_for_exit(-9) == ("crashed", -9)


def test_the_outcome_classes_are_the_stores() -> None:
    assert OUTCOMES == SCHEDULE_OUTCOMES


# --- the report ---

REPORT_NOW = instant("2026-10-14T12:00:00Z")  # a Wednesday


def report(*views: OwnerView, now: datetime = REPORT_NOW) -> list[tuple[str, str]]:
    return [(line.job, line.problem) for line in report_lines(views, jobs(), now)]


def healthy(last_slot: str = "2026-10-14T09:00:00Z", **changes: Any) -> dict[str, JobState]:
    return {"discovery": ok_state(last_slot, **changes)}


def test_a_healthy_owner_is_not_a_problem() -> None:
    assert report(view(new_owner(), states=healthy())) == [("discovery", "")]


@pytest.mark.parametrize("outcome", ["failed", "crashed", "timed_out", "refused"])
def test_a_failing_last_outcome_is_a_problem(outcome: str) -> None:
    states = healthy(last_outcome=outcome, last_exit=None)
    assert report(view(new_owner(), states=states)) == [("discovery", f"last run {outcome}")]


@pytest.mark.parametrize("outcome", ["not_configured", "interrupted", "missed", "running"])
def test_the_other_outcomes_are_shown_and_are_not_problems(outcome: str) -> None:
    states = healthy(last_outcome=outcome, last_exit=None)
    assert report(view(new_owner(), states=states)) == [("discovery", "")]


def test_a_job_is_overdue_after_twice_its_longest_gap() -> None:
    """Discovery's longest gap is 13 hours (20:00 to 09:00), so its last ok
    is overdue after 26 hours; academic discovery's is a week, so after 14
    days (spec 029's rule, spec 108, "The operator's report")."""
    owner = new_owner()
    # 25 hours ago: not yet. 27 hours ago: overdue. The slot itself was
    # claimed, so this is the overdue rule alone.
    recent = healthy(last_ok_at="2026-10-13T11:00:00Z")
    assert report(view(owner, states=recent)) == [("discovery", "")]
    stale = healthy(last_ok_at="2026-10-13T09:00:00Z")
    [(_, problem)] = report(view(owner, states=stale))
    assert problem == "overdue: last ok 1d3h ago"
    # Academic: 13 days ago is fine, 15 days ago is overdue.
    weekly = {
        "academic-discovery": ok_state("2026-10-12T09:30:00Z", last_ok_at="2026-10-01T09:30:00Z")
    }
    assert report(view(owner, names=("academic-discovery",), states=weekly)) == [
        ("academic-discovery", "")
    ]
    weekly["academic-discovery"] = ok_state(
        "2026-10-12T09:30:00Z", last_ok_at="2026-09-29T09:30:00Z"
    )
    [(_, weekly_problem)] = report(view(owner, names=("academic-discovery",), states=weekly))
    assert weekly_problem == "overdue: last ok 15d2h ago"


def test_the_overdue_limit_is_exclusive() -> None:
    """Twice the longest gap, 26 hours for discovery: a last ok exactly that
    old is not overdue, a second older is."""
    owner = new_owner()
    exactly = healthy(last_ok_at="2026-10-13T10:00:00Z")
    assert report(view(owner, states=exactly)) == [("discovery", "")]
    older = healthy(last_ok_at="2026-10-13T09:59:59Z")
    assert report(view(owner, states=older))[0][1].startswith("overdue: last ok")


def test_a_slot_at_the_instant_of_the_choice_can_be_unclaimed() -> None:
    """A choice made at the slot's own instant owns that slot, so left
    unclaimed it is a problem like any other."""
    owner = new_owner()
    chosen = view(owner, updated_at="2026-10-14 09:00:00")
    [(_, problem)] = report(chosen, now=instant("2026-10-14T12:30:00Z"))
    assert problem == "a whole slot passed unclaimed: 2026-10-14T09:00:00Z"


def test_a_job_never_ok_is_overdue_once_chosen_long_enough_ago() -> None:
    never = healthy(last_ok_at=None, last_outcome="not_configured", last_exit=None)
    # Chosen 25 hours ago: not yet; 27 hours ago: overdue.
    assert report(view(new_owner(), updated_at="2026-10-13 11:00:00", states=never)) == [
        ("discovery", "")
    ]
    [(_, problem)] = report(view(new_owner(), updated_at="2026-10-13 09:00:00", states=never))
    assert problem == "overdue: never ok since chosen 1d3h ago"
    # No state at all, chosen long ago: the same.
    [(_, nothing)] = report(view(new_owner(), updated_at="2026-10-01 00:00:00"))
    assert nothing.startswith("overdue: never ok since chosen")


def test_a_slot_unclaimed_for_hours_is_a_problem() -> None:
    """The latest slot is later than `last_slot_at` and passed more than
    `UNCLAIMED_AFTER` ago: the scheduler is not keeping up or not running."""
    assert timedelta(hours=3) == UNCLAIMED_AFTER
    owner = new_owner()
    # At 12:00 the 09:00 slot is exactly three hours old: still within it.
    claimed_before = healthy("2026-10-13T20:00:00Z", last_ok_at="2026-10-13T20:05:00Z")
    assert report(view(owner, states=claimed_before)) == [("discovery", "")]
    # A second past it, it is a problem.
    [(_, problem)] = report(view(owner, states=claimed_before), now=instant("2026-10-14T12:00:01Z"))
    assert problem == "a whole slot passed unclaimed: 2026-10-14T09:00:00Z"
    # A claimed slot is not unclaimed, however old.
    assert report(view(owner, states=healthy("2026-10-14T09:00:00Z"))) == [("discovery", "")]
    # And a slot before the owner's choice was never theirs.
    chosen_after = view(owner, updated_at="2026-10-14 09:30:00", states=claimed_before)
    assert report(chosen_after, now=instant("2026-10-14T12:30:00Z")) == [("discovery", "")]


def test_only_a_chosen_job_can_be_a_problem() -> None:
    """An owner who stopped a job is not reported for its last failure."""
    states = healthy(last_outcome="failed", last_exit=3)
    stopped = view(new_owner(), names=(), states=states)
    lines = report_lines([stopped], jobs(), REPORT_NOW)
    assert [(line.job, line.problem) for line in lines] == [("discovery", "")]
    assert lines[0].text.endswith("(not chosen)")


def test_an_invalid_choice_is_shown_and_its_jobs_still_go_overdue() -> None:
    """A row that reaches the store another way is skipped by the scheduler;
    the jobs its owner has state for become overdue (spec 108, Failure
    modes). The field is named, the value is not."""
    secret = "Mars/Secret_Place"
    bad = json.dumps({"timezone": secret, "jobs": ["discovery"]})
    owner = new_owner()
    stale = healthy(last_ok_at="2026-10-01T09:00:00Z")
    lines = report_lines([OwnerView(owner, bad, "2026-09-01 00:00:00", stale)], jobs(), REPORT_NOW)
    assert [(line.job, line.problem.split(":")[0]) for line in lines] == [("discovery", "overdue")]
    assert "schedule.timezone is not a zone this server can load" in lines[0].text
    # No state: one line naming the field, and no problem to report yet.
    empty = report_lines([OwnerView(owner, bad, "2026-09-01 00:00:00")], jobs(), REPORT_NOW)
    assert [(line.job, line.problem) for line in empty] == [("-", "")]
    assert secret not in "".join(line.text for line in (*lines, *empty))


def test_a_report_line_names_the_owner_job_cadence_and_state() -> None:
    owner = new_owner()
    [line] = report_lines([view(owner, states=healthy())], jobs(), REPORT_NOW)
    assert line.text == (
        f"{owner} discovery cadence='daily at 09:00, 13:00, 16:00, 20:00' "
        "slot=2026-10-14T09:00:00Z outcome=ok exit=0 ok=2026-10-14T09:00:00Z"
    )
