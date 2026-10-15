"""`harrier operator invite` and `harrier operator erase` (spec 111).

The auth server and Storage are a fake behind an `httpx.MockTransport`, so
no test reaches the network. For `erase` the fake is backed by a throwaway
Postgres database with spec 105's shim: `auth.users` is the shim's table, so
a delete of a user who still owns a row is refused by the same foreign key
the real server meets. Every address, key and URL here is synthetic.
"""

from __future__ import annotations

import ast
import io
import json
import re
import uuid
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pg_support import (
    MIGRATOR_PASSWORD,
    MIGRATOR_ROLE,
    admin_url,
    fresh_database,
    new_owner,
    superuser_url,
    with_user,
)
from test_owner_policy import fill, migrate_to

from harrier import operator
from harrier.connection import (
    POSTGRES_NOT_YET,
    RUN_OWNER_VARIABLE,
    URL_VARIABLE,
    Connection,
    IdentityDidNotHold,
)
from harrier.db import data_dir
from harrier.operator import (
    ADDRESS_EXISTS,
    BUCKET,
    CONFIRM_MISMATCH,
    LATE_ROWS,
    NEEDS_SERVICE_ROLE,
    NOT_A_USER_ID,
    NOT_AN_ADDRESS,
    NOT_MIGRATED,
    OBJECTS_REMAIN,
    ROLE_DID_NOT_HOLD,
    SECRET_KEY_VARIABLE,
    SUPABASE_URL_VARIABLE,
    OperatorError,
    erase_rows,
    needs,
    secret_key_in_dotenv,
)
from harrier.pgstore import migrate_postgres
from harrier.tracker.schema import OWNED_TABLES
from harrier_cli import main as cli

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

PROJECT = "https://project.example.test"
SECRET = "sb_secret_synthetic0000"
ADDRESS = "invitee@example.test"
UNREACHABLE_URL = "postgresql://someone:pw-unused@127.0.0.1:1/nowhere"


# --- a fake Supabase ---


