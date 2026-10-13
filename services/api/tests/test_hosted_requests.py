"""Each hosted request runs as its user (spec 134): the request transaction.

`create_app` builds the hosted app here as uvicorn builds it, from the
process environment, on a copy of the migrated template
(`postgres_template`, spec 126). It connects as a login role made for the
test: `NOINHERIT`, a member of `harrier_tenant` alone, with a password made
at run time. Keys and tokens are made here too and the key fetcher is
replaced, so no test reaches the network. Every role a test makes is dropped
after it, so the server's roles are the same before and after the run.

The probe routes below are added to the built app for these tests only:
each reads or writes through `Conn`, as every route does.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pg_support import (
    admin_url,
    database_copy,
    new_owner,
    run_as_migrator,
    superuser_url,
    with_user,
)
from test_hosted_auth import (
    API_HOST,
    KID,
    SPA_ORIGIN,
    SUPABASE_URL,
    KeyServer,
    bearer,
    claims_for,
    mint,
    new_ec_key,
)

from harrier.connection import (
    IDENTITY_DID_NOT_HOLD,
    URL_VARIABLE,
    StoreBusyError,
    StoreDatabaseError,
    open_store,
)
from harrier.pgstore import postgres_connect
from harrier.tracker.schema import TENANT_ROLE
from harrier.tracks import add_track
from harrier_api import app as app_module
from harrier_api import hostedauth, hostedstore
from harrier_api.app import create_app
from harrier_api.deps import Conn
from harrier_api.hostedstore import (
    COULD_NOT_RUN_AS_USER,
    REQUEST_ENDED,
    REQUEST_IDLE_TIMEOUT_MS,
    REQUEST_LOCK_TIMEOUT_MS,
    REQUEST_STATEMENT_TIMEOUT_MS,
    RequestStore,
)

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

GENERIC = {"detail": COULD_NOT_RUN_AS_USER}

# --- the server: a copy of the template, and the roles a test makes ---


@dataclass
class LoginRole:
    name: str
    password: str
    url: str


@dataclass
class Server:
    """One test's copy of the migrated template, as the migrating role, and
    the roles the test made on the server, dropped after it."""

    url: str
    roles: list[str] = field(default_factory=lambda: [])

    @contextmanager
    def superuser(self) -> Generator[PgConnection]:
        """The server's superuser on this copy: only it may make a role a
        superuser or give it BYPASSRLS."""
        import psycopg

        with psycopg.connect(superuser_url(self.url), autocommit=True) as conn:
            yield conn

    def role(self, attributes: str = "NOLOGIN") -> str:
        name = f"harrier_t_{uuid.uuid4().hex[:12]}"
        self.roles.append(name)
        with self.superuser() as conn:
            conn.execute(f"CREATE ROLE {name} {attributes}".encode())
        return name

    def login_role(self, attributes: str = "", *, tenant: str = "") -> LoginRole:
        """A login role, `NOINHERIT`, granted `harrier_tenant` with `tenant`
        as the grant's options, or not granted it when `tenant` is `none`."""
        password = uuid.uuid4().hex
        name = self.role(f"LOGIN NOINHERIT {attributes} PASSWORD '{password}'")
        if tenant != "none":
            options = f" WITH {tenant}" if tenant else ""
            self.sql(f"GRANT {TENANT_ROLE} TO {name}{options}")
        return LoginRole(name, password, with_user(self.url, name, password))

    def sql(self, *statements: str) -> None:
        """As the superuser, on this copy."""
        with self.superuser() as conn:
            for statement in statements:
                conn.execute(statement.encode())

    def migrator(self, *statements: str) -> None:
        run_as_migrator(self.url, *statements)

    def one(self, statement: str) -> Any:
        with closing(postgres_connect(self.url)) as conn:
            row = conn.execute(statement.encode()).fetchone()
        assert row is not None
        return row[0]

    def owner(self) -> str:
        with closing(postgres_connect(self.url)) as conn:
            return new_owner(conn)


def drop_roles(roles: list[str]) -> None:
    import psycopg

    with psycopg.connect(admin_url(), autocommit=True) as conn:
        for name in reversed(roles):
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = %s",
                (name,),
            )
            conn.execute(f"DROP ROLE IF EXISTS {name}".encode())


