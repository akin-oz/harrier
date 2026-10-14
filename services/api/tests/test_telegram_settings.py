"""Each owner brings a Telegram bot (spec 109, part 2).

`check_telegram` is tested against a fake Bot API handed in as the opener,
with no store. The three routes are tested hosted, on a copy of the migrated
Postgres template, as two synthetic owners, with `urllib.request.urlopen`
replaced by the same fake. Locally the routes answer 404.

Every token and chat id here is made at run time, and nothing reaches the
network: the fake is the only opener, and `http.client` is told to refuse a
connection, so a call that missed the fake would fail. The Postgres tests
skip locally without a test server and fail in CI without one
(`tests/pg_support.py`).
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import http.client
import io
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from conftest import auth
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pg_support import open_owner_store
from test_credentials import record_statements
from test_host_lease import api_routes
from test_hosted_auth import credentials_key
from test_hosted_requests import Hosted, Server, copied_server, serving
from test_hosted_runs import (
    PRINT_ENVIRONMENT,
    digest,
    emit,
    event_text,
    hosted_manager,
    printed,
    run_to_end,
)
from test_tenant_notifications import AsEmitted, assert_holds_none_of, bot_token, chat_id

from harrier.credentials import FIELD_MAX_BYTES, decrypt, stored_credentials
from harrier.logredact import SECRET_REDACTION
from harrier.notify import (
    BOT_API_TIMEOUT_SECONDS,
    CONFIRMATION_TEXT,
    TELEGRAM_REASONS,
    TelegramReason,
    check_telegram,
)
from harrier_api.app import create_app
from harrier_api.deps import get_conn
from harrier_api.notify_routes import (
    LOCAL_TEXT,
    REFUSALS,
    TelegramSettingsIn,
)

TELEGRAM_HOST = "https://api.telegram.org/bot"

# --- a fake Bot API ---


@dataclass
class Answer:
    """What the fake Bot API does for one method: answers with a status and
    a body, or raises what an opener raises."""

    status: int = 200
    body: bytes = b'{"ok": true, "result": {}}'
    error: BaseException | None = None


OK = Answer()


def refused(status: int, *, error_code: int | None = None) -> Answer:
    """The Bot API's refusal: a 4xx and a JSON body with `ok: false`. Its
    `description` is text the harrier must never repeat."""
    body: dict[str, object] = {"ok": False, "description": "synthetic description from Telegram"}
    if error_code is not None:
        body["error_code"] = error_code
    return Answer(status, json.dumps(body).encode())


class Response:
    """The context manager `urlopen` returns."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]


@dataclass
class Call:
    method: str
    http_method: str
    fields: dict[str, list[str]]
    timeout: float | None
    url: str


@dataclass
class BotApi:
    """`urllib.request.urlopen`, as the Bot API at api.telegram.org answers
    it. Records every call; refuses any URL that is not a Bot API method."""

    script: dict[str, Answer] = field(default_factory=lambda: {"getMe": OK, "sendMessage": OK})
    calls: list[Call] = field(default_factory=lambda: [])

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> Response:
        url = request.full_url
        assert url.startswith(TELEGRAM_HOST), f"not the Bot API: {url[:40]}"
        method = url.rsplit("/", 1)[1]
        raw = request.data
        assert raw is None or isinstance(raw, bytes)
        fields = urllib.parse.parse_qs(raw.decode()) if raw else {}
        self.calls.append(Call(method, request.get_method(), fields, timeout, url))
        answer = self.script[method]
        if answer.error is not None:
            raise answer.error
        if answer.status >= 400:
            raise urllib.error.HTTPError(
                url, answer.status, "synthetic", Message(), io.BytesIO(answer.body)
            )
        return Response(answer.status, answer.body)

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """A call that missed the fake would connect through `http.client`."""

    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("this test reaches no network")

    monkeypatch.setattr(http.client.HTTPConnection, "connect", no_network)