class FakeSupabase:
    """The auth server's admin routes and Storage's list route, as far as
    these commands call them. Records every call; `failures` answers a kind
    of call with a status, once each, in order; `listings` says whether each
    Storage listing finds an object (default: none)."""

    def __init__(self, db: PgConnection | None = None) -> None:
        self.db = db
        self.calls: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.failures: dict[str, list[int]] = {}
        self.listings: list[bool] = []
        self.addresses: dict[str, str] = {}
        self.before: dict[str, Callable[[str], None]] = {}
        self.events: list[str] = []
        # Ids `auth.users` holds that the auth server cannot see: rows a
        # test inserted by SQL with only an id, as spec 115's staging check
        # does. Every admin route answers 404 for them (spec 143).
        self.unknown: set[str] = set()

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def body(self, index: int = -1) -> Any:
        content = self.requests[index].content
        return json.loads(content) if content else None

    def of_kind(self, kind: str) -> list[httpx.Request]:
        return [r for r, (k, _) in zip(self.requests, self.calls, strict=True) if k == kind]

    def kind(self, request: httpx.Request) -> str:
        path = request.url.path
        if path == "/auth/v1/admin/users":
            return "create"
        if path.startswith("/auth/v1/admin/users/"):
            return {"GET": "get", "PUT": "ban", "DELETE": "delete"}[request.method]
        if path == f"/storage/v1/object/list/{BUCKET}":
            return "list"
        return "unknown"

    def handler(self, request: httpx.Request) -> httpx.Response:
        kind = self.kind(request)
        self.calls.append((kind, request.url.path))
        self.requests.append(request)
        self.events.append(kind)
        user = request.url.path.rsplit("/", 1)[-1]
        if kind in self.before:
            self.before[kind](user)
        status = self.failures.get(kind, []).pop(0) if self.failures.get(kind) else 0
        if status:
            return httpx.Response(status, json={"error_code": "unexpected_failure"})
        if kind == "create":
            address = json.loads(request.content)["email"]
            if address in self.addresses:
                return httpx.Response(
                    422, json={"error_code": "email_exists", "msg": "already registered"}
                )
            identifier = str(uuid.uuid4())
            self.addresses[address] = identifier
            if self.db is not None:
                self.db.execute("INSERT INTO auth.users (id) VALUES (%s)", (identifier,))
            return httpx.Response(200, json={"id": identifier, "email": address})
        if kind in ("get", "ban", "delete") and user in self.unknown:
            return httpx.Response(404, json={"error_code": "user_not_found"})
        if kind in ("get", "delete"):
            return self.user_call(kind, user)
        if kind == "ban":
            return httpx.Response(200, json={"id": user})
        if kind == "list":
            found = self.listings.pop(0) if self.listings else False
            return httpx.Response(200, json=[{"name": "folder", "id": None}] if found else [])
        return httpx.Response(404, json={})

    def user_call(self, kind: str, user: str) -> httpx.Response:
        import psycopg

        assert self.db is not None
        exists = self.db.execute("SELECT 1 FROM auth.users WHERE id = %s", (user,)).fetchone()
        if kind == "get":
            if exists is None:
                return httpx.Response(404, json={"error_code": "user_not_found"})
            return httpx.Response(200, json={"id": user})
        if exists is None:
            return httpx.Response(404, json={"error_code": "user_not_found"})
        try:
            self.db.execute("DELETE FROM auth.users WHERE id = %s", (user,))
        except psycopg.errors.ForeignKeyViolation:
            return httpx.Response(500, json={"error_code": "unexpected_failure"})
        return httpx.Response(200, json={})


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSupabase:
    server = FakeSupabase()
    monkeypatch.setattr(operator, "http_client", server.client)
    return server


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str = UNREACHABLE_URL) -> None:
    """The operator's shell: a Postgres URL, the project and its secret key,
    in a working directory with no `.env`."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(URL_VARIABLE, url)
    monkeypatch.setenv(SUPABASE_URL_VARIABLE, PROJECT)
    monkeypatch.setenv(SECRET_KEY_VARIABLE, SECRET)
    monkeypatch.delenv(RUN_OWNER_VARIABLE, raising=False)


def run(
    argv: list[str], capsys: pytest.CaptureFixture[str], stdin: str = ""
) -> tuple[int, str, str]:
    import sys

    real = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        code = cli.main(argv)
    finally:
        sys.stdin = real
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def never_postgres(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Any connection to a Postgres store is recorded and refused."""
    opened: list[str] = []

    def refuse(*_args: object, **_kwargs: object) -> object:
        opened.append("postgres_connect")
        raise AssertionError("a database connection was opened")

    monkeypatch.setattr("harrier.pgstore.postgres_connect", refuse)
    return opened


# --- invite ---


