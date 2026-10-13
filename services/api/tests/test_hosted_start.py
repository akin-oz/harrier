"""The spec 103 gate lifts, and the hosted API's start checks (spec 134).

A Postgres store is served only hosted, and only once the start checks
pass: the login role cannot step past the row policy
(`harrier.loginrole`), the store defaults to `READ COMMITTED`, and the store
is at the version this code needs. The checks run once, at start.

Each case runs on a copy of the migrated template with roles made for it,
dropped after it, through the helpers in `tests/test_hosted_requests.py`.
A start that is refused raises from `create_app`, which is what makes
uvicorn's import fail and the process exit 1; the cases that check the exit
itself run the import as a process, as `tests/test_hosted_auth.py` does.
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, which is why every API test file carries these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest
from pg_support import fresh_database
from test_hosted_auth import API_HOST, SPA_ORIGIN, child_environment, start_api
from test_hosted_requests import (
    LoginRole,
    Server,
    copied_server,
    drop_roles,
    hosted_environment,
    keys_served_here,
    serving,
)
from test_owner_policy import migrate_to

import harrier.pgstore
from harrier.connection import Connection, StoreVersionError
from harrier.demo import repo_root
from harrier.loginrole import (
    BYPASSRLS,
    CANNOT_SET_TENANT,
    MEMBER_PAST_THE_POLICY,
    NO_SUCH_ROLE,
    OWNS_A_TABLE_IN_PUBLIC,
    REACHED_BY_NAME,
    REACHED_BYPASSRLS,
    REACHED_OWNS_PUBLIC,
    REACHED_SUPERUSER,
    SUPERUSER,
    LoginRoleProblem,
    login_role_problems,
)
from harrier.pgstore import check_postgres_version, postgres_connect, target_version
from harrier.tracker.schema import TENANT_ROLE
from harrier_api import app as app_module
from harrier_api import hostedstore
from harrier_api.app import create_app
from harrier_api.hostedauth import POSTGRES_NEEDS_HOSTED_REFUSAL
from harrier_api.hostedstore import (
    START_UNREACHABLE,
    HostedStartRefused,
    check_failed,
    isolation_refusal,
    login_role_refusal,
)

CONTRACT = repo_root() / "packages" / "contract" / "openapi.json"


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


# --- the gate ---


def test_the_api_serves_postgres_only_in_hosted_mode(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    password = f"synthetic{uuid.uuid4().hex}"
    local = child_environment(
        tmp_path, HARRIER_DATABASE_URL=f"postgresql://harrier_api:{password}@127.0.0.1:1/postgres"
    )
    started = start_api(local, tmp_path)
    assert started.returncode == 1, started.stderr
    last_line = started.stderr.strip().splitlines()[-1]
    assert last_line == f"harrier_api.hostedauth.HostedConfigError: {POSTGRES_NEEDS_HOSTED_REFUSAL}"
    assert password not in started.stdout + started.stderr

    # Complete hosted mode, a store at the target and a login role that
    # passes: it starts.
    with serving(monkeypatch, server.login_role().url) as hosted:
        assert hosted.store is hosted.app.state.request_store
    assert not hasattr(app_module, "POSTGRES_NOT_YET")


def test_create_app_serves_a_complete_hosted_configuration_hosted(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serving(monkeypatch, server.login_role().url) as hosted:
        client = hosted.client()
        missing = client.get("/jobs")
        assert (missing.status_code, missing.json()["reason"]) == (401, "missing")
        assert client.get("/session").status_code == 404
        # Served from the Postgres store: the request took a connection.
        before = hosted.store.pool.handed_out()
        served = client.get("/jobs", headers=hosted.headers(server.owner()))
        assert (served.status_code, served.json()) == (200, [])
        assert hosted.store.pool.handed_out() == before + 1


def test_the_contract_is_the_same_with_the_store(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted `get_conn` and the transaction middleware change no route
    declaration: the hosted app on a store serves the committed contract
    (contract review of this change)."""
    keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, server.login_role().url)
    app = create_app()
    try:
        assert app.dependency_overrides  # the hosted get_conn is installed
        served = json.dumps(app.openapi(), sort_keys=True)
    finally:
        app.state.request_store.close()
    committed = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert served == json.dumps(committed, sort_keys=True)


def test_an_unreachable_store_refuses_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hostedstore, "POOL_WAIT_SECONDS", 1.0)
    password = f"synthetic{uuid.uuid4().hex}"
    keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, f"postgresql://harrier_api:{password}@127.0.0.1:1/postgres")
    with pytest.raises(HostedStartRefused) as refused:
        create_app()
    assert str(refused.value) == START_UNREACHABLE
    assert refused.value.__cause__ is None and refused.value.__context__ is None


