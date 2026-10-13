"""Telegram and Gmail for tenants (spec 109).

Gmail watch is not offered on the hosted service: the hosted API refuses
both mail routes, the run manager refuses a hosted `gmail-watch` before it
spawns, and the digest on a Postgres store opens no mail file and says why.
The hosted API refuses to start while it holds the operator's own Telegram
or any Gmail setting, and a failed Telegram send logs no token.

Every token, chat id and key here is made at run time. No test reaches
Telegram, Google or Supabase; the Postgres tests use the local test server
(spec 039) and skip without one.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import secrets
import sys
import urllib.parse
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import DualStore
from test_hosted_auth import child_environment, complete_hosted_environment, start_api
from test_run_owner import CONTRACT, hosted_manager, new_owner, owners

from harrier.digest import MAIL_NOT_OFFERED_LINE, run_digest
from harrier.mail.watch import events_path
from harrier.notify import send_telegram_message
from harrier.runoutcome import MAIL_WATCH_JOB
from harrier.tracks import default_scope
from harrier_api.app import create_app
from harrier_api.hostedauth import operator_notification_refusal
from harrier_api.runs import (
    GMAIL_NOT_HOSTED_DETAIL,
    GmailNotHosted,
    RunManager,
    RunParams,
    RunsNotHosted,
    run_inputs_dir,
)

# --- synthetic secrets ---


def bot_token() -> str:
    """A token of the form the Bot API documents, `<bot id>:<secret>`."""
    return f"{secrets.randbelow(9 * 10**9) + 10**9}:{secrets.token_urlsafe(26)}"


def chat_id() -> str:
    return str(secrets.randbelow(9 * 10**9) + 10**9)


def windows(value: str, width: int = 8) -> set[str]:
    """Every run of `width` characters in `value`, or the value when it is
    shorter: a secret cut short, or wrapped, still leaks."""
    if len(value) <= width:
        return {value}
    return {value[at : at + width] for at in range(len(value) - width + 1)}


def assert_holds_none_of(text: str, *values: str) -> None:
    for value in values:
        leaked = sorted(window for window in windows(value) if window in text)
        assert not leaked, leaked


# --- a failed send ---


class AsEmitted(logging.Handler):
    """The text of each record as its logger emits it, before any handler's
    filter on the way up: the root's scrub (spec 133) rewrites a record in
    place, so a record read after it says nothing of what the code logged."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_telegram_failure_log_never_holds_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token, chat = bot_token(), chat_id()
    monkeypatch.delenv("HARRIER_DEMO", raising=False)

    def refuse(request: urllib.request.Request, timeout: float) -> object:
        # What `http.client` raises for a token pasted with a stray
        # character: its message quotes the request path, token and all.
        path = urllib.parse.urlparse(request.full_url).path
        raise http.client.InvalidURL(f"URL can't contain control characters. {path!r}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    notify_logger = logging.getLogger("harrier.notify")
    emitted = AsEmitted()
    notify_logger.addHandler(emitted)
    monkeypatch.setattr(notify_logger, "level", logging.DEBUG)
    try:
        assert send_telegram_message("a digest line", token=token, chat_id=chat) == 1
    finally:
        notify_logger.removeHandler(emitted)

    failed = [message for message in emitted.messages if "telegram send failed" in message]
    assert len(failed) == 1, emitted.messages
    assert "/botREDACTED/sendMessage" in failed[0]
    assert_holds_none_of("\n".join(emitted.messages), token, chat)


# --- Gmail, hosted ---


def test_hosted_mail_routes_answer_501(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    # An archive the read would serve, and a watch the run would start.
    path = events_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"kind": "interview_invite", "company": "Example Co"}) + "\n")
    read: list[int | None] = []

    def read_events(limit: int | None = None) -> None:
        read.append(limit)

    monkeypatch.setattr("harrier.mail.read_events", read_events)
    spawned: list[object] = []

    async def spawn(*argv: object, **_: object) -> object:
        spawned.append(argv)
        raise OSError("this test spawns nothing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    manager = hosted_manager(tmp_path)
    started: list[str] = []
    refusing_start = manager.start

    async def start(kind: str, params: Any = None, *, owner: str = "") -> Any:
        started.append(kind)
        return await refusing_start(kind, params, owner=owner)

    monkeypatch.setattr(manager, "start", start)

    with owners(monkeypatch, manager) as hosted:
        headers = hosted.as_owner(new_owner())
        for method, route, send in (
            ("GET", "/mail/events", {}),
            ("GET", "/mail/events?limit=5", {}),
            ("POST", "/mail/watch", {"json": {}}),
            ("POST", "/mail/watch", {"json": {"dry_run": True}}),
        ):
            response = hosted.client.request(method, route, headers=headers, **send)
            assert response.status_code == 501, (route, response.text)
            assert response.json() == {"detail": GMAIL_NOT_HOSTED_DETAIL}, route

    # Neither route read the archive nor reached the run manager.
    assert read == []
    assert started == []
    assert spawned == []
    assert manager.list_runs() == []
    assert not (tmp_path / "journal.jsonl").exists()
    assert not run_inputs_dir().exists()

    # Both declare the 501 with its detail, in the committed contract and in
    # the document the local app builds, so the two modes share one.
    monkeypatch.delenv("HARRIER_AUTH")
    error_out = {"$ref": "#/components/schemas/ErrorOut"}
    built = create_app(run_manager=RunManager(kind_commands={})).openapi()
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    for document in (committed, built):
        for method, route in (("get", "/mail/events"), ("post", "/mail/watch")):
            refusal = document["paths"][route][method]["responses"]["501"]
            assert refusal["content"]["application/json"]["schema"] == error_out
            assert refusal["description"] == f"hosted only: {GMAIL_NOT_HOSTED_DETAIL}", route


@pytest.mark.parametrize("lifted", [False, True], ids=["today", "with spec 106's lift"])
def test_a_hosted_gmail_watch_run_is_refused_before_it_spawns(
    lifted: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    if lifted:
        # Spec 106 lifts the refusal of every hosted run; the Gmail one
        # stays.
        monkeypatch.setattr("harrier_api.runs.refuse_every_hosted_run", lambda: None)
    spawned: list[object] = []

    async def spawn(*argv: object, **_: object) -> object:
        spawned.append(argv)
        raise OSError("this test spawns nothing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    journal = tmp_path / "journal.jsonl"
    manager = RunManager(
        journal_path=journal, kind_commands={"demo": [sys.executable, "-c", "pass"]}
    )

    async def attempt() -> None:
        for params in (RunParams(), RunParams(switches=frozenset({"--dry-run"}))):
            with pytest.raises(GmailNotHosted) as refused:
                await manager.start("gmail-watch", params, owner=new_owner())
            assert str(refused.value) == GMAIL_NOT_HOSTED_DETAIL
        if not lifted:
            # Every other kind meets the refusal spec 106 lifts.
            with pytest.raises(RunsNotHosted) as other:
                await manager.start("demo", owner=new_owner())
            assert not isinstance(other.value, GmailNotHosted)

    asyncio.run(attempt())

    assert spawned == []
    assert manager.list_runs() == []
    assert not journal.exists()
    assert not run_inputs_dir().exists()


# --- the digest on Postgres ---


@contextmanager
def opened_under(directory: Path) -> Generator[list[str]]:
    """Every file opened under `directory` while the block runs, read from
    the interpreter's own `open` audit event. An audit hook cannot be
    removed, so this one records only while its list is live."""
    opened: list[str] = []
    live = [True]
    root = str(directory.resolve())

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if not live or event != "open" or not args or not isinstance(args[0], (str, Path)):
            return
        path = str(Path(args[0]).resolve()) if Path(args[0]).is_absolute() else str(args[0])
        if path.startswith(root):
            opened.append(path)

    sys.addaudithook(hook)
    try:
        yield opened
    finally:
        live.clear()


def test_a_postgres_digest_reads_no_mail_file_and_says_why(dual_store: DualStore) -> None:
    conn = dual_store.open()
    target = datetime.now(UTC).date()
    # An actionable event for the digest's day, in the data directory a
    # SQLite digest reads.
    path = events_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "kind": "interview_invite",
                "company": "Example Co",
                "role": "Senior Frontend Engineer",
                "timestamp": f"{target.isoformat()}T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def no_send(message: str) -> int:
        raise AssertionError("a dry run sends nothing")

    data_dir = path.parent.parent
    with opened_under(data_dir) as opened:
        digest, rc = run_digest(conn, default_scope(conn), target, dry_run=True, send=no_send)
    assert rc == 0
    mail = digest.split("Updates needing action\n", 1)[1].splitlines()

    if dual_store.dialect == "postgres":
        assert mail == [MAIL_NOT_OFFERED_LINE]
        assert MAIL_WATCH_JOB not in digest
        assert "Example Co" not in digest
        assert opened == []
    else:
        # Unchanged locally: the event, and the watch's health line.
        assert len(mail) == 1 and mail[0].startswith("• interview invite: Example Co"), mail
        assert f"{MAIL_WATCH_JOB}: has never recorded a success" in digest
        assert str(path.resolve()) in opened


# --- the hosted API's own environment ---

OPERATOR_SETTINGS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GMAIL_ACCOUNT", "GMAIL_EMPTY")


@pytest.mark.parametrize("name", OPERATOR_SETTINGS)
def test_hosted_start_refuses_operator_notification_settings(name: str, tmp_path: Path) -> None:
    values = {
        "TELEGRAM_BOT_TOKEN": bot_token(),
        "TELEGRAM_CHAT_ID": chat_id(),
        "GMAIL_ACCOUNT": f"owner-{secrets.token_hex(6)}@example.test",
        # Present and empty is refused too: the name alone is the operator's.
        "GMAIL_EMPTY": "",
    }
    environ = child_environment(tmp_path, **{**complete_hosted_environment(), name: values[name]})

    started = start_api(environ, tmp_path)

    assert started.returncode == 1, started.stderr
    last_line = started.stderr.strip().splitlines()[-1]
    assert last_line == (
        f"harrier_api.hostedauth.HostedConfigError: {operator_notification_refusal(name)}"
    ), started.stderr
    if values[name]:
        assert_holds_none_of(started.stdout + started.stderr, values[name])
    assert not (tmp_path / "data").exists()