def copied_server(template: str) -> Iterator[Server]:
    """A copy of the template for one test; its roles go with it."""
    roles: list[str] = []
    try:
        with database_copy(template) as url:
            yield Server(url, roles)
    finally:
        drop_roles(roles)


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


# --- the probes, created on the copy as the migrating role ---

PROBE_ROWS = "public.harrier_probe_rows"
PROBE_SEQUENCE = "public.harrier_probe_seq"
PROBE_DEFERRED = "public.harrier_probe_deferred"


def create_probes(server: Server) -> None:
    server.migrator(
        f"CREATE TABLE {PROBE_ROWS} (body text)",
        f"GRANT SELECT, INSERT ON {PROBE_ROWS} TO {TENANT_ROLE}",
        # A sequence is not transactional: a nextval that ran shows after a
        # rollback, so it proves a statement never ran.
        f"CREATE SEQUENCE {PROBE_SEQUENCE}",
        f"GRANT USAGE ON SEQUENCE {PROBE_SEQUENCE} TO {TENANT_ROLE}",
        # A row here is refused at commit, not when it is written.
        f"CREATE TABLE {PROBE_DEFERRED} (body text)",
        f"GRANT INSERT ON {PROBE_DEFERRED} TO {TENANT_ROLE}",
        "CREATE FUNCTION public.harrier_probe_refuse() RETURNS trigger LANGUAGE plpgsql "
        "AS $$ BEGIN RAISE EXCEPTION 'refused at commit'; END $$",
        f"CREATE CONSTRAINT TRIGGER harrier_probe_at_commit AFTER INSERT ON {PROBE_DEFERRED} "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.harrier_probe_refuse()",
    )


def probe_rows(server: Server) -> list[str]:
    with closing(postgres_connect(server.url)) as conn:
        rows = conn.execute(f"SELECT body FROM {PROBE_ROWS} ORDER BY body".encode()).fetchall()
    return [str(row[0]) for row in rows]


def sequence_ran(server: Server) -> bool:
    return bool(server.one(f"SELECT is_called FROM {PROBE_SEQUENCE}"))


@dataclass
class Probes:
    """What the probe routes saw, and the barrier two of them meet at."""

    entered: list[str] = field(default_factory=lambda: [])
    barrier: threading.Barrier = field(default_factory=lambda: threading.Barrier(2, timeout=20))


def add_probe_routes(app: FastAPI) -> Probes:
    probes = Probes()

    def identity(conn: Conn) -> dict[str, Any]:
        probes.entered.append("identity")
        row = conn.execute(
            "SELECT current_user::text, public.harrier_owner_id()::text, "
            "current_setting('request.jwt.claims', true), "
            "current_setting('transaction_isolation'), r.rolbypassrls, pg_backend_pid() "
            "FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
        ).fetchone()
        settings = dict(
            conn.execute(
                "SELECT name, setting FROM pg_catalog.pg_settings WHERE name IN "
                "('lock_timeout', 'statement_timeout', 'idle_in_transaction_session_timeout')"
            ).fetchall()
        )
        return {
            "role": row[0],
            "owner": row[1],
            "claims": json.loads(row[2]) if row[2] else None,
            "isolation": row[3],
            "bypassrls": row[4],
            "pid": row[5],
            "settings": settings,
        }

    def sequence(conn: Conn) -> dict[str, Any]:
        probes.entered.append("sequence")
        conn.execute(f"SELECT nextval('{PROBE_SEQUENCE}')")
        return {}

    def write(conn: Conn, body: str, then: str) -> Any:
        conn.execute(f"INSERT INTO {PROBE_ROWS} (body) VALUES (?)", (body,))
        if then == "raise":
            raise RuntimeError("the route failed after its write")
        if then == "conflict":
            # Returned, not raised, as `POST /capture/add` answers 409.
            return JSONResponse(status_code=409, content={"detail": "refused"})
        if then == "failed statement":
            # A statement fails at the top of the request's transaction and
            # the route answers 200 anyway: COMMIT there rolls back.
            with suppress(StoreDatabaseError):
                conn.execute("SELECT 1 / 0")
        if then == "deferred":
            conn.execute(f"INSERT INTO {PROBE_DEFERRED} (body) VALUES (?)", (body,))
        return {"written": body}

    def plain_set(conn: Conn) -> dict[str, Any]:
        for name in ("lock_timeout", "statement_timeout", "idle_in_transaction_session_timeout"):
            conn.execute(f"SET {name} = 0")
        row = conn.execute("SELECT pg_backend_pid()").fetchone()
        return {"pid": row[0]}

    def locked(conn: Conn, body: str) -> dict[str, Any]:
        conn.execute(f"INSERT INTO {PROBE_ROWS} (body) VALUES (?)", (body,))
        conn.execute("SELECT count(*) FROM jobs").fetchone()
        return {}

    def idle(conn: Conn, seconds: float) -> dict[str, Any]:
        row = conn.execute("SELECT pg_backend_pid()").fetchone()
        probes.entered.append(f"idle {row[0]}")
        time.sleep(seconds)
        return {"pid": row[0]}

    def track(conn: Conn, slug: str) -> dict[str, Any]:
        probes.barrier.wait()
        return {"id": add_track(conn, slug, "academic", f"Synthetic {slug}").id}

    def together(conn: Conn) -> dict[str, Any]:
        first = conn.execute("SELECT public.harrier_owner_id()::text").fetchone()
        probes.barrier.wait()
        second = conn.execute("SELECT public.harrier_owner_id()::text").fetchone()
        return {"owners": [first[0], second[0]]}

    def stream(conn: Conn) -> StreamingResponse:
        def body() -> Iterator[bytes]:
            yield b"first "
            conn.execute("SELECT 1")
            yield b"second"

        return StreamingResponse(body(), media_type="text/plain")

    app.add_api_route("/probe/identity", identity, methods=["GET"])
    app.add_api_route("/probe/sequence", sequence, methods=["GET"])
    app.add_api_route("/probe/write", write, methods=["POST"])
    app.add_api_route("/probe/plain-set", plain_set, methods=["POST"])
    app.add_api_route("/probe/locked", locked, methods=["POST"])
    app.add_api_route("/probe/idle", idle, methods=["GET"])
    app.add_api_route("/probe/track", track, methods=["POST"])
    app.add_api_route("/probe/together", together, methods=["GET"])
    app.add_api_route("/probe/stream", stream, methods=["GET"])
    return probes