def test_invite_creates_a_confirmed_user_and_never_prints_the_address(
    fake: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The address is read from standard input and posted once to the admin
    user creation with `email_confirm` true and no password. The user's id is
    printed alone. The address and the key appear in no output, log record or
    argument, there is no link, and no database connection is opened."""
    configure(monkeypatch, tmp_path)
    opened = never_postgres(monkeypatch)
    caplog.set_level(0)

    code, out, err = run(["operator", "invite"], capsys, stdin=f"{ADDRESS}\n")

    assert code == 0 and err == ""
    created = fake.addresses[ADDRESS]
    assert out == f"{created}\n"
    assert fake.calls == [("create", "/auth/v1/admin/users")]
    request = fake.requests[0]
    assert request.method == "POST"
    assert fake.body() == {"email": ADDRESS, "email_confirm": True}
    assert request.headers["apikey"] == SECRET
    assert "authorization" not in request.headers
    assert str(request.url) == f"{PROJECT}/auth/v1/admin/users"
    assert [r.name for r in caplog.records if r.name.startswith(("httpx", "httpcore"))] == []
    assert ADDRESS not in (out + err + caplog.text + str(request.url))
    assert SECRET not in (out + err + caplog.text)
    assert opened == []
    # No link, whichever route a link would come from.
    assert all("generate_link" not in path and "invite" not in path for _, path in fake.calls)
    assert "redirect_to" not in json.dumps(fake.body())


def test_invite_refuses_an_existing_address(
    fake: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An address that has a user exits 1 with the fixed text, prints no user
    id and never the address."""
    configure(monkeypatch, tmp_path)
    fake.addresses[ADDRESS] = str(uuid.uuid4())
    code, out, err = run(["operator", "invite"], capsys, stdin=f"{ADDRESS}\n")
    assert (code, out, err) == (1, "", f"error: {ADDRESS_EXISTS}\n")
    assert fake.addresses[ADDRESS] not in out + err
    assert ADDRESS not in out + err


def test_invite_fails_without_quoting_the_server(
    fake: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Any other refusal names the status and nothing the server sent."""
    configure(monkeypatch, tmp_path)
    fake.failures["create"] = [500]
    code, out, err = run(["operator", "invite"], capsys, stdin=f"{ADDRESS}\n")
    assert (code, out) == (1, "")
    assert err == "error: the auth server answered 500\n"


def test_no_link_and_no_invitation_page_exist() -> None:
    """Decision D6: no `generate_link` call and no `redirect_to` in the
    operator module (read as names and strings of its syntax tree, so a
    comment does not count), and no page of the SPA is an invitation page."""
    source = Path(operator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    spelled: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            spelled.add(node.value)
        elif isinstance(node, ast.Name):
            spelled.add(node.id)
        elif isinstance(node, ast.Attribute):
            spelled.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            spelled.add(node.arg)
    for text in spelled:
        assert "generate_link" not in text and "redirect_to" not in text, text

    pages = Path(__file__).resolve().parents[3] / "apps" / "web" / "src" / "pages"
    assert pages.is_dir()
    assert [p.name for p in pages.iterdir() if "invit" in p.name.lower()] == []


# --- the refusals, before any call ---


@contextmanager
def a_login_that_cannot_act_as_service_role(url: str) -> Generator[str]:
    """The URL of a login role that is a member of nothing, so it cannot SET
    ROLE service_role. The role belongs to the server, so it is dropped after
    the database is."""
    import psycopg

    login = f"harrier_test_plain_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(superuser_url(url), autocommit=True) as admin:
        admin.execute(f"CREATE ROLE {login} LOGIN PASSWORD 'plain-password-xyz'".encode())
    try:
        yield with_user(url, login, "plain-password-xyz")
    finally:
        with psycopg.connect(admin_url(), autocommit=True) as admin:
            admin.execute(f"DROP ROLE IF EXISTS {login}".encode())


def test_operator_refusals_name_no_secret(
    fake: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing variables, a mismatched `--confirm`, an id that is an address,
    a login that cannot act as `service_role`, a store without the function,
    and the secret key defined in `.env` each exit 1 before any call, and no
    output contains the key, a URL's password or an address."""
    caplog.set_level(0)
    owner = str(uuid.uuid4())
    erase = ["operator", "erase", owner, "--confirm", owner]
    texts: list[str] = []

    def refused(argv: list[str], expected: str, stdin: str = "") -> None:
        code, out, err = run(argv, capsys, stdin)
        assert (code, out, err) == (1, "", f"error: {expected}\n"), argv
        texts.append(out + err)
        assert fake.calls == [], argv

    with fresh_database() as url:
        migrate_postgres(url)
        configure(monkeypatch, tmp_path, url)
        # Variables.
        monkeypatch.delenv(SUPABASE_URL_VARIABLE)
        refused(erase, needs(SUPABASE_URL_VARIABLE))
        refused(["operator", "invite"], needs(SUPABASE_URL_VARIABLE), f"{ADDRESS}\n")
        monkeypatch.setenv(SUPABASE_URL_VARIABLE, PROJECT)
        monkeypatch.delenv(SECRET_KEY_VARIABLE)
        refused(erase, needs(SECRET_KEY_VARIABLE))
        refused(["operator", "invite"], needs(SECRET_KEY_VARIABLE), f"{ADDRESS}\n")
        monkeypatch.setenv(SECRET_KEY_VARIABLE, SECRET)
        monkeypatch.setenv(SUPABASE_URL_VARIABLE, f"{PROJECT}/path?key={SECRET}")
        refused(erase, operator.SUPABASE_URL_REFUSAL)
        monkeypatch.setenv(SUPABASE_URL_VARIABLE, PROJECT)

        # The confirmation and the id.
        other = str(uuid.uuid4())
        refused(["operator", "erase", owner, "--confirm", other], CONFIRM_MISMATCH)
        refused(["operator", "erase", ADDRESS, "--confirm", ADDRESS], NOT_A_USER_ID)

        # The secret key in `.env`, whatever the environment holds.
        (tmp_path / ".env").write_text(f"{SECRET_KEY_VARIABLE}=sb_secret_fromfile0\n")
        monkeypatch.setenv(SECRET_KEY_VARIABLE, "")
        refused(erase, secret_key_in_dotenv())
        refused(["operator", "invite"], secret_key_in_dotenv(), f"{ADDRESS}\n")
        monkeypatch.setenv(SECRET_KEY_VARIABLE, SECRET)
        refused(erase, secret_key_in_dotenv())
        (tmp_path / ".env").unlink()

        # An address that is not one.
        refused(["operator", "invite"], NOT_AN_ADDRESS, "not-an-address-xyz\n")
        refused(["operator", "invite"], NOT_AN_ADDRESS, "")

        # A login that cannot act as service_role.
        with a_login_that_cannot_act_as_service_role(url) as plain:
            monkeypatch.setenv(URL_VARIABLE, plain)
            refused(erase, NEEDS_SERVICE_ROLE)
            assert "plain-password-xyz" not in "".join(texts)

    # A store that has not reached the erasure migration.
    with fresh_database() as url:
        migrate_to(url, 21, monkeypatch)
        monkeypatch.setenv(URL_VARIABLE, url)
        refused(erase, NOT_MIGRATED)

    everything = "".join(texts) + caplog.text
    for secret in (SECRET, "sb_secret_fromfile0", ADDRESS, "not-an-address-xyz", MIGRATOR_PASSWORD):
        assert secret not in everything, secret


def test_the_operator_group_runs_on_postgres_only(
    fake: FakeSupabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Beside SQLite both commands are refused with the operator text before
    anything is read or called; with `HARRIER_RUN_OWNER` set a run is not an
    operator and gets the gate's one text."""
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv(URL_VARIABLE, "")
    owner = str(uuid.uuid4())
    for argv, name in (
        (["operator", "invite"], "operator invite"),
        (["operator", "erase", owner, "--confirm", owner], "operator erase"),
    ):
        code, out, err = run(argv, capsys, stdin=f"{ADDRESS}\n")
        assert (code, out) == (1, "")
        assert err == (
            f"error: harrier {name} runs only against a hosted store; "
            "locally the schedule is launchd (harrier schedule)\n"
        )
    monkeypatch.setenv(URL_VARIABLE, UNREACHABLE_URL)
    monkeypatch.setenv(RUN_OWNER_VARIABLE, owner)
    for argv in (["operator", "invite"], ["operator", "erase", owner, "--confirm", owner]):
        code, out, err = run(argv, capsys, stdin=f"{ADDRESS}\n")
        assert (code, out, err) == (1, "", f"error: {POSTGRES_NOT_YET}\n")
    assert fake.calls == []


# --- erase ---


class Erasure:
    """A shimmed, migrated store with two owners whose rows fill every owned
    table, a fake Supabase over it, and the operator's shell."""

    def __init__(self, url: str, db: PgConnection, fake: FakeSupabase, tmp_path: Path) -> None:
        self.url = url
        self.db = db
        self.fake = fake
        self.tmp_path = tmp_path
        self.a = new_owner(db)
        self.b = new_owner(db)
        fill(db, self.a)
        fill(db, self.b)

    def argv(self) -> list[str]:
        return ["operator", "erase", self.a, "--confirm", self.a]

    def rows(self, owner: str) -> int:
        total = 0
        for table in OWNED_TABLES:
            found = self.db.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id = %s".encode(), (owner,)
            ).fetchone()
            assert found is not None
            total += int(found[0])
        return total

    def user_exists(self, owner: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM auth.users WHERE id = %s", (owner,)).fetchone()
            is not None
        )

    def ledger(self) -> list[dict[str, Any]]:
        path = data_dir() / "hosted" / "erasures.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def erasure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Erasure]:
    import psycopg

    with fresh_database() as url:
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as db:
            server = FakeSupabase(db)
            monkeypatch.setattr(operator, "http_client", server.client)
            configure(monkeypatch, tmp_path, url)
            yield Erasure(url, db, server, tmp_path)


def step_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("step ")]


def test_erase_runs_its_steps_in_order(
    erasure: Erasure, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    """Ban, Storage, rows, auth delete, stragglers and the ledger, in that
    order. At the moment of each call the fake reads what has been done: the
    ban comes while the rows exist, the delete after the rows are gone, the
    second listing after the user is. One ledger line, with no address."""
    a, b = erasure.a, erasure.b
    seen: list[tuple[str, int, bool]] = []
    for kind in ("get", "ban", "list", "delete"):
        erasure.fake.before[kind] = lambda _user, kind=kind: seen.append(
            (kind, erasure.rows(a), erasure.user_exists(a))
        )
    b_rows = erasure.rows(b)
    before = erasure.rows(a)
    caplog.set_level(0)

    code, out, err = run(erasure.argv(), capsys)
    # The HTTP client's request lines name the user id; none reaches a log.
    assert [r.name for r in caplog.records if r.name.startswith(("httpx", "httpcore"))] == []

    assert (code, err) == (0, "")
    assert erasure.fake.events == ["get", "ban", "list", "delete", "list"]
    assert seen == [
        ("get", before, True),
        ("ban", before, True),
        ("list", before, True),
        ("delete", 0, True),
        ("list", 0, False),
    ]
    ban = erasure.fake.of_kind("ban")[0]
    assert json.loads(ban.content) == {"ban_duration": "876000h"}
    assert json.loads(erasure.fake.of_kind("delete")[0].content) == {"should_soft_delete": False}
    for listing in erasure.fake.of_kind("list"):
        assert json.loads(listing.content) == {"prefix": a, "limit": 1, "offset": 0}
        assert listing.headers["apikey"] == SECRET and "authorization" not in listing.headers
        assert listing.method == "POST"
    # Nothing in Storage is deleted: no call but those four kinds, and none
    # of them a delete of an object.
    assert {kind for kind, _ in erasure.fake.calls} == {"get", "ban", "list", "delete"}

    lines = step_lines(out)
    assert [line.split(":")[0] for line in lines if not line.startswith("step 3")] == [
        "step 1 ban",
        "step 2 storage",
        "step 4 auth user",
        "step 5 stragglers",
        "step 6 ledger",
    ]
    tables = [line.split()[3] for line in lines if line.startswith("step 3 rows:")]
    assert tables == list(OWNED_TABLES)
    assert lines.index("step 4 auth user: deleted") > max(
        i for i, line in enumerate(lines) if line.startswith("step 3")
    )
    assert tables.index("job_eligibility") < tables.index("job_events")
    assert erasure.rows(a) == 0 and erasure.rows(b) == b_rows
    assert not erasure.user_exists(a) and erasure.user_exists(b)

    ledger = erasure.ledger()
    assert len(ledger) == 1
    assert set(ledger[0]) == {"user_id", "at", "completed", "complete"}
    assert ledger[0]["user_id"] == a and ledger[0]["complete"] is True
    assert ledger[0]["completed"] == ["ban", "storage", "rows", "auth", "stragglers", "ledger"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ledger[0]["at"])
    assert "@" not in json.dumps(ledger)


def test_erase_resumes_after_a_failed_step(
    erasure: Erasure, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at each step exits 1 naming it, and a re-run with the same id
    completes the rest. Before step 3 nothing is deleted; after it, a re-run
    finds nothing left to delete and does not repeat the failed call's work."""
    import psycopg

    a, fake = erasure.a, erasure.fake
    all_rows = erasure.rows(a)

    # Step 1: the ban is refused. Nothing else was called, nothing deleted.
    fake.failures["ban"] = [500]
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, "error: step 1 (ban): the server answered 500\n")
    assert erasure.rows(a) == all_rows and fake.events == ["get", "ban"]

    # Step 2: Storage cannot be read, then it holds an object. Rows untouched.
    fake.events.clear()
    fake.failures["list"] = [503]
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, "error: step 2 (storage): the server answered 503\n")
    assert erasure.rows(a) == all_rows
    fake.listings = [True]
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, f"error: step 2 (storage): {OBJECTS_REMAIN}\n")
    assert erasure.rows(a) == all_rows and erasure.user_exists(a)
    assert "delete" not in fake.events and erasure.ledger() == []

    # Step 3: a row lock is held past the lock timeout. Nothing deleted.
    monkeypatch.setattr("harrier.db.BUSY_TIMEOUT_MS", 150)
    holder = psycopg.connect(erasure.url, autocommit=True)
    try:
        with holder.transaction():
            holder.execute("SELECT id FROM jobs WHERE owner_id = %s FOR UPDATE", (a,))
            code, out, err = run(erasure.argv(), capsys)
        assert code == 1 and err.startswith("error: step 3 (rows): ")
        assert erasure.rows(a) == all_rows and erasure.ledger() == []
    finally:
        holder.close()

    # Step 4: the auth delete is refused for another reason. The rows are
    # gone and the ledger says so, incomplete.
    fake.failures["delete"] = [500]
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, "error: step 4 (auth): the server answered 500\n")
    assert erasure.rows(a) == 0 and erasure.user_exists(a)
    assert [line["complete"] for line in erasure.ledger()] == [False]
    assert erasure.ledger()[0]["completed"] == ["ban", "storage", "rows"]

    # Step 5: an object appears after the delete. The user is gone.
    fake.listings = [False, True]
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, f"error: step 5 (stragglers): {OBJECTS_REMAIN}\n")
    assert not erasure.user_exists(a)
    assert [line["complete"] for line in erasure.ledger()] == [False, False]

    # A re-run: the user is gone, so steps 1, 3 and 4 are skipped, and the
    # rest completes.
    fake.events.clear()
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (0, "")
    assert fake.events == ["get", "list", "list"]
    assert "step 1 ban: skipped" in out and "step 3 rows: skipped" in out
    assert "step 4 auth user: skipped" in out
    assert erasure.ledger()[-1]["complete"] is True