def reason_cases() -> dict[str, tuple[TelegramReason, dict[str, Answer], list[str]]]:
    """Each reason a save is refused for, how the fake Bot API makes it
    happen, and the calls the check makes before it stops."""
    return {
        "token_shape": ("token_shape", {}, []),
        "chat_id_shape": ("chat_id_shape", {}, []),
        "getMe ok false": ("token_rejected", {"getMe": refused(401)}, ["getMe"]),
        "getMe ok false, any code": (
            "token_rejected",
            {"getMe": refused(400, error_code=500)},
            ["getMe"],
        ),
        "sendMessage ok false": (
            "chat_unreachable",
            {"sendMessage": refused(400)},
            ["getMe", "sendMessage"],
        ),
        "the bot was blocked": (
            "chat_unreachable",
            {"sendMessage": refused(403)},
            ["getMe", "sendMessage"],
        ),
        "getMe times out": (
            "telegram_unavailable",
            {"getMe": Answer(error=TimeoutError("timed out"))},
            ["getMe"],
        ),
        "sendMessage times out": (
            "telegram_unavailable",
            {"sendMessage": Answer(error=TimeoutError("timed out"))},
            ["getMe", "sendMessage"],
        ),
    }


# --- check_telegram, with no store ---


def test_check_telegram_checks_the_token_then_sends_one_confirmation() -> None:
    token, chat = bot_token(), chat_id()
    fake = BotApi()
    assert check_telegram(token, chat, opener=fake) is None
    assert fake.methods == ["getMe", "sendMessage"]
    get_me, send = fake.calls
    assert (get_me.http_method, get_me.fields, get_me.url) == (
        "GET",
        {},
        f"{TELEGRAM_HOST}{token}/getMe",
    )
    assert (send.http_method, send.url) == ("POST", f"{TELEGRAM_HOST}{token}/sendMessage")
    # Exactly one message, with the fixed text and nothing else.
    assert send.fields == {"chat_id": [chat], "text": [CONFIRMATION_TEXT]}
    assert CONFIRMATION_TEXT == "harrier: Telegram notifications are connected for this account."
    # Each call has the 30 seconds `send_telegram_message` allows.
    assert [call.timeout for call in fake.calls] == [30, 30]
    assert BOT_API_TIMEOUT_SECONDS == 30


def test_check_telegram_uses_the_urllib_opener_unless_given_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = BotApi()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert check_telegram(bot_token(), chat_id()) is None
    assert fake.methods == ["getMe", "sendMessage"]


def other_digits(zero: int, count: int) -> str:
    """`count` digits of another script, which `\\d` would take as digits
    and the Bot API's form does not: Arabic-Indic from 0x660, fullwidth
    from 0xFF10."""
    return "".join(chr(zero + 1 + at) for at in range(count))


def malformed_tokens() -> dict[str, str]:
    good = bot_token()
    bot, secret = good.split(":", 1)
    return {
        "empty": "",
        "no colon": bot + secret,
        "no bot id": ":" + secret,
        "no secret": bot + ":",
        "a leading space": " " + good,
        "a trailing space": good + " ",
        "a trailing newline": good + "\n",
        "a space inside": f"{bot}:{secret[:5]} {secret[5:]}",
        "a path in it": f"{bot}:{secret}/../getUpdates",
        "a query in it": f"{bot}:{secret}?x=1",
        "a second colon": f"{bot}:{secret}:{secret}",
        "Arabic-Indic digits": other_digits(0x660, 9) + ":" + secret,
        "fullwidth digits": other_digits(0xFF10, 3) + ":" + secret,
        "a non-ASCII letter": f"{bot}:{secret}{chr(0xE9)}",
        "a lone surrogate": f"{bot}:{secret}\ud800",
        "a channel username": "@" + secret,
        "over the field bound": f"{bot}:" + "a" * FIELD_MAX_BYTES,
    }


def malformed_chat_ids() -> dict[str, str]:
    return {
        "empty": "",
        "a minus alone": "-",
        "a channel username": "@somechannel",
        "a decimal": "12.5",
        "a leading plus": "+123456789",
        "a space": "123 456",
        "a trailing newline": "123456789\n",
        "Arabic-Indic digits": other_digits(0x660, 4),
        "fullwidth digits": other_digits(0xFF10, 3),
        "a double minus": "--123",
        "over the field bound": "9" * (FIELD_MAX_BYTES + 1),
    }


