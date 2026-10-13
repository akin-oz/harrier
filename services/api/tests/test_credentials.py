"""Tenant credentials are stored encrypted (spec 116).

The store's tests run on a copy of the migrated Postgres template as two
synthetic owners under the row policy, through `harrier.credentials`. The
routes' tests build the hosted app as uvicorn does, on such a copy, with
the helpers in `tests/test_hosted_requests.py`. Locally the routes answer
404, and SQLite has no credentials table.

Every key, token and value here is made at run time: the credentials key
is 32 random bytes, and each submitted value is a random string no other
text holds, so finding it anywhere is a leak and not a coincidence. The
Postgres tests skip locally without a test server and fail in CI without
one (`tests/pg_support.py`).
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import base64
import json
import logging
import os
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from conftest import auth
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pg_support import (
    database_copy,
    fresh_database,
    new_owner,
    open_owner_store,
    run_as_migrator,
)
from test_host_lease import api_routes
from test_hosted_auth import (
    API_HOST,
    CREDENTIALS_KEY_TEXT,
    Clock,
    KeyServer,
    child_environment,
    complete_hosted_environment,
    credentials_key,
    new_credentials_key_text,
    start_api,
)
from test_hosted_requests import Hosted, Server, copied_server, serving
from test_owner_policy import migrate_to

from harrier.connection import Connection, Cursor, StoreVersionError
from harrier.credentials import (
    CONTROL_CHARACTER,
    EMPTY,
    FIELD_MAX_BYTES,
    FIELDS,
    KEY_BYTES,
    KIND_FIELDS,
    KINDS,
    NOT_A_PROVIDER,
    NOT_UTF8,
    TOO_LONG,
    CredentialRefused,
    CredentialsKey,
    CredentialUnreadable,
    decrypt,
    parse_key,
    put_credential,
    stored_credentials,
    unknown_field_problem,
)
from harrier.db import connect
from harrier.pgstore import (
    migrate_postgres,
    open_postgres_store,
    postgres_connect,
    target_version,
)
from harrier_api.app import create_app, create_hosted_app
from harrier_api.credentials_routes import (
    LOCAL_TEXT,
    TELEGRAM_ELSEWHERE,
    UNKNOWN_KIND,
    AiCredentialIn,
    ApifyCredentialIn,
    HunterCredentialIn,
)
from harrier_api.deps import get_conn
from harrier_api.hostedauth import (
    CREDENTIALS_KEY_VARIABLE,
    HostedConfigError,
    hosted_config,
)

# Spec 116's refusal, as its Behavior writes it without the `error: ` prefix.
SPEC_116_REFUSAL = "HARRIER_CREDENTIALS_KEY must be <key id>:<base64 of 32 bytes>"


def secret(label: str = "value") -> str:
    """A submitted value no other text holds."""
    return f"synthetic-{label}-{uuid.uuid4().hex}"


def base64_forms(value: str) -> tuple[str, ...]:
    """`value` as base64 at each of the three byte alignments it can take
    inside a longer encoded text, in both alphabets, and as hex. At each
    alignment, the characters that also cover a neighbouring byte are cut
    off, so the form is found wherever the value was encoded with others."""
    raw = value.encode()
    forms: list[str] = [raw.hex()]
    for shift in range(3):
        encoded = base64.b64encode(bytes(shift) + raw).decode()
        inner = encoded[-(-8 * shift // 6) : -4]
        forms += [inner, inner.replace("+", "-").replace("/", "_")]
    return tuple(forms)


# --- the store, as two owners ---


@dataclass
class TwoOwners:
    url: str
    a: str
    b: str
    as_a: Connection
    as_b: Connection

    def every_column_as_text(self) -> str:
        """Every row of `credentials`, every column, as the migrating role
        reads it past the policy: as text, bytes in hex and escaped."""
        with closing(postgres_connect(self.url)) as admin:
            rows = admin.execute(
                b"SELECT row_to_json(c)::text, encode(nonce, 'escape'), "
                b"encode(ciphertext, 'escape'), ciphertext FROM credentials c"
            ).fetchall()
        assert rows, "nothing was stored"
        parts: list[str] = []
        for as_json, nonce, ciphertext, raw in rows:
            parts += [str(as_json), str(nonce), str(ciphertext)]
            assert isinstance(raw, bytes)
            parts.append(raw.decode("latin-1"))
        return "\n".join(parts)


@pytest.fixture
def owners(postgres_template: str) -> Iterator[TwoOwners]:
    with database_copy(postgres_template) as url:
        with closing(postgres_connect(url)) as admin:
            a, b = new_owner(admin), new_owner(admin)
        as_a = open_owner_store(url, a)
        as_b = open_owner_store(url, b)
        try:
            yield TwoOwners(url, a, b, as_a, as_b)
        finally:
            as_a.close()
            as_b.close()


def test_a_fresh_sqlite_store_has_no_credentials_table(tmp_path: Path) -> None:
    """Locally credentials stay in .env: the migration that creates the
    table on Postgres records its version on SQLite and changes nothing."""
    with closing(connect(tmp_path / "fresh.db")) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    assert "credentials" not in tables
    assert version is not None and int(version[0]) >= 18


def test_a_credential_round_trips_for_its_owner_only(owners: TwoOwners) -> None:
    key = credentials_key()
    written: dict[str, dict[str, str]] = {
        "ai": {"provider": "anthropic-api", "key": secret("ai"), "model": "synthetic-model"},
        "apify": {"token": secret("apify")},
        "hunter": {"key": secret("hunter")},
        "telegram": {"bot_token": secret("bot"), "chat_id": secret("chat")},
    }
    for kind, fields in written.items():
        # Stripped on the way in.
        padded = {name: f"  {value}\t" for name, value in fields.items()}
        put_credential(owners.as_a, key, owners.a, kind, padded)  # pyright: ignore[reportArgumentType]

    stored = stored_credentials(owners.as_a)
    assert [row.kind for row in stored] == sorted(KINDS)
    assert {row.kind: decrypt(key, owners.a, row) for row in stored} == written

    # Written again, it replaces the first, with a fresh nonce.
    first = {row.kind: row for row in stored}
    put_credential(owners.as_a, key, owners.a, "apify", {"token": "replaced-token"})
    again = {row.kind: row for row in stored_credentials(owners.as_a)}
    assert decrypt(key, owners.a, again["apify"]) == {"token": "replaced-token"}
    assert again["apify"].nonce != first["apify"].nonce

    # As B, A's rows are not read, and every kind reads unset.
    assert stored_credentials(owners.as_b) == []
    with owners.as_b.transaction():
        seen = owners.as_b.execute("SELECT count(*) FROM credentials").fetchone()
    assert seen is not None and seen[0] == 0
    # B's own write lands under B and leaves A's alone.
    put_credential(owners.as_b, key, owners.b, "hunter", {"key": "b-key"})
    assert [row.kind for row in stored_credentials(owners.as_b)] == ["hunter"]
    # A's rows read again after B's write: the same four, A's hunter key
    # untouched.
    after = {row.kind: row for row in stored_credentials(owners.as_a)}
    assert sorted(after) == sorted(KINDS)
    assert decrypt(key, owners.a, after["hunter"]) == written["hunter"]
    assert after["hunter"].ciphertext == again["hunter"].ciphertext


def test_a_refused_credential_writes_nothing_and_quotes_nothing(owners: TwoOwners) -> None:
    """The field rule in the domain, with no request model in front of it,
    as spec 109's Telegram route will write (local review of PR #310). Each
    refusal names the field and the rule, never the value or a field name
    the kind does not take, and writes no row."""
    value = secret("refused")
    cases: list[tuple[str, dict[str, str | None], list[tuple[str, str]]]] = [
        ("apify", {"token": " \t "}, [("token", EMPTY)]),
        ("hunter", {"key": value + "x" * FIELD_MAX_BYTES}, [("key", TOO_LONG)]),
        (
            "telegram",
            {"bot_token": value, "chat_id": f"{value}\n1"},
            [("chat_id", CONTROL_CHARACTER)],
        ),
        ("telegram", {"bot_token": f"{value}\ud800", "chat_id": "1"}, [("bot_token", NOT_UTF8)]),
        ("apify", {"token": "fine", value: "x"}, [(FIELDS, unknown_field_problem("apify"))]),
        ("ai", {"provider": value, "key": "fine"}, [("provider", NOT_A_PROVIDER)]),
        ("ai", {"key": "fine"}, [("provider", EMPTY)]),
    ]
    for kind, fields, problems in cases:
        with pytest.raises(CredentialRefused) as refused:
            put_credential(owners.as_a, credentials_key(), owners.a, kind, fields)  # pyright: ignore[reportArgumentType]
        assert refused.value.problems == problems, kind
        assert refused.value.__cause__ is None and refused.value.__context__ is None
        shown = " ".join((str(refused.value), repr(refused.value), repr(refused.value.problems)))
        assert_no_leak([shown], [value])
    assert stored_credentials(owners.as_a) == []


def test_a_store_that_skipped_a_declared_migration_is_refused(
    owners: TwoOwners, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration declared below the version a store already reached would
    never be applied: the runner applies only higher versions. A store that
    records the latest version without the one declared before it, as a
    store migrated to 18 before 17 was declared would, is refused by
    migrating, by the store's opener and by the hosted start, naming the
    version, and nothing runs out of order. A version declared with no
    statements is not counted. A store at the previous version, the order
    the runner expects, migrates on (spec 116, local review of PR #310)."""
    from harrier.tracker import schema

    target = target_version()
    real = list(schema.POSTGRES_MIGRATIONS)
    previous = max(version for version, _ in real if version < target)
    forget = f"DELETE FROM schema_version WHERE version = {previous}"
    run_as_migrator(owners.url, forget)
    run_as_migrator(server.url, forget)
    text = (
        f"the store is at version {target} but never applied migration {previous}, "
        "declared below it after the store passed it. harrier store migrate applies "
        "migrations in order only; renumber the migration above the store's version."
    )
    with pytest.raises(StoreVersionError) as refused:
        migrate_postgres(owners.url)
    assert str(refused.value) == text
    with pytest.raises(StoreVersionError) as opened:
        open_postgres_store(owners.url)
    assert str(opened.value) == text
    with closing(postgres_connect(owners.url)) as admin:
        rows = admin.execute(b"SELECT version FROM schema_version ORDER BY 1").fetchall()
    recorded = {int(str(row[0])) for row in rows}
    assert target in recorded and previous not in recorded, "a migration ran out of order"
    with (
        pytest.raises(StoreVersionError) as at_start,
        serving(monkeypatch, server.login_role().url),
    ):
        pass
    assert str(at_start.value) == text

    # Declared with no statements, for the other dialect: nothing to skip.
    emptied = [(v, [] if v == previous else statements) for v, statements in real]
    monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", emptied)
    assert migrate_postgres(owners.url) == (target, target)
    open_postgres_store(owners.url).close()
    monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", real)

    # A store at the previous version migrates to the latest: what it lacks
    # is above its own version, so nothing is skipped.
    with fresh_database() as url:
        migrate_to(url, previous, monkeypatch)
        assert migrate_postgres(url) == (previous, target)
        open_postgres_store(url).close()


