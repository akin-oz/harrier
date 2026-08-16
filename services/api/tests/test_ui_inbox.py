"""The Inbox page reads what the watch archived (spec 049).

The constraint that shapes this file is what the archive does not hold.
`redact_event` keeps a fixed field set, reduces the sender to its domain,
and drops the subject and body summary entirely, on the stated grounds that
they are the other party's words. So the strongest test here is a negative
one: `test_the_events_route_returns_nothing_the_archive_redacted` seeds an
event carrying a subject, a body and a full sender address, and asserts none
of it reaches the response.

The second theme is telling apart states that produce the same empty list.
A watch that never ran, a watch that ran and classified nothing, and a
rotated archive are three different situations with three different
answers, and an empty table that reads as any of the others is the defect.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import ast
import json
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from harrier.mail import read_events
from harrier.mail.watch import EVENTS_MAX_LINES, append_event, events_path
from harrier_api.app import create_app
from harrier_api.runs import PARAMETERIZED_KINDS, RunParams, build_command
from harrier_cli.main import build_parser, main


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


@pytest.fixture
def client(env: Path) -> TestClient:
    return TestClient(create_app())


def seed(**overrides: object) -> dict[str, object]:
    """One classified event, written the way the watch writes them.

    Every value is invented: an invented company at an invented domain
    (ADR-008).
    """
    event: dict[str, object] = {
        "kind": "interview_invite",
        "priority": "high",
        "company": "Northwind Labs",
        "role": "Senior Frontend Engineer",
        "tracker_row": "12",
        "next_action": "Reply and confirm interview availability.",
        "timestamp": "2026-08-10T09:00:00+00:00",
        "messageId": "abc123",
        "actionable": True,
        "from": "Someone Invented <someone@northwind.example>",
        "subject": "Interview availability for next week",
        "body_summary": "We would like to book a call.",
    }
    event.update(overrides)
    return event


# --- one implementation, two callers -----------------------------------------


def test_the_route_argv_reaches_the_same_function_the_cli_verb_does(env: Path) -> None:
    argv = build_command("gmail-watch", RunParams())[3:]
    assert argv[0] == PARAMETERIZED_KINDS["gmail-watch"].verb

    with patch("harrier.mail.run_watch") as domain, suppress(Exception):
        main(argv)
    assert domain.call_args is not None, "the gmail-watch argv did not reach run_watch"


def test_the_dry_run_flag_survives_the_trip_through_argv(env: Path) -> None:
    plain = build_parser().parse_args(build_command("gmail-watch", RunParams())[3:])
    dry = build_parser().parse_args(
        build_command("gmail-watch", RunParams(switches=frozenset({"--dry-run"})))[3:]
    )
    assert plain.dry_run is False
    assert dry.dry_run is True


def test_the_watch_takes_no_job(env: Path) -> None:
    """It reads a mailbox, not a tracker row."""
    with pytest.raises(ValueError, match="takes no job"):
        build_command("gmail-watch", RunParams(job_id=1))


def test_a_dry_run_notifies_nobody(env: Path) -> None:
    """Dry run suppresses the send. It still archives, which is what makes
    the classifier checkable without sending yourself messages.

    `fetch` and `send` are passed rather than patched: `run_watch` binds
    both as defaults, so patching the module attribute would leave the
    already-bound original in place and reach the real mailbox.
    """
    from harrier.db import connect
    from harrier.mail import run_watch
    from harrier.mail.watch import GmailMessage

    message = GmailMessage(
        message_id="abc123",
        subject="Interview availability",
        sender="Someone Invented <someone@northwind.example>",
        sender_email="someone@northwind.example",
        timestamp="2026-08-10T09:00:00+00:00",
        body_plain="We would like to book a call.",
        snippet="We would like to book a call.",
    )
    sent: list[str] = []

    def record(text: str) -> int:
        sent.append(text)
        return 0

    summary = run_watch(connect(), dry_run=True, fetch=lambda: [message], send=record)

    assert sent == [], "a dry run sent a notification"
    # It did classify: a dry run that silently did nothing would satisfy the
    # assertion above for the wrong reason.
    assert summary.fetched_count == 1


def test_a_missing_token_fails_the_watch_naming_the_command_that_repairs_it(
    env: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The likeliest failure on this page, and the one with a specific fix.

    A generic "watch failed" here costs the operator the most time, so the
    message has to carry the command. The environment is configured and only
    the token is absent: with nothing configured at all the watch fails
    earlier, on the variables, which is a different problem with a different
    fix and is not what this pins.
    """
    monkeypatch.setenv("GMAIL_ACCOUNT", "operator@invented.example")
    monkeypatch.setenv("GMAIL_OAUTH_CLIENT_SECRET_FILE", str(env / "client.json"))
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_FILE", str(env / "absent-token.json"))

    code = main(["gmail-watch"])
    printed = capsys.readouterr().err

    assert code != 0
    assert "gmail-oauth" in printed, printed