@pytest.mark.parametrize("case", sorted(malformed_tokens()))
def test_check_telegram_refuses_a_malformed_token_without_a_call(case: str) -> None:
    fake = BotApi()
    assert check_telegram(malformed_tokens()[case], chat_id(), opener=fake) == "token_shape"
    assert fake.calls == []


@pytest.mark.parametrize("case", sorted(malformed_chat_ids()))
def test_check_telegram_refuses_a_malformed_chat_id_without_a_call(case: str) -> None:
    fake = BotApi()
    assert check_telegram(bot_token(), malformed_chat_ids()[case], opener=fake) == "chat_id_shape"
    assert fake.calls == []


def test_check_telegram_accepts_the_documented_shapes() -> None:
    # A group's chat id is negative; a token's secret may hold `_` and `-`.
    for chat in ("0", "123456789", "-1001234567890"):
        assert check_telegram("1:a_b-C", chat, opener=BotApi()) is None


@pytest.mark.parametrize("case", sorted(reason_cases()))
def test_check_telegram_maps_each_answer_to_one_reason(case: str) -> None:
    reason, script, calls = reason_cases()[case]
    fake = BotApi({"getMe": OK, "sendMessage": OK, **script})
    token = "1:valid" if reason != "token_shape" else "not a token"
    chat = "42" if reason != "chat_id_shape" else "@name"
    assert check_telegram(token, chat, opener=fake) == reason
    # A call is made only when the one before it passed.
    assert fake.methods == calls


def test_every_reason_is_reached_and_none_is_invented() -> None:
    reached = {reason for reason, _, _ in reason_cases().values()}
    assert reached == set(TELEGRAM_REASONS)
    assert set(REFUSALS) == set(TELEGRAM_REASONS)


def transport_failures() -> dict[str, BaseException]:
    return {
        "a timeout": TimeoutError("timed out"),
        "a reset": ConnectionResetError("reset"),
        "no route": urllib.error.URLError("unreachable"),
        "a bad status line": http.client.BadStatusLine("x"),
        "the server hung up": http.client.RemoteDisconnected("gone"),
        "a refused path": http.client.InvalidURL("URL can't contain control characters"),
        "a value error": ValueError("x"),
    }


@pytest.mark.parametrize("method", ["getMe", "sendMessage"])
@pytest.mark.parametrize("case", sorted(transport_failures()))
def test_a_transport_failure_is_telegram_unavailable(case: str, method: str) -> None:
    fake = BotApi(
        {"getMe": OK, "sendMessage": OK, method: Answer(error=transport_failures()[case])}
    )
    assert check_telegram("1:valid", "42", opener=fake) == "telegram_unavailable"


def unusable_answers() -> dict[str, Answer]:
    return {
        "a 500 with ok false": refused(500),
        "a 502 with ok false": refused(502),
        "a 429 with ok false": refused(429),
        "a 503 whose body says ok true": Answer(503, b'{"ok": true}'),
        "a 200 that is HTML": Answer(200, b"<html>bad gateway</html>"),
        "a 200 that is empty": Answer(200, b""),
        "a 200 JSON list": Answer(200, b'[{"ok": true}]'),
        "a 200 with no ok": Answer(200, b'{"result": {}}'),
        "a 200 with ok as text": Answer(200, b'{"ok": "true"}'),
        "a 200 with ok as 1": Answer(200, b'{"ok": 1}'),
        "a 200 that is not UTF-8": Answer(200, b"\xff\xfe{"),
        "a 200 nested past the parser": Answer(200, b"[" * 100_000 + b"]" * 100_000),
        "a 4xx with no JSON": Answer(404, b"Not Found"),
        "a 4xx whose body says ok true": Answer(400, b'{"ok": true}'),
    }