def test_a_ciphertext_moved_to_another_owner_or_kind_does_not_decrypt(
    owners: TwoOwners,
) -> None:
    key = credentials_key()
    put_credential(owners.as_a, key, owners.a, "apify", {"token": secret("apify")})
    with closing(postgres_connect(owners.url)) as admin:
        # Past the policy, as a database-side attacker would: A's
        # ciphertext into B's row, and into A's hunter row.
        admin.execute(
            b"INSERT INTO credentials (owner_id, kind, key_id, nonce, ciphertext) "
            b"SELECT %s, kind, key_id, nonce, ciphertext FROM credentials "
            b"WHERE owner_id = %s AND kind = 'apify'",
            (owners.b, owners.a),
        )
        admin.execute(
            b"INSERT INTO credentials (owner_id, kind, key_id, nonce, ciphertext) "
            b"SELECT owner_id, 'hunter', key_id, nonce, ciphertext FROM credentials "
            b"WHERE owner_id = %s AND kind = 'apify'",
            (owners.a,),
        )

    (moved_to_b,) = stored_credentials(owners.as_b)
    with pytest.raises(CredentialUnreadable) as to_b:
        decrypt(key, owners.b, moved_to_b)
    assert str(to_b.value) == "your apify credential cannot be read; set it again"
    assert (to_b.value.kind, to_b.value.key_id) == ("apify", key.key_id)

    by_kind = {row.kind: row for row in stored_credentials(owners.as_a)}
    with pytest.raises(CredentialUnreadable, match="your hunter credential cannot be read"):
        decrypt(key, owners.a, by_kind["hunter"])
    # The original still decrypts where it was written.
    assert set(decrypt(key, owners.a, by_kind["apify"])) == {"token"}

    # The key replaced: another id and other bytes, the same id with other
    # bytes, or the same bytes under an id the row does not record.
    same_bytes = CREDENTIALS_KEY_TEXT.partition(":")[2]
    for replaced in (
        parse_key(new_credentials_key_text()),
        CredentialsKey(key.key_id, os.urandom(KEY_BYTES)),
        parse_key(f"another-{key.key_id}:{same_bytes}"),
    ):
        assert replaced is not None
        with pytest.raises(CredentialUnreadable) as unreadable:
            decrypt(replaced, owners.a, by_kind["apify"])
        assert unreadable.value.__cause__ is None
        assert unreadable.value.__context__ is None