def test_this_build_deletes_no_storage_object(
    erasure: Erasure, capsys: pytest.CaptureFixture[str]
) -> None:
    """Until spec 107's Storage seam exists (spec 111, Amendment
    (2026-10-15)) the erase only lists the owner's prefix. Run with an object
    listed and without, it makes no Storage call but that list, and the list
    has the documented shape. The module names no `storage.objects` and no
    Storage route but the list. Spec 107's build replaces the check with the
    delete and updates this test."""
    a, fake = erasure.a, erasure.fake
    fake.listings = [True]
    code, _, err = run(erasure.argv(), capsys)
    assert code == 1 and OBJECTS_REMAIN in err
    # The listing stopped it: no row call, no auth delete.
    assert fake.events == ["get", "ban", "list"]
    assert erasure.user_exists(a)
    fake.events.clear()
    code, _, err = run(erasure.argv(), capsys)
    assert (code, err) == (0, "")
    storage = [r for r in fake.requests if r.url.path.startswith("/storage/")]
    assert storage
    for request in storage:
        assert request.method == "POST"
        assert request.url.path == f"/storage/v1/object/list/{BUCKET}"
        assert json.loads(request.content) == {"prefix": a, "limit": 1, "offset": 0}
        assert request.headers["apikey"] == SECRET

    tree = ast.parse(Path(operator.__file__).read_text(encoding="utf-8"))
    strings = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert not [text for text in strings if "storage.objects" in text.lower()]
    assert {text for text in strings if text.startswith("/storage")} == {"/storage/v1/object/list/"}