@pytest.mark.parametrize("method", ["getMe", "sendMessage"])
@pytest.mark.parametrize("case", sorted(unusable_answers()))
def test_an_answer_that_is_not_about_the_bot_is_telegram_unavailable(
    case: str, method: str
) -> None:
    fake = BotApi({"getMe": OK, "sendMessage": OK, method: unusable_answers()[case]})
    assert check_telegram("1:valid", "42", opener=fake) == "telegram_unavailable"


def test_the_mapping_reads_ok_and_not_error_code() -> None:
    # `ok: false` is a refusal whatever `error_code` says, and `ok: true`
    # is success whatever it says (the Bot API calls `error_code` subject
    # to change).
    refusal = BotApi({"getMe": refused(401, error_code=200), "sendMessage": OK})
    assert check_telegram("1:valid", "42", opener=refusal) == "token_rejected"
    success = BotApi({"getMe": Answer(200, b'{"ok": true, "error_code": 401}'), "sendMessage": OK})
    assert check_telegram("1:valid", "42", opener=success) is None


def test_an_error_that_quotes_the_token_leaves_nothing_of_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    token, chat = bot_token(), chat_id()
    quoting = http.client.InvalidURL(f"URL can't contain control characters. '/bot{token}/getMe'")
    for method in ("getMe", "sendMessage"):
        fake = BotApi({"getMe": OK, "sendMessage": OK, method: Answer(error=quoting)})
        assert check_telegram(token, chat, opener=fake) == "telegram_unavailable"
    assert caplog.records == []


# --- the routes, hosted ---


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture
def hosted(server: Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hosted]:
    with serving(monkeypatch, server.login_role().url) as built:
        yield built


@pytest.fixture
def bot_api(monkeypatch: pytest.MonkeyPatch) -> BotApi:
    """The Bot API, answering every call ok until a test scripts it."""
    fake = BotApi()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def save(hosted: Hosted, owner: str, token: str, chat: str, **extra: Any) -> Any:
    return hosted.client().put(
        "/notifications/telegram",
        json={"bot_token": token, "chat_id": chat, **extra},
        headers=hosted.headers(owner),
    )


def get_state(hosted: Hosted, owner: str) -> dict[str, Any]:
    response = hosted.client().get("/notifications/telegram", headers=hosted.headers(owner))
    assert response.status_code == 200, response.text
    return response.json()


def stored_pair(server: Server, owner: str) -> dict[str, str] | None:
    """What the owner's `telegram` row decrypts to, read as the owner."""
    conn = open_owner_store(server.url, owner)
    try:
        rows = [row for row in stored_credentials(conn) if row.kind == "telegram"]
        return decrypt(credentials_key(), owner, rows[0]) if rows else None
    finally:
        conn.close()


def telegram_rows(server: Server, owner: str) -> int:
    conn = open_owner_store(server.url, owner)
    try:
        return sum(1 for row in stored_credentials(conn) if row.kind == "telegram")
    finally:
        conn.close()


def test_saving_telegram_checks_the_bot_and_sends_one_confirmation(
    server: Server, hosted: Hosted, bot_api: BotApi
) -> None:
    owner = server.owner()
    token, chat = bot_token(), chat_id()
    assert get_state(hosted, owner) == {"configured": False, "updated_at": None}

    # Padded, as a paste can be: checked and stored stripped.
    response = save(hosted, owner, f"  {token}\n", f"\t{chat} ")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert set(answer) == {"configured", "updated_at"}
    assert answer["configured"] is True and isinstance(answer["updated_at"], str)

    # getMe, then exactly one message with the fixed text, to the chat.
    assert bot_api.methods == ["getMe", "sendMessage"]
    assert bot_api.calls[0].url == f"{TELEGRAM_HOST}{token}/getMe"
    send = bot_api.calls[1]
    assert send.url == f"{TELEGRAM_HOST}{token}/sendMessage"
    assert send.fields == {"chat_id": [chat], "text": [CONFIRMATION_TEXT]}
    assert [call.timeout for call in bot_api.calls] == [30, 30]

    # Both values are stored for the request's owner, encrypted.
    assert stored_pair(server, owner) == {"bot_token": token, "chat_id": chat}
    assert get_state(hosted, owner) == answer

    # A later save replaces the pair: one row, the new values.
    other_token, other_chat = bot_token(), chat_id()
    again = save(hosted, owner, other_token, other_chat)
    assert again.status_code == 200, again.text
    assert telegram_rows(server, owner) == 1
    assert stored_pair(server, owner) == {"bot_token": other_token, "chat_id": other_chat}
    assert bot_api.methods == ["getMe", "sendMessage"] * 2


