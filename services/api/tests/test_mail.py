"""Behavior pins for the Gmail watch (spec 018), ported from the old
repo's tests/test_gmail_watch.py onto the database and the library run."""

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from harrier.db import connect
from harrier.mail import (
    GmailMessage,
    classify_message,
    events_path,
    format_telegram_message,
    normalize_gmail_api_message,
    run_watch,
    validate_env,
)
from harrier.tracker import add_job


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return connect()


def build_message(
    subject: str,
    body: str,
    sender: str = "Recruiter <jobs@reedsy.com>",
    message_id: str = "<abc@example.com>",
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        subject=subject,
        sender=sender,
        sender_email=sender.split("<")[-1].rstrip(">") if "<" in sender else sender,
        timestamp="2026-03-20T12:00:00+00:00",
        body_plain=body,
        snippet=body[:120],
    )


REEDSY_ROW = {
    "company": "reedsy",
    "title": "Senior Software Engineer (Node/Vue/TypeScript) - Remote Europe",
    "id": "7",
}


# ---------------------------------------------------------------------------
# Classification cascade
# ---------------------------------------------------------------------------


def test_interview_invite_classification_with_tracker_match() -> None:
    event = classify_message(
        build_message(
            "Interview invitation for Senior Software Engineer",
            "We would like to invite you to interview next week.",
        ),
        [dict(REEDSY_ROW)],
    )
    assert event["kind"] == "interview_invite"
    assert event["tracker_row"] == "7"
    assert event["priority"] == "high"
    assert event["actionable"] is True


def test_rejection_classification() -> None:
    event = classify_message(
        build_message(
            "Application update", "We regret to inform you that we will not be moving forward."
        ),
        [],
    )
    assert event["kind"] == "rejection"


def test_marketing_message_is_ignored_with_reason() -> None:
    event = classify_message(
        build_message("Daily jobs for you", "Unsubscribe and manage preferences here."), []
    )
    assert event["kind"] == "ignored"
    assert event["ignore_reason"] == "marketing_or_security_email"


def test_follow_up_beats_application_confirmation() -> None:
    event = classify_message(
        build_message(
            "Follow-up on your application",
            "Thanks for applying. We'd like to move forward and share next steps.",
        ),
        [],
    )
    assert event["kind"] == "recruiter_reply"


def test_thanks_for_applying_is_application_confirmation() -> None:
    event = classify_message(
        build_message(
            "Thanks for applying to Reedsy!",
            "Thanks for applying to Reedsy. We received your application and will review it.",
        ),
        [dict(REEDSY_ROW)],
    )
    assert event["kind"] == "application_confirmation"
    assert event["actionable"] is True
    assert event["priority"] == "low"
    assert event["tracker_row"] == "7"


def test_thanks_for_your_interest_is_application_confirmation() -> None:
    event = classify_message(
        build_message(
            "Thanks for your interest in ExampleCo",
            "Thanks for your interest. We received your application and will be in touch.",
            sender="ExampleCo Careers <careers@example.co>",
        ),
        [],
    )
    assert event["kind"] == "application_confirmation"
    assert event["actionable"] is True


def test_security_alert_stays_ignored() -> None:
    event = classify_message(
        build_message(
            "Google security alert",
            "A new sign-in was detected on your account.",
            sender="Google <no-reply@accounts.google.com>",
        ),
        [],
    )
    assert event["kind"] == "ignored"
    assert event["ignore_reason"] == "marketing_or_security_email"


# ---------------------------------------------------------------------------
# Telegram formats
# ---------------------------------------------------------------------------

ASSESSMENT_EVENT: dict[str, object] = {
    "priority": "high",
    "kind": "assessment",
    "company": "Reedsy",
    "role": "Senior Software Engineer",
    "tracker_row": "7",
    "next_action": "Review the assessment, confirm deadline, and plan completion time.",
    "summary": "Coding assessment with a one-week deadline.",
    "from": "Recruiter <jobs@reedsy.com>",
    "timestamp": "2026-03-20T12:00:00+00:00",
}


def test_telegram_message_is_compact_and_readable() -> None:
    message = format_telegram_message(dict(ASSESSMENT_EVENT))
    assert "Priority: high • assessment" in message
    assert "Company/Role: Reedsy — Senior Software Engineer" in message
    assert "Next action:" in message
    assert "Summary:" in message
    assert "From/time:" in message


def test_application_confirmation_telegram_message_is_low_noise() -> None:
    event = {**ASSESSMENT_EVENT, "kind": "application_confirmation", "priority": "low"}
    message = format_telegram_message(event)
    assert "🟡 Application confirmed" in message
    assert "Next action:" not in message
    assert "Summary:" in message


# ---------------------------------------------------------------------------
# API normalization
# ---------------------------------------------------------------------------