def test_the_database_never_holds_a_credential_in_plain_text(owners: TwoOwners) -> None:
    values = {
        "ai": {"provider": "openai-api", "key": secret("ai")},
        "apify": {"token": secret("apify")},
        "hunter": {"key": secret("hunter")},
    }
    for kind, fields in values.items():
        put_credential(owners.as_a, credentials_key(), owners.a, kind, fields)  # pyright: ignore[reportArgumentType]
    stored = owners.every_column_as_text()
    for kind, fields in values.items():
        for name in KIND_FIELDS[kind].secret:  # pyright: ignore[reportArgumentType]
            value = fields[name]
            assert value not in stored, (kind, name)
            for form in base64_forms(value):
                assert form not in stored, (kind, name, form)


def test_the_telegram_chat_id_is_a_secret_field(owners: TwoOwners) -> None:
    """Decision D7: the chat id is secret everywhere, stored encrypted with
    the bot token in the one ciphertext."""
    assert KIND_FIELDS["telegram"].secret == ("bot_token", "chat_id")
    fields = {"bot_token": secret("bot"), "chat_id": secret("chat")}
    put_credential(owners.as_a, credentials_key(), owners.a, "telegram", fields)
    stored = owners.every_column_as_text()
    for value in fields.values():
        assert value not in stored
        for form in base64_forms(value):
            assert form not in stored
    (row,) = stored_credentials(owners.as_a)
    assert decrypt(credentials_key(), owners.a, row) == fields