def test_a_delivery_failure_is_not_the_same_as_a_broken_watch(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Classified and archived, but Telegram declined.

    Both used to read as a failed run with nothing to tell them apart, and
    they need entirely different things from the operator.
    """
    from harrier.mail import WatchSummary

    summary = WatchSummary(actionable_count=2, send_failure=7)
    summary.lines.append("actionable_count=2")

    with patch("harrier.mail.run_watch", return_value=summary):
        code = main(["gmail-watch"])

    printed = capsys.readouterr()
    assert code == 7
    assert "classified_but_not_delivered" in printed.err
    # The classification is still reported as having worked.
    assert "actionable_count=2" in printed.out + printed.err


# --- the archive is the boundary ---------------------------------------------


def test_the_events_route_returns_nothing_the_archive_redacted(
    client: TestClient, env: Path
) -> None:
    """The strongest claim in this spec, checked on the response body.

    `append_event` redacts on the way in, so this seeds through it rather
    than writing the file by hand: a reader that somehow reached the
    unredacted event would still have to get past the writer.
    """
    append_event(seed())
    body = client.get("/mail/events").text

    assert "Interview availability" not in body, "a subject reached the page"
    assert "We would like to book" not in body, "a body summary reached the page"
    assert "someone@northwind.example" not in body, "a full sender address reached the page"
    assert "Someone Invented" not in body, "a sender's name reached the page"
    # What it does carry: the classification and the employer's domain.
    assert "interview_invite" in body
    assert "northwind.example" in body
    # The archive's own message id is not returned either: this route answers
    # without a token, and a stable identifier is the one field the spec's
    # "nothing identifying is here" argument does not cover.
    assert "abc123" not in body


def test_the_archive_still_holds_only_what_it_held(env: Path) -> None:
    """The store's own boundary, pinned separately from the reader's.

    Widening `ARCHIVED_FIELDS` is the failure spec 049 names, and the
    redaction test above cannot catch it on its own: `append_event` drops
    the subject on the way in, so a reader that grew a subject field still
    renders an empty one. This is what makes adding the field a failing
    test rather than a quiet change.
    """
    from harrier.mail.watch import ARCHIVED_FIELDS

    assert set(ARCHIVED_FIELDS) == {
        "kind",
        "priority",
        "company",
        "role",
        "tracker_row",
        "next_action",
        "timestamp",
        "messageId",
        "actionable",
        "ignore_reason",
    }
    for never in ("subject", "body_summary", "from", "sender", "snippet"):
        assert never not in ARCHIVED_FIELDS, f"the archive grew a {never} field"


def test_the_route_carries_only_fields_the_archive_holds(client: TestClient, env: Path) -> None:
    append_event(seed())
    event = client.get("/mail/events").json()["events"][0]
    assert set(event) == {
        "kind",
        "priority",
        "company",
        "role",
        "tracker_row",
        "next_action",
        "timestamp",
        "from_domain",
        "actionable",
        "ignore_reason",
    }


def test_the_next_action_comes_from_the_classifier(client: TestClient, env: Path) -> None:
    """The page renders the action the cascade decided; it does not decide
    one of its own, which would be a second implementation of the mapping."""
    append_event(seed())
    event = client.get("/mail/events").json()["events"][0]
    assert event["next_action"] == "Reply and confirm interview availability."


def test_events_are_newest_first(client: TestClient, env: Path) -> None:
    append_event(seed(company="Older Labs", timestamp="2026-08-01T09:00:00+00:00"))
    append_event(seed(company="Newer Labs", timestamp="2026-08-09T09:00:00+00:00"))
    events = client.get("/mail/events").json()["events"]
    assert [item["company"] for item in events] == ["Newer Labs", "Older Labs"]


def test_the_limit_takes_the_newest(client: TestClient, env: Path) -> None:
    for index in range(5):
        append_event(seed(company=f"Company {index}"))
    events = client.get("/mail/events?limit=2").json()["events"]
    assert [item["company"] for item in events] == ["Company 4", "Company 3"]


# --- three empty lists that mean different things ----------------------------


def test_a_watch_that_never_ran_is_not_an_empty_inbox(client: TestClient, env: Path) -> None:
    body = client.get("/mail/events").json()
    assert body["events"] == []
    assert body["has_run"] is False


def test_a_watch_that_ran_and_classified_nothing_says_so(client: TestClient, env: Path) -> None:
    """The file exists and holds nothing, which is a different answer from
    the one above and must not render the same way."""
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    body = client.get("/mail/events").json()
    assert body["events"] == []
    assert body["has_run"] is True


def test_a_rotated_archive_is_reported_as_a_window(client: TestClient, env: Path) -> None:
    """`_rotate_events` keeps the most recent lines and drops the rest, so
    the page is looking at recent history and has to say so."""
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(seed(messageId=f"m{i}")) for i in range(EVENTS_MAX_LINES)) + "\n",
        encoding="utf-8",
    )
    assert client.get("/mail/events").json()["at_cap"] is True


def test_a_short_archive_is_not_reported_as_a_window(client: TestClient, env: Path) -> None:
    append_event(seed())
    assert client.get("/mail/events").json()["at_cap"] is False


def test_an_unreadable_archive_is_not_reported_as_never_run(env: Path) -> None:
    """Sending the operator to run a watch that already ran would be the
    wrong fix for the wrong problem."""
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe not utf-8 at all")

    window = read_events()
    assert window.has_run is True
    assert window.events == []


def test_a_malformed_line_is_skipped_rather_than_failing_the_read(
    client: TestClient, env: Path
) -> None:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n" + json.dumps(seed()) + "\n", encoding="utf-8")
    assert len(client.get("/mail/events").json()["events"]) == 1


# --- nothing here writes to the archive --------------------------------------


def _imported_names(module_path: str) -> set[str]:
    """Every name the module imports, read from its syntax tree.

    Not a text search: the first version of this grepped the source and
    failed on its own docstring, which names `redact_event` while explaining
    why nothing calls it. A tree cannot make that mistake, and it does not
    break on a wrapped line either.
    """
    tree = ast.parse(Path(module_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
    return names


def test_only_the_watch_can_write_to_the_archive(env: Path) -> None:
    """The route module reaches no writer.

    Paired with the behavioural check below: this catches the import, and
    that catches the file changing.
    """
    names = _imported_names("src/harrier_api/mail_routes.py")
    for forbidden in ("append_event", "classify_message", "redact_event", "write_text"):
        assert forbidden not in names, f"a mail route reached {forbidden}"


def test_reading_the_events_does_not_change_them(client: TestClient, env: Path) -> None:
    append_event(seed())
    before = events_path().read_bytes()
    client.get("/mail/events")
    client.get("/mail/events?limit=1")
    assert events_path().read_bytes() == before


def test_no_mail_route_replies_to_anything(env: Path) -> None:
    """The watch reads and classifies. The operator replies in their own
    mail client, and no route here sends mail."""
    names = _imported_names("src/harrier_api/mail_routes.py")
    for forbidden in ("smtplib", "send_message", "send_telegram_message", "reply"):
        assert forbidden not in names


# --- the token boundary ------------------------------------------------------


def test_reading_the_events_needs_no_token(client: TestClient, env: Path) -> None:
    """Deliberately unlike spec 047's artifacts and spec 048's contacts.

    Those carry a person's own documents and a named human being. This
    carries a classification and an employer's domain, because the archive
    dropped everything else before it was written.
    """
    assert client.get("/mail/events").status_code == 200


def test_running_the_watch_needs_the_token(client: TestClient, env: Path) -> None:
    assert client.post("/mail/watch", json={}).status_code == 403


def test_both_routes_are_in_the_contract(env: Path) -> None:
    paths = create_app().openapi()["paths"]
    assert "/mail/events" in paths
    assert "/mail/watch" in paths
