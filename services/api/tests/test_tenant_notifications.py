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
import fcntl
import http.client
import json
import logging
import os
import secrets
import socket
import sys
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import DualStore
from test_hosted_auth import child_environment, complete_hosted_environment, start_api
from test_hosted_runs import hosted_manager as bound_manager
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


def malformed_tokens(token: str) -> dict[str, str]:
    """A token as an owner or an `.env` line could carry it: with a stray
    character where the scrub's `/bot<digits>:` pattern stops reading
    (privacy review of PR #312)."""
    bot, secret = token.split(":", 1)
    return {
        "a leading space": f" {token}",
        "a leading tab": f"\t{token}",
        "a leading newline": f"\n{token}",
        "a carriage return inside": f"{bot}:{secret[:8]}\r{secret[8:]}",
        "a space inside": f"{bot}:{secret[:8]} {secret[8:]}",
    }


# Faked rather than sent: a well-formed token would reach the network, and
# the last case is an error that quotes the path as written, not escaped.
FAKED = ("well formed", "a tab, quoted as written")
TOKEN_CASES = (*FAKED, *malformed_tokens(bot_token()))


@pytest.mark.parametrize("case", TOKEN_CASES)
def test_a_telegram_failure_log_never_holds_the_token(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = bot_token()
    token = {
        "well formed": clean,
        "a tab, quoted as written": malformed_tokens(clean)["a leading tab"],
    }.get(case) or malformed_tokens(clean)[case]
    monkeypatch.delenv("HARRIER_DEMO", raising=False)

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("this test reaches no network")

    # The malformed tokens go through the real HTTP client, which refuses
    # the path before it connects and quotes it, escaped, in its error.
    monkeypatch.setattr(socket, "create_connection", no_network)
    if case in FAKED:
        # What `http.client` raises for a path it will not send, quoting it
        # escaped; or an error that quotes it as written.
        def refuse(request: urllib.request.Request, timeout: float) -> object:
            # Read off the URL as the request holds it: `urlparse` would
            # drop the tab before any error could quote it.
            path = request.full_url.removeprefix("https://api.telegram.org")
            if case == "well formed":
                raise http.client.InvalidURL(f"URL can't contain control characters. {path!r}")
            raise OSError(f"cannot send {path}")

        monkeypatch.setattr(urllib.request, "urlopen", refuse)
    notify_logger = logging.getLogger("harrier.notify")
    emitted = AsEmitted()
    notify_logger.addHandler(emitted)
    monkeypatch.setattr(notify_logger, "level", logging.DEBUG)
    try:
        assert send_telegram_message("a digest line", token=token, chat_id=chat_id()) == 1
    finally:
        notify_logger.removeHandler(emitted)

    failed = [message for message in emitted.messages if "telegram send failed" in message]
    assert len(failed) == 1, emitted.messages
    assert "/botREDACTED/sendMessage" in failed[0], failed[0]
    assert_holds_none_of("\n".join(emitted.messages), clean, token)


# --- Gmail, hosted ---


def test_hosted_mail_routes_answer_501(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An archive the read would serve, and a watch the run would start.
    # Written before the process is hosted: hosted, the seam names no local
    # file (spec 107).
    path = events_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"kind": "interview_invite", "company": "Example Co"}) + "\n")
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    read: list[int | None] = []

    def read_events(limit: int | None = None) -> None:
        read.append(limit)

    monkeypatch.setattr("harrier.mail.read_events", read_events)
    spawned: list[tuple[object, ...]] = []

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
        with opened_under(path.parent.parent) as opened:
            for method, route, send in (
                ("GET", "/mail/events", {}),
                ("GET", "/mail/events?limit=5", {}),
                ("POST", "/mail/watch", {"json": {}}),
                ("POST", "/mail/watch", {"json": {"dry_run": True}}),
            ):
                response = hosted.client.request(method, route, headers=headers, **send)
                assert response.status_code == 501, (route, response.text)
                assert response.json() == {"detail": GMAIL_NOT_HOSTED_DETAIL}, route

    # Neither route opened a file in the data directory, read the archive or
    # reached the run manager.
    assert opened == []
    assert read == []
    assert started == []
    assert spawned == []
    assert manager.list_runs() == []
    assert not (tmp_path / "journal.jsonl").exists()

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