# --- the routes, hosted ---


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture
def hosted(server: Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hosted]:
    with serving(monkeypatch, server.login_role().url) as built:
        yield built


def statuses(hosted: Hosted, owner: str) -> dict[str, tuple[bool, str | None]]:
    response = hosted.client().get("/credentials", headers=hosted.headers(owner))
    assert response.status_code == 200, response.text
    return {
        item["kind"]: (item["set"], item["updated_at"]) for item in response.json()["credentials"]
    }


def record_statements(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement any `Connection` runs from here on."""
    seen: list[str] = []
    real = Connection.execute

    def execute(self: Connection, sql: str, params: Sequence[object] = ()) -> Cursor:
        seen.append(sql)
        return real(self, sql, params)

    monkeypatch.setattr(Connection, "execute", execute)
    return seen


def assert_no_leak(texts: list[str], values: list[str]) -> None:
    for text in texts:
        for value in values:
            assert value not in text, value
            for form in base64_forms(value):
                assert form not in text, form


def test_credential_routes_never_return_a_value(
    server: Server, hosted: Hosted, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = hosted.client()
    a, b = server.owner(), server.owner()
    unset = dict.fromkeys(KINDS, (False, None))
    assert statuses(hosted, a) == unset

    submitted = {
        "ai": {"provider": "openai-api", "key": secret("ai"), "model": "synthetic-model"},
        "apify": {"token": secret("apify")},
        "hunter": {"key": secret("hunter")},
    }
    answers: list[str] = []
    for kind, body in submitted.items():
        response = client.put(f"/credentials/{kind}", json=body, headers=hosted.headers(a))
        assert response.status_code == 200, response.text
        answers.append(response.text)
        answer = response.json()
        assert set(answer) == {"kind", "set", "updated_at"}
        assert (answer["kind"], answer["set"]) == (kind, True)
        assert isinstance(answer["updated_at"], str)

    with pytest.MonkeyPatch.context() as patch:
        seen = record_statements(patch)
        listed = client.get("/credentials", headers=hosted.headers(a))
    answers.append(listed.text)
    by_kind = {item["kind"]: item for item in listed.json()["credentials"]}
    assert [item["kind"] for item in listed.json()["credentials"]] == list(KINDS)
    assert {kind: item["set"] for kind, item in by_kind.items()} == {
        "ai": True,
        "apify": True,
        "hunter": True,
        "telegram": False,
    }
    reads = [sql for sql in seen if "credentials" in sql]
    assert reads, "the list did not read the credentials table"
    assert not any("ciphertext" in sql or "SELECT *" in sql for sql in seen), seen

    # The PUT encrypted with the app's key, for the token's owner.
    as_a = open_owner_store(server.url, a)
    try:
        stored = {row.kind: decrypt(credentials_key(), a, row) for row in stored_credentials(as_a)}
    finally:
        as_a.close()
    assert stored == submitted

    # B sees none of A's.
    assert statuses(hosted, b) == unset

    # Delete answers unset, and again when nothing is stored.
    for _ in range(2):
        response = client.delete("/credentials/hunter", headers=hosted.headers(a))
        assert response.status_code == 200, response.text
        assert response.json() == {"kind": "hunter", "set": False, "updated_at": None}
    assert statuses(hosted, a)["hunter"] == (False, None)
    assert statuses(hosted, a)["apify"][0] is True

    values = [submitted["ai"]["key"], submitted["apify"]["token"], submitted["hunter"]["key"]]
    records = [record.getMessage() for record in caplog.records]
    assert_no_leak(answers + records, values)
    key_secret = CREDENTIALS_KEY_TEXT.partition(":")[2]
    assert all(key_secret not in text for text in answers + records)


def test_a_refused_credential_body_does_not_echo_the_value(
    server: Server, hosted: Hosted, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    client = hosted.client()
    owner = server.owner()
    value = secret("refused")
    cases: list[tuple[str, dict[str, Any], list[dict[str, str]]]] = [
        ("apify", {"token": "  \t "}, [{"field": "token", "problem": EMPTY}]),
        ("apify", {"token": f"{value}\nmore"}, [{"field": "token", "problem": CONTROL_CHARACTER}]),
        ("apify", {"token": f"{value}\x00"}, [{"field": "token", "problem": CONTROL_CHARACTER}]),
        ("hunter", {"key": value + "x" * FIELD_MAX_BYTES}, [{"field": "key", "problem": TOO_LONG}]),
        (
            "hunter",
            {"key": "fine", value: "x"},
            [{"field": FIELDS, "problem": unknown_field_problem("hunter")}],
        ),
        ("apify", {"token": f"{value}\ud800"}, [{"field": "token", "problem": NOT_UTF8}]),
        ("ai", {"provider": value, "key": "fine"}, [{"field": "provider", "problem": ""}]),
        (
            "ai",
            {"provider": "openai-api", "key": f"\x07{value}", "model": f"m\x1b{value}"},
            [
                {"field": "key", "problem": CONTROL_CHARACTER},
                {"field": "model", "problem": CONTROL_CHARACTER},
            ],
        ),
    ]
    answers: list[str] = []
    for kind, body, problems in cases:
        # Sent as ASCII JSON, so a lone surrogate travels as its escape.
        response = client.put(
            f"/credentials/{kind}",
            content=json.dumps(body).encode(),
            headers={**hosted.headers(owner), "Content-Type": "application/json"},
        )
        answers.append(response.text)
        assert response.status_code == 422, (kind, response.text)
        detail = response.json()["detail"]
        assert all(set(entry) == {"field", "problem"} for entry in detail), detail
        assert [entry["field"] for entry in detail] == [p["field"] for p in problems]
        for entry, problem in zip(detail, problems, strict=True):
            if problem["problem"]:
                assert entry["problem"] == problem["problem"]
            assert entry["problem"], entry
    # A body that is not JSON is refused under the field "body", with no
    # character offset and nothing of the text quoted.
    response = client.put(
        "/credentials/apify",
        content=f'{{"token": "{value}"'.encode(),
        headers={**hosted.headers(owner), "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    assert [entry["field"] for entry in response.json()["detail"]] == ["body"]
    answers.append(response.text)

    # Served under a root path, the route is the same route, and its 422 the
    # same: it is told by the matched route, not by the request's path.
    mounted = TestClient(hosted.app, base_url=f"http://{API_HOST}", root_path="/mounted")
    response = mounted.put(
        "/mounted/credentials/apify",
        content=json.dumps({"token": f"{value}\x00x"}).encode(),
        headers={**hosted.headers(owner), "Content-Type": "application/json"},
    )
    assert (response.status_code, response.json()) == (
        422,
        {"detail": [{"field": "token", "problem": CONTROL_CHARACTER}]},
    )
    answers.append(response.text)

    # A model that reaches a log record or an error by repr shows no secret.
    for model in (
        AiCredentialIn(provider="openai-api", key=value),
        ApifyCredentialIn(token=value),
        HunterCredentialIn(key=value),
    ):
        answers.append(repr(model))
        answers.append(str(model))

    # Nothing refused was written.
    assert statuses(hosted, owner) == dict.fromkeys(KINDS, (False, None))
    records = [record.getMessage() for record in caplog.records]
    records += [str(record.exc_text or "") for record in caplog.records]
    assert_no_leak(answers + records, [value])


def test_only_the_declared_kinds_are_set_here(server: Server, hosted: Hosted) -> None:
    client = hosted.client()
    owner = server.owner()
    bot = {"bot_token": secret("bot"), "chat_id": secret("chat")}
    refused = [
        ("PUT", "/credentials/telegram", bot, TELEGRAM_ELSEWHERE),
        ("PUT", "/credentials/gmail", {"token": secret()}, UNKNOWN_KIND),
        ("PUT", "/credentials/nothing-like-it", {}, UNKNOWN_KIND),
        ("DELETE", "/credentials/gmail", None, UNKNOWN_KIND),
        ("DELETE", "/credentials/nothing-like-it", None, UNKNOWN_KIND),
    ]
    for method, path, body, detail in refused:
        response = client.request(method, path, json=body, headers=hosted.headers(owner))
        assert (response.status_code, response.json()) == (404, {"detail": detail}), path
        for value in (body or {}).values():
            assert value not in response.text
    assert statuses(hosted, owner) == dict.fromkeys(KINDS, (False, None))


# --- the routes, locally ---


def credentials_routes(app: Any) -> list[APIRoute]:
    return [route for route in api_routes(app.routes) if route.path.startswith("/credentials")]


def test_credential_routes_are_not_served_locally() -> None:
    app = create_app()
    entered: list[str] = []

    def spy() -> Iterator[None]:
        entered.append("get_conn")
        yield None

    app.dependency_overrides[get_conn] = spy
    client = TestClient(app)
    walked: list[tuple[str, str]] = []
    for route in credentials_routes(app):
        for method in sorted(route.methods or ()):
            # The PUT that refuses other kinds is reached with one of them.
            filled = "telegram" if method == "PUT" else "apify"
            path = route.path.replace("{kind}", filled)
            # A valid body, an invalid one, none, and one that is not JSON,
            # which FastAPI reads before any dependency: always the local 404.
            for body in ({"token": secret()}, {"token": ""}, None, b'{"token": '):
                if isinstance(body, bytes):
                    response = client.request(
                        method,
                        path,
                        content=body,
                        headers={**auth(), "Content-Type": "application/json"},
                    )
                else:
                    response = client.request(method, path, json=body, headers=auth())
                assert (response.status_code, response.json()) == (404, {"detail": LOCAL_TEXT}), (
                    method,
                    path,
                    body,
                )
            walked.append((method, route.path))
    assert sorted(walked) == [
        ("DELETE", "/credentials/{kind}"),
        ("GET", "/credentials"),
        ("PUT", "/credentials/ai"),
        ("PUT", "/credentials/apify"),
        ("PUT", "/credentials/hunter"),
        ("PUT", "/credentials/{kind}"),
    ]
    assert entered == []
    # The same routes are in the one contract.
    paths = app.openapi()["paths"]
    for method, path in walked:
        assert method.lower() in paths[path], (method, path)


# --- the key, at start ---


def test_hosted_start_needs_a_well_formed_credentials_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    good = new_credentials_key_text()
    key_id, _, encoded = good.partition(":")
    raw = base64.b64decode(encoded)
    malformed = {
        "absent": None,
        "empty": "",
        "no key id": f":{encoded}",
        "no separator": encoded,
        "a key id with a space": f"key one:{encoded}",
        "not base64": f"{key_id}:{encoded[:-2]}!!",
        "31 bytes": f"{key_id}:{base64.b64encode(raw[:31]).decode()}",
        "33 bytes": f"{key_id}:{base64.b64encode(raw + b'x').decode()}",
        "url-safe base64": f"{key_id}:{base64.urlsafe_b64encode(b'\xfb' * 32).decode()}",
        "surrounding space": f" {good}",
    }
    for case, value in malformed.items():
        environ = dict(complete_hosted_environment())
        environ.pop(CREDENTIALS_KEY_VARIABLE)
        if value is not None:
            environ[CREDENTIALS_KEY_VARIABLE] = value
        with pytest.raises(HostedConfigError) as refused:
            hosted_config(environ)
        assert str(refused.value) == SPEC_116_REFUSAL, case
        assert CREDENTIALS_KEY_VARIABLE in str(refused.value)
        assert refused.value.__cause__ is None and refused.value.__context__ is None
        if value:
            assert value.strip() not in str(refused.value), case

    # As uvicorn starts it: the process exits 1, and prints no value.
    for case in ("absent", "31 bytes"):
        value = malformed[case]
        overrides = {**complete_hosted_environment()}
        overrides[CREDENTIALS_KEY_VARIABLE] = value or ""
        environ = child_environment(tmp_path / case.replace(" ", "-"), **overrides)
        if value is None:
            del environ[CREDENTIALS_KEY_VARIABLE]
        started = start_api(environ, tmp_path)
        assert started.returncode == 1, started.stderr
        last_line = started.stderr.strip().splitlines()[-1]
        assert last_line == f"harrier_api.hostedauth.HostedConfigError: {SPEC_116_REFUSAL}"
        # What the child was given: none of it, nor a long part of it, is
        # printed (test integrity review of PR #310).
        printed = started.stdout + started.stderr
        given = (value or "").partition(":")[2]
        for part in (given, given[:20], given[-20:]):
            assert not part or part not in printed, case

    # Well formed, the key is read, and its text form names its id only.
    config = hosted_config({**complete_hosted_environment(), CREDENTIALS_KEY_VARIABLE: good})
    assert config is not None
    assert config.credentials_key.key_id == key_id
    assert repr(config.credentials_key) == f"CredentialsKey(key_id={key_id!r})"
    assert str(config.credentials_key) == f"CredentialsKey(key_id={key_id!r})"
    forms = (encoded, raw.hex(), repr(raw), raw.decode("latin-1"), encoded[:20])
    for shown in (repr(config), str(config)):
        assert not any(form in shown for form in forms), shown

    # A hosted start that takes the key logs nothing of it. The key fetch
    # fails, so the start logs, and there is a record to read.
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    keys = KeyServer()
    keys.failing = True
    app = create_hosted_app(config, fetch_keys=keys, clock=Clock(), database_url=None)
    assert app.state.credentials_key is config.credentials_key
    records = [record.getMessage() for record in caplog.records]
    assert records, "the start logged nothing, so this proves nothing"
    assert not any(form in text for form in forms for text in records)

    # Locally the variable is not read at all.
    assert hosted_config({CREDENTIALS_KEY_VARIABLE: "not a key"}) is None