def test_a_rerun_after_the_rows_are_gone_deletes_none(
    erasure: Erasure, capsys: pytest.CaptureFixture[str]
) -> None:
    """A re-run after a partial erase (rows gone, user still there) deletes
    zero rows in step 3, then deletes the user: each step is a no-op on what
    is already gone."""
    fake = erasure.fake
    fake.failures["delete"] = [500]
    assert run(erasure.argv(), capsys)[0] == 1
    code, out, err = run(erasure.argv(), capsys)
    assert (code, err) == (0, "")
    counts = [int(line.split()[4]) for line in step_lines(out) if line.startswith("step 3 rows:")]
    assert counts == [0] * len(OWNED_TABLES)
    assert not erasure.user_exists(erasure.a)


def test_a_ledger_that_cannot_be_written_fails_step_six(
    erasure: Erasure, capsys: pytest.CaptureFixture[str]
) -> None:
    """The erase is done, but the line a restore depends on is not written:
    exit 1 naming the ledger, so the operator notes the id by hand."""
    hosted = data_dir() / "hosted"
    hosted.parent.mkdir(parents=True, exist_ok=True)
    hosted.write_text("a file where the ledger's directory belongs")
    code, _, err = run(erasure.argv(), capsys)
    assert code == 1
    assert err == "error: step 6 (ledger): the ledger line could not be written\n"
    assert erasure.rows(erasure.a) == 0