def refusal_attempts() -> dict[str, tuple[TelegramReason, dict[str, Answer], list[str], str, str]]:
    """Each case with a token and a chat id that reach the reason."""
    out: dict[str, tuple[TelegramReason, dict[str, Answer], list[str], str, str]] = {}
    for case, (reason, script, calls) in reason_cases().items():
        token = bot_token() if reason != "token_shape" else "not a token"
        chat = chat_id() if reason != "chat_id_shape" else "@name"
        out[case] = (reason, script, calls, token, chat)
    return out


@pytest.mark.parametrize("case", sorted(reason_cases()))
def test_a_refused_telegram_setting_stores_nothing(
    case: str, server: Server, hosted: Hosted, bot_api: BotApi
) -> None:
    reason, script, calls, token, chat = refusal_attempts()[case]
    owner = server.owner()
    # An earlier pair, saved when the Bot API says yes to everything.
    before_token, before_chat = bot_token(), chat_id()
    assert save(hosted, owner, before_token, before_chat).status_code == 200
    before = get_state(hosted, owner)
    bot_api.calls.clear()

    bot_api.script = {"getMe": OK, "sendMessage": OK, **script}
    response = save(hosted, owner, token, chat)

    status, detail = REFUSALS[reason]
    assert (response.status_code, response.json()) == (status, {"detail": detail, "reason": reason})
    assert bot_api.methods == calls
    # The earlier pair survives, unchanged and not rewritten.
    assert stored_pair(server, owner) == {"bot_token": before_token, "chat_id": before_chat}
    assert get_state(hosted, owner) == before

    # An owner with no earlier pair has still none.
    fresh = server.owner()
    again = save(hosted, fresh, token, chat)
    assert (again.status_code, again.json()["reason"]) == (status, reason)
    assert telegram_rows(server, fresh) == 0
    assert get_state(hosted, fresh) == {"configured": False, "updated_at": None}


def test_each_reason_has_its_own_status_and_sentence() -> None:
    statuses = {reason: REFUSALS[reason][0] for reason in TELEGRAM_REASONS}
    assert statuses == {
        "token_shape": 400,
        "chat_id_shape": 400,
        "token_rejected": 400,
        "chat_unreachable": 400,
        "telegram_unavailable": 502,
    }
    sentences = [REFUSALS[reason][1] for reason in TELEGRAM_REASONS]
    assert len(set(sentences)) == len(sentences)
    # The owner who has not yet written to their bot is told to.
    assert "Send your bot a message first" in REFUSALS["chat_unreachable"][1]


def malformed_bodies(token: str, chat: str) -> dict[str, bytes]:
    """Bodies the schema refuses, each holding the secrets where a default
    422 would echo them."""

    def encode(value: object) -> bytes:
        return json.dumps(value).encode()

    return {
        "chat id as a number": encode({"bot_token": token, "chat_id": int(chat)}),
        "token as a number": encode({"bot_token": int(chat), "chat_id": chat}),
        "token null": encode({"bot_token": None, "chat_id": chat}),
        "chat id missing": encode({"bot_token": token}),
        "token missing": encode({"chat_id": chat}),
        "a field named by the token": encode({"bot_token": token, "chat_id": chat, token: chat}),
        "a field named by the chat id": encode({"bot_token": token, "chat_id": chat, chat: "x"}),
        "a list": encode([token, chat]),
        "a string": encode(f"{token} {chat}"),
        "not JSON": f'{{"bot_token": "{token}", "chat_id": "{chat}"'.encode(),
    }