# --- the hosted app, as uvicorn builds it ---


@dataclass
class Hosted:
    app: FastAPI
    store: RequestStore
    key: ec.EllipticCurvePrivateKey
    probes: Probes

    def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
        return TestClient(
            self.app,
            base_url=f"http://{API_HOST}",
            raise_server_exceptions=raise_server_exceptions,
        )

    def headers(self, owner: str, **overrides: object) -> dict[str, str]:
        return bearer(mint(self.key, claims_for(sub=owner, **overrides)))


def hosted_environment(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """Complete hosted mode in the process environment, as on Fly."""
    for name, value in list(os.environ.items()):
        if name == "SUPABASE_SERVICE_ROLE_KEY" or value.strip().startswith("sb_secret_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HARRIER_AUTH", "supabase")
    monkeypatch.setenv("HARRIER_SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("HARRIER_ALLOWED_HOSTS", API_HOST)
    monkeypatch.setenv("HARRIER_CORS_ORIGINS", SPA_ORIGIN)
    monkeypatch.setenv(URL_VARIABLE, database_url)


def keys_served_here(monkeypatch: pytest.MonkeyPatch) -> ec.EllipticCurvePrivateKey:
    """The project's key endpoint, served from memory: `create_app` fetches
    the keys through `fetch_from`, which is replaced."""
    server = KeyServer()
    key = new_ec_key()
    server.publish(KID, key)

    def fetch_from(_url: str) -> KeyServer:
        return server

    monkeypatch.setattr(app_module, "fetch_from", fetch_from)
    return key


@contextmanager
def serving(monkeypatch: pytest.MonkeyPatch, database_url: str) -> Generator[Hosted]:
    key = keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, database_url)
    app = create_app()
    store = app.state.request_store
    assert isinstance(store, RequestStore)
    try:
        yield Hosted(app, store, key, add_probe_routes(app))
    finally:
        store.close()


@pytest.fixture
def hosted(server: Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Hosted]:
    create_probes(server)
    with serving(monkeypatch, server.login_role().url) as built:
        yield built


def one_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pool of one, so consecutive requests share a connection."""
    monkeypatch.setattr(hostedstore, "POOL_MAX_SIZE", 1)


# --- the request transaction ---


def test_the_request_transaction_runs_as_its_user(server: Server, hosted: Hosted) -> None:
    owner = server.owner()
    claims = claims_for(sub=owner)

    response = hosted.client().get("/probe/identity", headers=bearer(mint(hosted.key, claims)))

    assert response.status_code == 200, response.text
    seen = response.json()
    assert seen["role"] == TENANT_ROLE
    assert seen["owner"] == owner
    # The whole verified payload, exactly as signed.
    assert seen["claims"] == claims


def test_a_request_never_runs_as_a_bypassrls_role(server: Server, hosted: Hosted) -> None:
    response = hosted.client().get("/probe/identity", headers=hosted.headers(server.owner()))
    assert response.status_code == 200, response.text
    assert (response.json()["role"], response.json()["bypassrls"]) == (TENANT_ROLE, False)


def test_identity_ends_with_the_request(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    create_probes(server)
    one_connection(monkeypatch)
    login = server.login_role()
    read = (
        "SELECT current_user::text, current_setting('request.jwt.claims', true), "
        "pg_backend_pid(), current_setting('lock_timeout'), "
        "current_setting('statement_timeout'), "
        "current_setting('idle_in_transaction_session_timeout')"
    )
    with serving(monkeypatch, login.url) as hosted:
        driver = hosted.store.pool.lease()
        try:
            before = driver.execute(read).fetchone()
        finally:
            hosted.store.pool.release(driver)
        response = hosted.client().get("/probe/identity", headers=hosted.headers(server.owner()))
        assert response.status_code == 200, response.text
        driver = hosted.store.pool.lease()
        try:
            after = driver.execute(read).fetchone()
        finally:
            hosted.store.pool.release(driver)
    assert before is not None and after is not None
    # The same pooled connection, back to the login role with no claims,
    # and with the timeouts it had before the request: all of them were
    # the request's alone.
    assert after[2] == before[2] == response.json()["pid"]
    assert after[0] == login.name
    assert after[1] in (None, "")
    assert after[3:] == before[3:]


class _Dropping:
    """A pooled connection that drops one statement on its way to the
    server: BEGIN, as behind a pooler that splits the transaction or a
    wrapper in autocommit mode, where every SET LOCAL then ends with its
    own statement; or the role step alone."""

    def __init__(self, driver: Any, dropped: bytes) -> None:
        self.inner = driver
        self.dropped = dropped

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if bytes(query).strip().upper().startswith(self.dropped):
            return None
        return self.inner.execute(query, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@pytest.mark.parametrize("dropped", [b"BEGIN", b"SET LOCAL ROLE"], ids=["begin", "role step"])
def test_a_lost_set_local_stops_the_request(
    dropped: bytes, server: Server, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = hosted.store.pool
    lease, release = pool.lease, pool.release
    # The login role may run the read-back and the probe itself, so only the
    # read-back stands between the request and the probe: as the login role
    # nothing else would refuse it.
    driver = lease()
    try:
        login = driver.execute("SELECT current_user::text").fetchone()
    finally:
        release(driver)
    assert login is not None
    server.migrator(
        f"GRANT EXECUTE ON FUNCTION public.harrier_owner_id() TO {login[0]}",
        f"GRANT USAGE ON SEQUENCE {PROBE_SEQUENCE} TO {login[0]}",
    )

    def returned(conn: _Dropping) -> None:
        release(conn.inner)

    monkeypatch.setattr(pool, "lease", lambda: _Dropping(lease(), dropped))
    monkeypatch.setattr(pool, "release", returned)

    response = hosted.client().get("/probe/sequence", headers=hosted.headers(server.owner()))

    assert (response.status_code, response.json()) == (500, GENERIC)
    assert hosted.probes.entered == []
    assert not sequence_ran(server)


class _OtherClaims(_Dropping):
    """A pooled connection that sets another owner's claims than asked."""

    def __init__(self, driver: Any, sub: str) -> None:
        super().__init__(driver, b"\x00")
        self.sub = sub

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if b"set_config('request.jwt.claims'" in bytes(query):
            params = (json.dumps({"sub": self.sub, "role": "authenticated"}),)
        return self.inner.execute(query, params, **kwargs)


def test_the_read_back_checks_the_claims_it_set(
    server: Server,
    hosted: Hosted,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The claims differ while the role and the owner the policy reads hold:
    the store's function answers the token's owner whatever the claims say,
    and the connection sets a stranger's claims."""
    owner, stranger = server.owner(), server.owner()
    server.migrator(
        "CREATE OR REPLACE FUNCTION public.harrier_owner_id() RETURNS uuid "
        "LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' "
        f"AS $$ SELECT '{owner}'::uuid $$"
    )
    pool = hosted.store.pool
    lease, release = pool.lease, pool.release

    def returned(conn: _OtherClaims) -> None:
        release(conn.inner)

    monkeypatch.setattr(pool, "lease", lambda: _OtherClaims(lease(), stranger))
    monkeypatch.setattr(pool, "release", returned)
    caplog.set_level(logging.WARNING, logger="harrier_api.hostedstore")

    response = hosted.client().get("/probe/sequence", headers=hosted.headers(owner))

    assert (response.status_code, response.json()) == (500, GENERIC)
    assert hosted.probes.entered == []
    assert not sequence_ran(server)
    assert "GET /probe/sequence could not run as its user: claims differed" in caplog.text


def test_concurrent_requests_keep_their_identities(server: Server, hosted: Hosted) -> None:
    owners = [server.owner(), server.owner()]
    seen: dict[str, Any] = {}

    def ask(owner: str) -> None:
        client = hosted.client(raise_server_exceptions=False)
        response = client.get("/probe/together", headers=hosted.headers(owner))
        ok = response.status_code == 200
        seen[owner] = (response.status_code, response.json() if ok else response.text)

    threads = [threading.Thread(target=ask, args=(owner,)) for owner in owners]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    # Both were inside their transactions at once (the barrier), and each
    # saw its own owner before and after the other's statements.
    assert seen == {owner: (200, {"owners": [owner, owner]}) for owner in owners}


def test_a_refused_request_commits_nothing(
    server: Server, hosted: Hosted, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = server.owner()
    headers = hosted.headers(owner)
    client = hosted.client(raise_server_exceptions=False)

    def write(body: str, then: str) -> Any:
        return client.post("/probe/write", params={"body": body, "then": then}, headers=headers)

    assert write("kept", "ok").status_code == 200
    # A route that raises.
    assert write("raised", "raise").status_code == 500
    # A 4xx the route returns without raising.
    assert write("returned 409", "conflict").status_code == 409
    # A commit the store refuses answers 500, not the route's 200.
    refused = write("refused at commit", "deferred")
    assert (refused.status_code, refused.json()) == (500, GENERIC)
    # A failed statement the route caught: COMMIT would roll back silently.
    failed = write("after a failed statement", "failed statement")
    assert (failed.status_code, failed.json()) == (500, GENERIC)
    assert probe_rows(server) == ["kept"]

    # `POST /capture/add` answers its duplicate 409 by returning, and the
    # middleware rolls that transaction back by its status.
    ended: list[bool] = []
    end = hostedstore.RequestTransaction.end

    def recording(transaction: hostedstore.RequestTransaction, *, commit: bool) -> bool:
        if transaction.is_open:
            ended.append(commit)
        return end(transaction, commit=commit)

    monkeypatch.setattr(hostedstore.RequestTransaction, "end", recording)
    job = {"company": "Synthetic Co", "title": "Synthetic Engineer"}
    assert client.post("/capture/add", json=job, headers=headers).status_code == 200
    assert client.post("/capture/add", json=job, headers=headers).status_code == 409
    assert ended == [True, False]
    assert server.one("SELECT count(*) FROM jobs WHERE company = 'Synthetic Co'") == 1


def pool_handed_out(hosted: Hosted) -> int:
    return hosted.store.pool.handed_out()


def test_an_exhausted_pool_answers_500(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """Open decision 1, as taken: a request that finds no free connection
    within the pool's wait fails as any exception does, 500."""
    create_probes(server)
    one_connection(monkeypatch)
    monkeypatch.setattr(hostedstore, "POOL_WAIT_SECONDS", 1.0)
    with serving(monkeypatch, server.login_role().url) as hosted:
        headers = hosted.headers(server.owner())
        held = hosted.store.pool.lease()
        try:
            client = hosted.client(raise_server_exceptions=False)
            exhausted = client.get("/probe/identity", headers=headers)
        finally:
            hosted.store.pool.release(held)
        assert exhausted.status_code == 500
        assert hosted.probes.entered == []
        # Once the connection is free again, the next request is served.
        assert hosted.client().get("/probe/identity", headers=headers).status_code == 200


def test_a_refused_token_takes_nothing_from_the_pool(server: Server, hosted: Hosted) -> None:
    owner = server.owner()
    client = hosted.client()
    before = pool_handed_out(hosted)

    expired = mint(hosted.key, claims_for(sub=owner, exp=int(time.time()) - 3600))
    forged = mint(new_ec_key(), claims_for(sub=owner))
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers=bearer(expired)).status_code == 401
    assert client.get("/jobs", headers=bearer(forged)).status_code == 401
    assert pool_handed_out(hosted) == before

    # The count does count: one request with a good token takes one.
    assert client.get("/jobs", headers=hosted.headers(owner)).status_code == 200
    assert pool_handed_out(hosted) == before + 1


def test_a_request_without_claims_opens_nothing(
    hosted: Hosted, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A route the token check lets through without a token, which no real
    # route is but `GET /health`, which opens nothing.
    monkeypatch.setattr(
        hostedauth, "PUBLIC_ROUTES", hostedauth.PUBLIC_ROUTES | {("GET", "/probe/identity")}
    )
    before = pool_handed_out(hosted)
    caplog.set_level(logging.WARNING, logger="harrier_api.hostedstore")

    response = hosted.client().get("/probe/identity")

    assert (response.status_code, response.json()) == (500, GENERIC)
    assert pool_handed_out(hosted) == before
    assert hosted.probes.entered == []
    assert "without verified claims" in caplog.text


def test_a_statement_after_the_transaction_ends_is_refused(server: Server, hosted: Hosted) -> None:
    """The middleware ends the transaction when the headers pass; a body
    that reads the store after them fails rather than running outside it."""
    with pytest.raises(StoreDatabaseError) as refused:
        hosted.client().get("/probe/stream", headers=hosted.headers(server.owner()))
    assert str(refused.value) == REQUEST_ENDED


# --- the owner read-back ---

OTHER_OWNER = str(uuid.uuid4())

# How each store breaks `harrier_owner_id()`, and what the read-back names.
BROKEN_OWNER: dict[str, tuple[str, str]] = {
    "another owner": (
        "CREATE OR REPLACE FUNCTION public.harrier_owner_id() RETURNS uuid "
        "LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' "
        f"AS $$ SELECT '{OTHER_OWNER}'::uuid $$",
        "owner",
    ),
    "no owner": (
        "CREATE OR REPLACE FUNCTION public.harrier_owner_id() RETURNS uuid "
        "LANGUAGE sql STABLE SECURITY INVOKER SET search_path = '' "
        "AS $$ SELECT NULL::uuid $$",
        "owner",
    ),
    "no execute": (
        f"REVOKE EXECUTE ON FUNCTION public.harrier_owner_id() FROM {TENANT_ROLE}",
        "read-back",
    ),
}


@pytest.mark.parametrize("case", sorted(BROKEN_OWNER))
def test_the_read_back_checks_the_owner_the_policy_reads(
    case: str, server: Server, hosted: Hosted, caplog: pytest.LogCaptureFixture
) -> None:
    statement, named = BROKEN_OWNER[case]
    server.migrator(statement)
    owner = server.owner()
    claims = claims_for(sub=owner)
    caplog.set_level(logging.DEBUG)

    response = hosted.client().get("/probe/sequence", headers=bearer(mint(hosted.key, claims)))

    assert (response.status_code, response.json()) == (500, GENERIC)
    assert hosted.probes.entered == []
    assert not sequence_ran(server)
    records = [r for r in caplog.records if r.name == "harrier_api.hostedstore"]
    assert [r.getMessage() for r in records] == [
        f"GET /probe/sequence could not run as its user: {named} differed"
    ]
    for value in (owner, OTHER_OWNER, claims["email"], claims["session_id"]):
        assert value not in caplog.text


@pytest.mark.parametrize("case", sorted(BROKEN_OWNER))
def test_an_owner_connection_checks_the_owner_the_policy_reads(case: str, server: Server) -> None:
    create_probes(server)
    statement, named = BROKEN_OWNER[case]
    server.migrator(statement)
    owner = server.owner()

    conn = open_store({URL_VARIABLE: server.url}, owner=owner)
    try:
        with pytest.raises(StoreDatabaseError) as refused, conn.transaction():
            conn.execute(f"SELECT nextval('{PROBE_SEQUENCE}')")
    finally:
        conn.close()

    assert str(refused.value) == f"{IDENTITY_DID_NOT_HOLD} ({named})"
    assert owner not in str(refused.value)
    assert not sequence_ran(server)


# --- timeouts and isolation ---

CHOSEN = {
    "lock_timeout": str(REQUEST_LOCK_TIMEOUT_MS),
    "statement_timeout": str(REQUEST_STATEMENT_TIMEOUT_MS),
    "idle_in_transaction_session_timeout": str(REQUEST_IDLE_TIMEOUT_MS),
}


def test_the_request_role_has_lock_and_statement_timeouts(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_probes(server)
    one_connection(monkeypatch)
    with serving(monkeypatch, server.login_role().url) as hosted:
        headers = hosted.headers(server.owner())
        client = hosted.client()

        seen = client.get("/probe/identity", headers=headers).json()
        assert seen["settings"] == CHOSEN
        assert "0" not in seen["settings"].values()

        # Another session holds a table in ACCESS EXCLUSIVE mode. The
        # request fails once its lock timeout passes, without waiting for
        # that session, and its write before the wait is not kept.
        with closing(postgres_connect(server.url)) as holder:
            holder.execute(b"BEGIN")
            holder.execute(b"LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE")
            started = time.monotonic()
            with pytest.raises(StoreBusyError):
                client.post("/probe/locked", params={"body": "waited"}, headers=headers)
            waited = time.monotonic() - started
            holder.execute(b"ROLLBACK")
        assert REQUEST_LOCK_TIMEOUT_MS / 1000 <= waited < REQUEST_STATEMENT_TIMEOUT_MS / 1000
        assert probe_rows(server) == []

        # A plain SET outlives its transaction on the connection; the next
        # request on the same connection sets its own values again.
        first = client.post("/probe/plain-set", headers=headers).json()
        again = client.get("/probe/identity", headers=headers).json()
        assert again["pid"] == first["pid"]
        assert again["settings"] == CHOSEN


def test_a_connection_ended_by_the_idle_timeout_is_not_reused(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_probes(server)
    one_connection(monkeypatch)
    # Shorter than the chosen two minutes, so the test does not wait them.
    monkeypatch.setattr(hostedstore, "REQUEST_IDLE_TIMEOUT_MS", 300)
    with serving(monkeypatch, server.login_role().url) as hosted:
        headers = hosted.headers(server.owner())
        client = hosted.client()

        ended = client.get("/probe/idle", params={"seconds": 1.5}, headers=headers)
        assert (ended.status_code, ended.json()) == (500, GENERIC)
        (entered,) = hosted.probes.entered
        killed = int(entered.split()[1])

        after = client.get("/probe/identity", headers=headers)
        assert after.status_code == 200, after.text
        assert after.json()["pid"] != killed


def test_a_connection_left_in_a_transaction_is_not_reused(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pool would roll such a connection back and hand it out again;
    the release discards it instead."""
    one_connection(monkeypatch)
    with serving(monkeypatch, server.login_role().url) as hosted:
        pool = hosted.store.pool
        driver = pool.lease()
        left = driver.execute("SELECT pg_backend_pid()").fetchone()
        driver.execute(b"BEGIN")
        pool.release(driver)
        driver = pool.lease()
        try:
            again = driver.execute("SELECT pg_backend_pid()").fetchone()
        finally:
            pool.release(driver)
    assert left != again


def test_the_request_transaction_reads_committed(server: Server, hosted: Hosted) -> None:
    owner = server.owner()
    headers = hosted.headers(owner)

    # Two track inserts for one owner, in two requests at once: the second
    # waits on the owner's track 1 and then sees the first's track.
    answers: dict[str, Any] = {}

    def insert(slug: str) -> None:
        response = hosted.client(raise_server_exceptions=False).post(
            "/probe/track", params={"slug": slug}, headers=headers
        )
        ok = response.status_code == 200
        answers[slug] = (response.status_code, response.json() if ok else response.text)

    threads = [threading.Thread(target=insert, args=(slug,)) for slug in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert {status for status, _ in answers.values()} == {200}, answers
    ids = sorted(body["id"] for _, body in answers.values())
    assert len(set(ids)) == 2

    seen = hosted.client().get("/probe/identity", headers=headers).json()
    assert seen["isolation"] == "read committed"