def test_normalize_gmail_api_message_extracts_headers_and_body() -> None:
    raw: dict[str, Any] = {
        "id": "18c123",
        "snippet": "We would like to invite you to interview next week.",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Interview invitation"},
                {"name": "From", "value": "Recruiter <jobs@reedsy.com>"},
                {"name": "Date", "value": "Fri, 20 Mar 2026 12:00:00 +0000"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": (
                            "V2Ugd291bGQgbGlrZSB0byBpbnZpdGUgeW91IHRvIGludGVydmlldyBuZXh0IHdlZWsu"
                        )
                    },
                }
            ],
        },
    }
    message = normalize_gmail_api_message(raw)
    assert message.message_id == "18c123"
    assert message.subject == "Interview invitation"
    assert message.sender_email == "jobs@reedsy.com"
    assert "invite you to interview" in message.body_plain.lower()


def test_validate_env_names_missing_variables() -> None:
    with pytest.raises(RuntimeError, match="GMAIL_ACCOUNT"):
        validate_env({"account": "", "client_secret_file": "", "token_file": ""})


# ---------------------------------------------------------------------------
# The run: counters, dedupe, dry-run silence
# ---------------------------------------------------------------------------


def no_send(message: str) -> int:
    raise AssertionError("dry run must never send")


def test_dry_run_counts_and_classifies_without_sending(db: sqlite3.Connection) -> None:
    messages = [
        build_message(
            "Daily jobs for you", "Unsubscribe and manage preferences here.", message_id="m1"
        ),
        build_message(
            "Follow-up on your application",
            "Thanks for applying. We'd like to move forward.",
            message_id="m2",
        ),
    ]
    summary = run_watch(db, dry_run=True, fetch=lambda: messages, send=no_send)
    joined = "\n".join(summary.lines)
    assert summary.fetched_count == 2
    assert summary.unseen_count == 2
    assert summary.actionable_count == 1
    assert summary.ignored_count == 1
    assert "classified_kind=ignored" in joined
    assert "ignore_reason=marketing_or_security_email" in joined
    assert "classified_kind=recruiter_reply" in joined
    # The event log recorded both events.
    events = [json.loads(line) for line in events_path().read_text().splitlines()]
    assert len(events) == 2


def test_missing_message_id_is_reported_clearly(db: sqlite3.Connection) -> None:
    messages = [
        build_message(
            "Interview invitation",
            "We would like to invite you to interview next week.",
            message_id="",
        )
    ]
    summary = run_watch(db, dry_run=True, fetch=lambda: messages, send=no_send)
    joined = "\n".join(summary.lines)
    assert "message_id=<missing>" in joined
    assert "classified_kind=invalid_message_id" in joined
    assert summary.fetched_count == 1
    assert summary.unseen_count == 0


def test_seen_message_reports_already_seen(db: sqlite3.Connection) -> None:
    messages = [
        build_message(
            "Thanks for applying to Reedsy!", "We received your application.", message_id="m1"
        )
    ]
    first = run_watch(db, dry_run=True, fetch=lambda: messages, send=no_send)
    assert first.unseen_count == 1
    second = run_watch(db, dry_run=True, fetch=lambda: messages, send=no_send)
    joined = "\n".join(second.lines)
    assert "classified_kind=skipped_seen" in joined
    assert "skip_reason=already_seen" in joined
    assert second.unseen_count == 0


def test_seen_state_cap_drops_the_oldest_ids(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import harrier.mail.run as run_module
    from harrier.mail.watch import load_state, save_state

    monkeypatch.setattr(run_module, "SEEN_STATE_LIMIT", 5)
    save_state({"seen_message_ids": ["old1", "old2", "old3", "old4"]})
    messages = [
        build_message("Interview invitation", "We invite you to interview.", message_id="new1"),
        build_message("Interview invitation b", "We invite you to interview.", message_id="new2"),
        build_message("Interview invitation c", "We invite you to interview.", message_id="new3"),
    ]
    run_watch(db, dry_run=True, fetch=lambda: messages, send=no_send)
    state = load_state()
    # The cap keeps the NEWEST ids in insertion order (review finding:
    # a set-based cap dropped an arbitrary subset).
    assert state["seen_message_ids"] == ["old3", "old4", "new1", "new2", "new3"]


def test_int_env_guard_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from harrier.mail.watch import env_config

    monkeypatch.setenv("GMAIL_POLL_LOOKBACK_DAYS", "soon")
    with pytest.raises(RuntimeError, match="GMAIL_POLL_LOOKBACK_DAYS"):
        env_config()


def test_live_run_sends_actionable_and_stops_on_send_failure(
    db: sqlite3.Connection,
) -> None:
    add_job(
        db,
        {
            "company": "reedsy",
            "title": "Senior Software Engineer",
            "url": "https://example.test/reedsy",
            "source": "greenhouse",
            "status": "applied",
        },
    )
    messages = [
        build_message(
            "Interview invitation",
            "We would like to invite you to interview next week.",
            message_id="m1",
        ),
        build_message(
            "Interview invitation two",
            "We would like to invite you to interview as well.",
            message_id="m2",
        ),
    ]
    sent: list[str] = []

    def failing_send(text: str) -> int:
        sent.append(text)
        return 1 if len(sent) == 2 else 0

    summary = run_watch(db, dry_run=False, fetch=lambda: messages, send=failing_send)
    assert len(sent) == 2
    assert summary.send_failure == 1