# --- the login role ---

SetUp = Callable[[Server], tuple[LoginRole, list[LoginRoleProblem]]]


def superuser(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    return server.login_role("SUPERUSER"), [LoginRoleProblem(SUPERUSER)]


def bypassrls(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    return server.login_role("BYPASSRLS"), [LoginRoleProblem(BYPASSRLS)]


def member_of_service_role(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    login = server.login_role()
    server.sql(f"GRANT service_role TO {login.name}")
    reached = LoginRoleProblem(
        MEMBER_PAST_THE_POLICY, "service_role", (REACHED_BYPASSRLS, REACHED_BY_NAME)
    )
    return login, [reached]


def cannot_set_tenant(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    return server.login_role(tenant="none"), [LoginRoleProblem(CANNOT_SET_TENANT)]


def owns_a_public_table(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    login = server.login_role()
    table = f"public.harrier_t_{uuid.uuid4().hex[:12]}"
    server.sql(f"CREATE TABLE {table} (body text)", f"ALTER TABLE {table} OWNER TO {login.name}")
    return login, [LoginRoleProblem(OWNS_A_TABLE_IN_PUBLIC)]


def through_a_noinherit_role(
    attributes: str, why: tuple[str, ...], *, owns_public: bool = False, named: str = ""
) -> SetUp:
    """A login role that reaches, through a `NOINHERIT` role between, a
    role past the policy."""

    def set_up(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
        login = server.login_role()
        between = server.role("NOLOGIN NOINHERIT")
        reached = named or server.role(f"NOLOGIN {attributes}")
        if owns_public:
            table = f"public.harrier_t_{uuid.uuid4().hex[:12]}"
            server.sql(
                f"CREATE TABLE {table} (body text)", f"ALTER TABLE {table} OWNER TO {reached}"
            )
        server.sql(f"GRANT {reached} TO {between}", f"GRANT {between} TO {login.name}")
        return login, [LoginRoleProblem(MEMBER_PAST_THE_POLICY, reached, why)]

    return set_up


def granted_without_set(server: Server) -> tuple[LoginRole, list[LoginRoleProblem]]:
    login = server.login_role()
    reached = server.role("NOLOGIN BYPASSRLS")
    server.sql(f"GRANT {reached} TO {login.name} WITH INHERIT FALSE, SET FALSE")
    return login, [LoginRoleProblem(MEMBER_PAST_THE_POLICY, reached, (REACHED_BYPASSRLS,))]


BYPASS_CASES: dict[str, SetUp] = {
    "a superuser": superuser,
    "bypassrls": bypassrls,
    "a member of service_role": member_of_service_role,
    "cannot set the tenant role": cannot_set_tenant,
    "owns a table in public": owns_a_public_table,
}

SET_ROLE_CASES: dict[str, SetUp] = {
    "through a noinherit role, a bypassrls role": through_a_noinherit_role(
        "BYPASSRLS", (REACHED_BYPASSRLS,)
    ),
    "through a noinherit role, a superuser role": through_a_noinherit_role(
        "SUPERUSER", (REACHED_SUPERUSER,)
    ),
    "through a noinherit role, a role owning a table in public": through_a_noinherit_role(
        "", (REACHED_OWNS_PUBLIC,), owns_public=True
    ),
    "through a noinherit role, service_role": through_a_noinherit_role(
        "", (REACHED_BYPASSRLS, REACHED_BY_NAME), named="service_role"
    ),
    "a direct membership without set": granted_without_set,
}


def refused_start(
    server: Server, monkeypatch: pytest.MonkeyPatch, login: LoginRole
) -> HostedStartRefused:
    keys_served_here(monkeypatch)
    hosted_environment(monkeypatch, login.url)
    with pytest.raises(HostedStartRefused) as refused:
        create_app()
    # Nothing of the URL or the password is in the refusal, and the pool it
    # opened is closed again.
    for value in (login.password, login.url, "postgresql://"):
        assert value not in str(refused.value)
    assert server.one(f"SELECT count(*) FROM pg_stat_activity WHERE usename = '{login.name}'") == 0
    return refused.value


@pytest.mark.parametrize("case", sorted(BYPASS_CASES))
def test_hosted_start_refuses_a_login_role_that_can_bypass_policy(
    case: str, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    login, problems = BYPASS_CASES[case](server)
    refused = refused_start(server, monkeypatch, login)
    assert str(refused) == login_role_refusal(problems)


@pytest.mark.parametrize("case", [*sorted(SET_ROLE_CASES), "harrier_tenant alone"])
def test_hosted_start_refuses_a_login_role_that_can_set_role_past_the_policy(
    case: str, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    if case == "harrier_tenant alone":
        with serving(monkeypatch, server.login_role().url) as hosted:
            response = hosted.client().get("/jobs", headers=hosted.headers(server.owner()))
        assert response.status_code == 200, response.text
        return
    login, problems = SET_ROLE_CASES[case](server)
    refused = refused_start(server, monkeypatch, login)
    assert str(refused) == login_role_refusal(problems)


def test_the_login_role_checks_name_each_failure(server: Server) -> None:
    built = {case: set_up(server) for case, set_up in {**BYPASS_CASES, **SET_ROLE_CASES}.items()}
    passing = server.login_role()
    with closing(postgres_connect(server.url)) as driver:
        conn = Connection.postgres(driver)
        found = {case: login_role_problems(conn, login.name) for case, (login, _) in built.items()}
        clean = login_role_problems(conn, passing.name)
        absent = login_role_problems(conn, f"harrier_t_{uuid.uuid4().hex[:12]}")
    assert found == {case: problems for case, (_, problems) in built.items()}
    assert clean == []
    assert absent == [LoginRoleProblem(NO_SUCH_ROLE)]
    # Names only: no case's text carries a password.
    for case, (login, _) in built.items():
        assert login.password not in "; ".join(map(str, found[case]))


# --- the store's default isolation ---


def test_a_store_defaulting_to_another_isolation_is_refused(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = server.login_role()
    server.sql(f"ALTER ROLE {login.name} SET default_transaction_isolation = 'repeatable read'")
    refused = refused_start(server, monkeypatch, login)
    assert str(refused) == isolation_refusal("repeatable read")


def test_a_check_the_store_refuses_names_the_check(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check statement the store refuses is a refusal naming the check,
    with the store's primary message and nothing chained (privacy review
    of this change)."""
    login = server.login_role()
    server.migrator(f"REVOKE SELECT ON schema_version FROM {TENANT_ROLE}")
    refused = refused_start(server, monkeypatch, login)
    assert str(refused) == check_failed(
        "store version", "permission denied for table schema_version"
    )
    assert refused.__cause__ is None and refused.__context__ is None


# --- the store version, once ---


def version_text(found: int) -> str:
    with pytest.raises(StoreVersionError) as refused:
        check_postgres_version(found)
    return str(refused.value)


def start_as_a_process(login: LoginRole, tmp_path: Path) -> tuple[int, str, str]:
    """`import harrier_api.app` in hosted mode, as uvicorn does. The key
    endpoint is a closed port on this machine: the first fetch fails, is
    logged, and the start goes on to the store, as spec 104 says it does."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    environ = child_environment(
        tmp_path,
        HARRIER_AUTH="supabase",
        HARRIER_SUPABASE_URL="https://127.0.0.1:1",
        HARRIER_ALLOWED_HOSTS=API_HOST,
        HARRIER_CORS_ORIGINS=SPA_ORIGIN,
        HARRIER_DATABASE_URL=login.url,
    )
    started = start_api(environ, tmp_path)
    return started.returncode, started.stdout + started.stderr, started.stderr.strip()


def test_hosted_start_checks_the_store_version_once(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = target_version()

    # One version behind.
    roles: list[str] = []
    try:
        with fresh_database() as url:
            migrate_to(url, target - 1, monkeypatch)
            behind = Server(url, roles).login_role()
            code, printed, stderr = start_as_a_process(behind, tmp_path / "behind")
            assert code == 1, printed
            assert stderr.splitlines()[-1] == (
                f"harrier.connection.StoreVersionError: {version_text(target - 1)}"
            )
            assert behind.password not in printed
    finally:
        drop_roles(roles)

    # One ahead.
    login = server.login_role()
    server.migrator(f"INSERT INTO schema_version (version) VALUES ({target + 1})")
    code, printed, stderr = start_as_a_process(login, tmp_path / "ahead")
    assert code == 1, printed
    assert stderr.splitlines()[-1] == (
        f"harrier.connection.StoreVersionError: {version_text(target + 1)}"
    )
    assert login.password not in printed
    server.migrator(f"DELETE FROM schema_version WHERE version = {target + 1}")

    # At the target it starts, and a request reads no version: the tenant
    # loses its grant on `schema_version`, and nothing that reads it runs.
    read: list[str] = []

    def reads_the_version(*_: object) -> int:
        read.append("version")
        return target

    with serving(monkeypatch, login.url) as hosted:
        server.migrator(f"REVOKE SELECT ON schema_version FROM {TENANT_ROLE}")
        monkeypatch.setattr(harrier.pgstore, "postgres_version", reads_the_version)
        monkeypatch.setattr(harrier.pgstore, "open_postgres_store", reads_the_version)
        response = hosted.client().get("/jobs", headers=hosted.headers(server.owner()))
    assert response.status_code == 200, response.text
    assert read == []