def test_erase_reports_rows_written_after_it(
    erasure: Erasure, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row written after step 3 (a contact, which references no track) makes
    the foreign key refuse the auth delete. The command exits 1 with the "run
    it again" text, the late row is erased, and a re-run completes."""
    a = erasure.a

    def write_late(_user: str) -> None:
        erasure.db.execute("INSERT INTO contacts (owner_id) VALUES (%s)", (a,))

    erasure.fake.before["delete"] = write_late
    code, _, err = run(erasure.argv(), capsys)
    assert (code, err) == (1, f"error: step 4 (auth): {LATE_ROWS}\n")
    assert erasure.user_exists(a)
    assert erasure.rows(a) == 0
    assert [line["complete"] for line in erasure.ledger()] == [False]

    erasure.fake.before.clear()
    code, _, err = run(erasure.argv(), capsys)
    assert (code, err) == (0, "")
    assert not erasure.user_exists(a)


def test_erase_of_an_id_with_no_user_still_clears_storage_and_writes_the_ledger(
    erasure: Erasure, capsys: pytest.CaptureFixture[str]
) -> None:
    """Steps 1, 3 and 4 are skipped, Storage is still listed twice, and the
    ledger records the id."""
    ghost = str(uuid.uuid4())
    code, _, err = run(["operator", "erase", ghost, "--confirm", ghost], capsys)
    assert (code, err) == (0, "")
    assert erasure.fake.events == ["get", "list", "list"]
    assert erasure.ledger()[0]["user_id"] == ghost


# --- the role the rows run as ---


class StubCursor:
    def __init__(self, role: str) -> None:
        self.role = role

    def fetchone(self) -> tuple[str]:
        return (self.role,)

    def fetchall(self) -> list[tuple[str, int]]:
        return []


class StubConnection:
    """What `erase_rows` is given: a connection whose transaction never took
    the role it asked for. Records every statement."""

    def __init__(self, role: str) -> None:
        self.role = role
        self.statements: list[str] = []
        self.transactions: list[dict[str, object]] = []

    @contextmanager
    def transaction(self, **options: object) -> Generator[None]:
        self.transactions.append(options)
        yield

    def execute(self, sql: str, params: object = ()) -> StubCursor:
        self.statements.append(sql)
        return StubCursor(self.role)


def test_erase_refuses_a_transaction_that_did_not_act_as_service_role() -> None:
    """The role is read back before the function runs. A transaction that
    reads another role raises, and `harrier_erase_owner` is never called."""
    stub = StubConnection("harrier_migrator")
    with pytest.raises(OperatorError) as refused:
        erase_rows(stub, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert refused.value.message == ROLE_DID_NOT_HOLD
    assert stub.transactions == [{"service_role": True}]
    assert not any("harrier_erase_owner" in statement for statement in stub.statements)

    good = StubConnection("service_role")
    assert erase_rows(good, str(uuid.uuid4())) == []  # type: ignore[arg-type]
    assert any("harrier_erase_owner" in statement for statement in good.statements)


def test_the_seam_takes_service_role_for_one_transaction(erasure: Erasure) -> None:
    """`transaction(service_role=True)` runs as `service_role` on a connection
    without claims, and is refused together with another role, nested, or on
    a connection with an owner's claims."""
    from harrier.pgstore import postgres_connect

    conn = Connection.postgres(postgres_connect(erasure.url))
    try:
        with conn.transaction(service_role=True):
            row = conn.execute("SELECT current_user::text").fetchone()
            assert row is not None and row[0] == "service_role"
        row = conn.execute("SELECT current_user::text").fetchone()
        assert row is not None and row[0] == MIGRATOR_ROLE
        # A role that moved inside the block is found before the commit.
        with (
            pytest.raises(IdentityDidNotHold),
            conn.transaction(service_role=True),
        ):
            conn.execute(f"SELECT set_config('role', '{MIGRATOR_ROLE}', true)")
        with (
            pytest.raises(ValueError, match="one role"),
            conn.transaction(service_role=True, tenant=True),
        ):
            pass
        with (
            pytest.raises(ValueError, match="one role"),
            conn.transaction(service_role=True, schedule_reader=True),
        ):
            pass
        with conn.transaction(), pytest.raises(ValueError), conn.transaction(service_role=True):
            pass
    finally:
        conn.close()
    owned = Connection.postgres(postgres_connect(erasure.url), claims={"sub": erasure.a})
    try:
        with (
            pytest.raises(ValueError, match="without claims"),
            owned.transaction(service_role=True),
        ):
            pass
    finally:
        owned.close()