def test_a_hosted_gmail_watch_run_is_refused_before_it_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spec 106 lifts the refusal of every hosted run; the Gmail one stays.
    The manager serves hosted with its store half in memory, so another
    kind gets past every refusal to its spawn, and only the Gmail refusal
    stopped the watch."""
    spawned: list[tuple[object, ...]] = []

    async def spawn(*argv: object, **_: object) -> object:
        spawned.append(argv)
        raise OSError("this test spawns nothing")

    manager = bound_manager(tmp_path, monkeypatch, "pass").manager
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    async def attempt() -> None:
        for params in (RunParams(), RunParams(switches=frozenset({"--dry-run"}))):
            with pytest.raises(GmailNotHosted) as refused:
                await manager.start("gmail-watch", params, owner=new_owner())
            assert str(refused.value) == GMAIL_NOT_HOSTED_DETAIL
        # Another kind gets past the refusal to its spawn, so the lift is
        # real and only the Gmail refusal stopped the watch.
        demo = await manager.start("demo", owner=new_owner())
        await manager.wait(demo.id)
        assert [run.kind for run in manager.list_runs()] == [demo.kind] == ["demo"]

    asyncio.run(attempt())

    assert len(spawned) == 1, spawned
    assert "gmail-watch" not in " ".join(str(part) for part in spawned[0])
    # A hosted run writes no journal file (spec 106).
    assert not (tmp_path / "journal.jsonl").exists()


# --- the digest on Postgres ---


def opened_path(raw: object) -> str | None:
    """The real path an `open` audit event names: a path as text, bytes or
    a path object, relative ones against the working directory, or a file
    descriptor where the platform can say what it refers to."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        get_path = getattr(fcntl, "F_GETPATH", None)
        proc = f"/proc/self/fd/{raw}"
        try:
            if os.path.exists(proc):
                return os.path.realpath(os.readlink(proc))
            if get_path is None:
                return None
            named = fcntl.fcntl(raw, get_path, b"\0" * 1024).rstrip(b"\0")
            return os.path.realpath(os.fsdecode(named))
        except OSError:
            return None
    if isinstance(raw, (str, bytes, os.PathLike)):
        return os.path.realpath(os.fsdecode(raw))  # pyright: ignore[reportUnknownArgumentType]
    return None


@contextmanager
def opened_under(directory: Path) -> Generator[list[str]]:
    """Every file opened under `directory` while the block runs, read from
    the interpreter's own `open` audit event. An audit hook cannot be
    removed, so this one records only while its list is live."""
    opened: list[str] = []
    live = [True]
    root = os.path.realpath(directory)

    def hook(event: str, args: tuple[Any, ...]) -> None:
        if not live or event != "open" or not args:
            return
        path = opened_path(args[0])
        if path is not None and (path == root or path.startswith(root + os.sep)):
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
LOCAL = "locally, every one of them"


@pytest.mark.parametrize("name", (*OPERATOR_SETTINGS, LOCAL))
def test_hosted_start_refuses_operator_notification_settings(name: str, tmp_path: Path) -> None:
    values = {
        "TELEGRAM_BOT_TOKEN": bot_token(),
        "TELEGRAM_CHAT_ID": chat_id(),
        "GMAIL_ACCOUNT": f"owner-{secrets.token_hex(6)}@example.test",
        # Present and empty is refused too: the name alone is the operator's.
        "GMAIL_EMPTY": "",
    }
    if name == LOCAL:
        # The local product keeps its `.env` Telegram and Gmail settings:
        # the API starts with all of them (spec 109, Migration).
        started = start_api(child_environment(tmp_path, **values), tmp_path)
        assert started.returncode == 0, started.stderr
        return
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