def test_a_telegram_secret_never_reaches_a_body_or_a_log(
    server: Server,
    hosted: Hosted,
    bot_api: BotApi,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    emitted = AsEmitted()
    route_logger = logging.getLogger("harrier_api.notify_routes")
    route_logger.addHandler(emitted)
    owner = server.owner()
    token, chat = bot_token(), chat_id()
    shown: list[str] = []
    client = hosted.client()

    try:
        # Every refusal, with the secrets in each, as the token a Bot API
        # error would quote it.
        quoting = http.client.InvalidURL(f"URL can't contain control chars '/bot{token}/getMe'")
        for case, (reason, script, _, _, _) in refusal_attempts().items():
            bot_api.script = {"getMe": OK, "sendMessage": OK, **script}
            if reason in ("token_shape", "chat_id_shape"):
                # Shapes: the secrets are the other half of a malformed pair.
                response = save(
                    hosted,
                    owner,
                    "x" + token if reason == "token_shape" else token,
                    "@" + chat if reason == "chat_id_shape" else chat,
                )
            else:
                response = save(hosted, owner, token, chat)
            assert response.status_code in (400, 502), (case, response.text)
            assert response.json()["reason"] == reason
            shown.append(response.text)
        bot_api.script = {"getMe": Answer(error=quoting), "sendMessage": OK}
        response = save(hosted, owner, token, chat)
        assert response.status_code == 502, response.text
        shown.append(response.text)
        bot_api.script = {"getMe": OK, "sendMessage": OK}

        # Bodies the schema refuses: the field and the rule, never the input.
        for case, body in malformed_bodies(token, chat).items():
            response = client.put(
                "/notifications/telegram",
                content=body,
                headers={**hosted.headers(owner), "Content-Type": "application/json"},
            )
            assert response.status_code == 422, (case, response.text)
            detail = response.json()["detail"]
            assert all(set(entry) == {"field", "problem"} for entry in detail), (case, detail)
            shown.append(response.text)
        # The unknown field is named by the route's own, not by the caller.
        response = client.put(
            "/notifications/telegram",
            json={"bot_token": token, "chat_id": chat, token: chat},
            headers=hosted.headers(owner),
        )
        assert response.json()["detail"] == [
            {"field": "fields", "problem": "may only be bot_token, chat_id"}
        ]

        # A good save, a read, a clear.
        for response in (
            save(hosted, owner, token, chat),
            client.get("/notifications/telegram", headers=hosted.headers(owner)),
            client.delete("/notifications/telegram", headers=hosted.headers(owner)),
        ):
            assert response.status_code == 200, response.text
            shown.append(response.text)
    finally:
        route_logger.removeHandler(emitted)

    # The models, by repr and str, hold neither.
    parsed = TelegramSettingsIn(bot_token=token, chat_id=chat)  # type: ignore[arg-type]
    shown += [repr(parsed), str(parsed)]

    records = [record.getMessage() for record in caplog.records] + emitted.messages
    records += [str(record.exc_text or "") for record in caplog.records]
    # The log is not empty, so reading it proves something: each refusal
    # logged its reason alone.
    refusals = [m for m in emitted.messages if m.startswith("telegram settings refused")]
    assert refusals
    assert {m.rpartition(": ")[2] for m in refusals} <= set(TELEGRAM_REASONS)
    for text in shown + records:
        assert_holds_none_of(text, token, chat)
    # The confirmation text is the one thing sent, and it holds neither.
    assert_holds_none_of(CONFIRMATION_TEXT, token, chat)


def test_the_response_to_a_good_save_is_not_the_pair(
    server: Server, hosted: Hosted, bot_api: BotApi
) -> None:
    owner = server.owner()
    token, chat = bot_token(), chat_id()
    response = save(hosted, owner, token, chat)
    assert response.status_code == 200
    assert_holds_none_of(response.text + json.dumps(dict(response.headers)), token, chat)


def test_reading_and_clearing_telegram_settings(
    server: Server, hosted: Hosted, bot_api: BotApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = server.owner()
    token, chat = bot_token(), chat_id()
    client = hosted.client()
    saved = save(hosted, owner, token, chat).json()
    bot_api.calls.clear()

    with monkeypatch.context() as patch:
        seen = record_statements(patch)
        read = client.get("/notifications/telegram", headers=hosted.headers(owner))
    assert read.status_code == 200
    assert read.json() == saved
    assert set(read.json()) == {"configured", "updated_at"}
    assert any("credentials" in sql for sql in seen), "the read did not touch the store"
    assert not any("ciphertext" in sql or "SELECT *" in sql for sql in seen), seen

    cleared = client.delete("/notifications/telegram", headers=hosted.headers(owner))
    assert (cleared.status_code, cleared.json()) == (
        200,
        {"configured": False, "updated_at": None},
    )
    assert telegram_rows(server, owner) == 0
    assert stored_pair(server, owner) is None
    assert get_state(hosted, owner) == {"configured": False, "updated_at": None}

    # Deleting when nothing is stored answers the same.
    again = client.delete("/notifications/telegram", headers=hosted.headers(owner))
    assert (again.status_code, again.json()) == (200, {"configured": False, "updated_at": None})

    # Neither route called Telegram.
    assert bot_api.calls == []


def test_clearing_telegram_leaves_the_other_credentials(
    server: Server, hosted: Hosted, bot_api: BotApi
) -> None:
    owner = server.owner()
    client = hosted.client()
    assert save(hosted, owner, bot_token(), chat_id()).status_code == 200
    put = client.put(
        "/credentials/apify", json={"token": "synthetic-apify-token"}, headers=hosted.headers(owner)
    )
    assert put.status_code == 200
    client.delete("/notifications/telegram", headers=hosted.headers(owner))
    listed = client.get("/credentials", headers=hosted.headers(owner)).json()["credentials"]
    assert {item["kind"]: item["set"] for item in listed} == {
        "ai": False,
        "apify": True,
        "hunter": False,
        "telegram": False,
    }


def test_telegram_settings_are_per_owner(server: Server, hosted: Hosted, bot_api: BotApi) -> None:
    a, b = server.owner(), server.owner()
    token, chat = bot_token(), chat_id()
    saved = save(hosted, a, token, chat).json()
    client = hosted.client()

    # B sees nothing of A's, before and after A reads their own, and
    # clearing leaves A's pair.
    assert get_state(hosted, a) == saved
    assert get_state(hosted, b) == {"configured": False, "updated_at": None}
    cleared = client.delete("/notifications/telegram", headers=hosted.headers(b))
    assert cleared.json() == {"configured": False, "updated_at": None}
    assert get_state(hosted, a) == saved
    assert get_state(hosted, b) == {"configured": False, "updated_at": None}
    assert stored_pair(server, a) == {"bot_token": token, "chat_id": chat}

    # B saving a pair of their own changes nothing of A's.
    b_token, b_chat = bot_token(), chat_id()
    assert save(hosted, b, b_token, b_chat).status_code == 200
    assert stored_pair(server, b) == {"bot_token": b_token, "chat_id": b_chat}
    assert stored_pair(server, a) == {"bot_token": token, "chat_id": chat}
    assert get_state(hosted, a) == saved


def test_a_saved_pair_reaches_only_its_owners_run_and_never_the_event_stream(
    server: Server, hosted: Hosted, bot_api: BotApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row the route writes is the row spec 106's run start reads: the
    owner's run holds the pair under the variable names, another owner's
    holds neither, and what the run prints reaches no event."""
    a, b = server.owner(), server.owner()
    token, chat = bot_token(), chat_id()
    assert save(hosted, a, token, chat).status_code == 200

    conn = open_owner_store(server.url, a)
    try:
        rows = stored_credentials(conn)
    finally:
        conn.close()
    summary = json.dumps({"event": "summary", "chat": chat, "nested": [token]})
    script = PRINT_ENVIRONMENT + emit(f"sent with {token} to {chat}", "::harrier::" + summary)
    run_host = hosted_manager(tmp_path, monkeypatch, script, {a: rows})

    mine = run_to_end(run_host.manager, a)
    assert mine.state == "succeeded"
    env = printed(mine)["env"]
    assert env["TELEGRAM_BOT_TOKEN"] == digest(token)
    assert env["TELEGRAM_CHAT_ID"] == digest(chat)
    text = event_text(mine)
    assert SECRET_REDACTION in text
    assert_holds_none_of(text, token, chat)

    theirs = run_to_end(run_host.manager, b)
    other = printed(theirs)["env"]
    assert "TELEGRAM_BOT_TOKEN" not in other and "TELEGRAM_CHAT_ID" not in other


# --- the routes, locally ---


def notification_routes(app: Any) -> list[APIRoute]:
    return [route for route in api_routes(app.routes) if route.path.startswith("/notifications")]


def test_telegram_routes_are_not_served_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = BotApi()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    app = create_app()
    entered: list[str] = []

    def spy() -> Iterator[None]:
        entered.append("get_conn")
        yield None

    app.dependency_overrides[get_conn] = spy
    client = TestClient(app)
    token, chat = bot_token(), chat_id()
    walked: list[tuple[str, str]] = []
    for route in notification_routes(app):
        for method in sorted(route.methods or ()):
            # A valid body, an invalid one, none, and one that is not JSON,
            # which FastAPI reads before any dependency: always the local 404.
            bodies: Sequence[object] = (
                {"bot_token": token, "chat_id": chat},
                {"bot_token": 1},
                None,
                b'{"bot_token": ',
            )
            for body in bodies:
                if isinstance(body, bytes):
                    response = client.request(
                        method,
                        route.path,
                        content=body,
                        headers={**auth(), "Content-Type": "application/json"},
                    )
                else:
                    response = client.request(method, route.path, json=body, headers=auth())
                assert (response.status_code, response.json()) == (404, {"detail": LOCAL_TEXT}), (
                    method,
                    body,
                )
                assert_holds_none_of(response.text, token, chat)
            walked.append((method, route.path))
    assert sorted(walked) == [
        ("DELETE", "/notifications/telegram"),
        ("GET", "/notifications/telegram"),
        ("PUT", "/notifications/telegram"),
    ]
    assert entered == [] and fake.calls == []
    assert LOCAL_TEXT == "Telegram is configured in .env on the local product"
    # The same routes are in the one contract.
    paths = app.openapi()["paths"]
    for method, path in walked:
        assert method.lower() in paths[path], (method, path)


# --- the contract ---


def test_the_contract_declares_the_telegram_routes() -> None:
    document = create_app().openapi()
    path = document["paths"]["/notifications/telegram"]
    assert sorted(path) == ["delete", "get", "put"]
    schemas = document["components"]["schemas"]

    def ref(operation: dict[str, Any], status: str) -> str:
        content = operation["responses"][status]["content"]["application/json"]["schema"]
        return content["$ref"].rpartition("/")[2]

    assert ref(path["get"], "200") == "TelegramSettingsOut"
    assert ref(path["delete"], "200") == "TelegramSettingsOut"
    put = path["put"]
    assert ref(put, "200") == "TelegramSettingsOut"
    assert ref(put, "400") == "TelegramRefusalOut"
    assert ref(put, "502") == "TelegramRefusalOut"
    assert ref(put, "422") == "CredentialRefusalOut"
    for operation in path.values():
        assert ref(operation, "404") == "ErrorOut"
    body = put["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert body.rpartition("/")[2] == "TelegramSettingsIn"

    settings_in = schemas["TelegramSettingsIn"]
    assert sorted(settings_in["required"]) == ["bot_token", "chat_id"]
    # Plain strings to the schema: no pattern, so a malformed value reaches
    # the handler's own check.
    for name in ("bot_token", "chat_id"):
        assert settings_in["properties"][name]["type"] == "string"
        assert "pattern" not in settings_in["properties"][name]
    assert sorted(schemas["TelegramSettingsOut"]["properties"]) == ["configured", "updated_at"]
    refusal = schemas["TelegramRefusalOut"]["properties"]
    assert sorted(refusal) == ["detail", "reason"]
    assert sorted(refusal["reason"]["enum"]) == sorted(TELEGRAM_REASONS)
