"""State that survives a crash, and a status that can say no (spec 040).

The mail watch is scheduled on a fixed interval and launchd will not start a
second instance of a live label, so one poll that never returns stops the
watch forever while the scheduler still reports it running. Spec 029 made a
failed run visible; this makes a hung run into a failed one, and neither is
sufficient alone.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harrier.atomicio import DamagedStateError, read_json_mapping, write_json_atomic
from harrier.mail.watch import (
    ARCHIVED_FIELDS,
    EVENTS_MAX_LINES,
    append_event,
    events_path,
    load_state,
    redact_event,
    save_state,
    state_path,
)
from harrier.schedule import JobStatus


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


def an_event() -> dict[str, object]:
    return {
        "kind": "interview_invite",
        "priority": "high",
        "company": "Example Co",
        "role": "Senior Frontend Engineer",
        "tracker_row": "12",
        "next_action": "Reply with availability.",
        "summary": "We would love to speak with you about the role next Tuesday.",
        "from": "Dana Recruiter <dana@exampleco.com>",
        "timestamp": "2026-08-10T09:00:00Z",
        "messageId": "abc123",
        "subject": "Interview with Example Co",
        "actionable": True,
    }


# --- atomic writes ----------------------------------------------------------


def test_a_state_file_round_trips(env: Path) -> None:
    save_state({"seen_message_ids": ["a", "b"]})
    assert load_state() == {"seen_message_ids": ["a", "b"]}


def test_no_temporary_file_is_left_behind(env: Path) -> None:
    save_state({"seen_message_ids": ["a"]})
    leftovers = [path.name for path in state_path().parent.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []


def test_a_failed_write_leaves_the_previous_state_intact(env: Path, tmp_path: Path) -> None:
    """The property the rename buys. A crash mid-write leaves either the old
    file or the new one, never half of either."""
    save_state({"seen_message_ids": ["original"]})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        write_json_atomic(state_path(), {"seen_message_ids": [Unserializable()]})

    assert load_state() == {"seen_message_ids": ["original"]}
    leftovers = [path.name for path in state_path().parent.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == [], "a failed write left its temporary file behind"


# --- damage is reported, not read as empty ----------------------------------


def test_an_absent_state_file_is_not_damage(env: Path) -> None:
    """A first run has no file, and that is ordinary."""
    assert read_json_mapping(state_path()) is None
    assert load_state() == {"seen_message_ids": []}


def test_a_truncated_state_file_is_refused(env: Path) -> None:
    """Reading it as empty means every message looks unprocessed, and the
    save that follows overwrites the damage so the original is gone."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"seen_message_ids": ["a", "b"', encoding="utf-8")
    with pytest.raises(DamagedStateError):
        load_state()


def test_an_empty_state_file_is_refused(env: Path) -> None:
    """Zero bytes is the exact shape of a crash between truncate and flush,
    and json would report it the same way as any other parse failure."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    with pytest.raises(DamagedStateError, match="truncated"):
        load_state()


def test_a_state_file_of_the_wrong_shape_is_refused(env: Path) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(DamagedStateError, match="expected an object"):
        load_state()


# --- the events archive holds no third party's words ------------------------


def test_the_archive_keeps_no_subject_or_body(env: Path) -> None:
    """It was an unbounded record of other people's correspondence: sender,
    subject, and the first sentence of the body, kept forever."""
    append_event(an_event())
    written = json.loads(events_path().read_text(encoding="utf-8").strip())
    assert "subject" not in written
    assert "summary" not in written
    assert "from" not in written


def test_the_archive_keeps_the_sender_domain_only(env: Path) -> None:
    """Enough to recognise which employer an event came from, without
    recording who wrote it."""
    append_event(an_event())
    written = json.loads(events_path().read_text(encoding="utf-8").strip())
    assert written["from_domain"] == "exampleco.com"
    assert "dana" not in json.dumps(written)


def test_the_archive_keeps_what_the_feature_needs(env: Path) -> None:
    append_event(an_event())
    written = json.loads(events_path().read_text(encoding="utf-8").strip())
    for field in ("kind", "priority", "company", "role", "tracker_row", "next_action"):
        assert field in written


def test_redaction_leaves_the_in_memory_event_alone() -> None:
    """The notification and the digest still say what they always said. Only
    what is written down is reduced."""
    event = an_event()
    redact_event(event)
    assert event["subject"] == "Interview with Example Co"
    assert str(event["summary"]).startswith("We would love")


def test_the_archived_fields_are_the_only_fields(env: Path) -> None:
    append_event(an_event())
    written = json.loads(events_path().read_text(encoding="utf-8").strip())
    unexpected = set(written) - set(ARCHIVED_FIELDS) - {"from_domain"}
    assert not unexpected, f"the archive gained fields nobody reviewed: {unexpected}"


def test_the_archive_is_bounded(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unattended machine must not accumulate years of it."""
    monkeypatch.setattr("harrier.mail.watch.EVENTS_MAX_LINES", 5)
    for index in range(9):
        event = an_event()
        event["messageId"] = f"m{index}"
        append_event(event)
    lines = events_path().read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[-1])["messageId"] == "m8", "rotation dropped the newest, not the oldest"


def test_the_bound_is_a_real_number() -> None:
    assert EVENTS_MAX_LINES > 0


# --- the status command can say no ------------------------------------------


def test_a_healthy_job_reports_no_problem() -> None:
    status = JobStatus(name="discovery", label="l", installed=True, loaded=True)
    assert status.problem == ""


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (JobStatus(name="d", label="l", installed=False), "not installed"),
        (JobStatus(name="d", label="l", installed=True, loaded=False), "not loaded"),
        (JobStatus(name="d", label="l", installed=True, loaded=True, drifted=True), "drifted"),
        (
            JobStatus(name="d", label="l", installed=True, loaded=True, last_exit_status="1"),
            "exited 1",
        ),
    ],
)
def test_an_unhealthy_job_reports_why(status: JobStatus, expected: str) -> None:
    """The command printed its table and returned zero regardless of what the
    table said, so nothing could be scripted against it."""
    assert expected in status.problem


def test_a_zero_exit_status_is_not_a_problem() -> None:
    """A job that ran and succeeded reports its exit status too."""
    status = JobStatus(name="d", label="l", installed=True, loaded=True, last_exit_status="0")
    assert status.problem == ""


def test_the_problem_shows_in_the_printed_line() -> None:
    status = JobStatus(name="discovery", label="l", installed=True, loaded=False)
    assert "PROBLEM" in status.line()
