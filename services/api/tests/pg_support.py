"""A throwaway Postgres database per test (spec 103).

The Postgres tests need a server. `HARRIER_TEST_POSTGRES_URL` names one
whose user may create databases and roles; each test gets its own database,
dropped afterwards. Locally, without the URL, the tests skip and pytest's
summary says so. In CI (`CI=true`) a missing URL or an unreachable server
fails the run: a gate that skips in CI has not run (spec 039).

To run them locally:

    docker run -d --rm --name harrier-pg -e POSTGRES_PASSWORD=postgres \\
        -p 127.0.0.1:55432:5432 postgres:17
    HARRIER_TEST_POSTGRES_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run pytest -q

Every fresh database gets a shim of Supabase's auth objects before anything
migrates it (spec 105): the roles anon, authenticated and service_role, a
schema auth with a table auth.users, auth.uid(), and Supabase's default
privileges, in public and global. Migration 10 needs them and creates none
of them.
`as_owner` is how a test acts as one owner: the same role (harrier_tenant,
which migration 10 creates) and claims a request's transaction will carry
(ADR-013 decision 3, spec 104).

Every store is migrated, and every test connects, as `harrier_migrator`, a
role shaped like Supabase's `postgres` as spec 115's staging run found it:
not a superuser, holding in the shim the auth privileges `postgres` holds on
a real project. A migration that needs a privilege `postgres` lacks then
fails here as it would on Supabase. The server's superuser only creates and
drops each database and installs the shim (spec 115).

The dual-store fixture (`store` in `tests/conftest.py`, spec 126) runs a
domain test on both stores. Its Postgres side migrates one template per
session (`migrated_template`), copies it per test (`database_copy`), and
opens the copy as one synthetic owner, failing the test unless the
connection runs as `harrier_tenant` (`open_owner_store`).

Staging mode (spec 115) points the isolation tests at a real Supabase
project instead (`tests/test_supabase_project.py`): `supabase_urls` names
the project or skips, and `open_staging` refuses a project anyone has signed
in to before it writes anything.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from harrier.pgstore import postgres_version, target_version
from harrier.tracker.schema import TENANT_ROLE

if TYPE_CHECKING:
    import psycopg

    from harrier.connection import Connection

    PgConnection = psycopg.Connection[Any]

TEST_URL_VARIABLE = "HARRIER_TEST_POSTGRES_URL"

# The role every Postgres test migrates and connects as (spec 115). Its
# attributes are the ones spec 115's staging run found Supabase's postgres
# has (A4): not a superuser; CREATEROLE, CREATEDB, REPLICATION, BYPASSRLS.
# The password is a throwaway for a role on a test server, like CI's
# postgres:postgres, not a credential.
MIGRATOR_ROLE = "harrier_migrator"
MIGRATOR_PASSWORD = "harrier_migrator"
MIGRATOR_ATTRIBUTES = "LOGIN NOSUPERUSER INHERIT CREATEROLE CREATEDB REPLICATION BYPASSRLS"
# Supabase's postgres is a member of these with the admin option, INHERIT
# and SET (spec 115's staging run). Tests SET ROLE to them.
MIGRATOR_MEMBER_OF: tuple[str, ...] = ("anon", "authenticated", "service_role")


def _in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def admin_url() -> str:
    """The test server's URL, or skip (locally) or fail (in CI)."""
    url = os.environ.get(TEST_URL_VARIABLE, "").strip()
    if not url:
        message = f"{TEST_URL_VARIABLE} is not set; the Postgres tests need a server"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        import psycopg  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        message = "psycopg is not installed; run uv sync (the dev group includes postgres)"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    return url


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _with_user(url: str, user: str, password: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:{password}@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def superuser_url(url: str) -> str:
    """The test server's superuser, on the database `url` names: for the few
    steps the migrating role may not take, such as installing the shim or
    giving a role SUPERUSER."""
    return _with_database(admin_url(), urlsplit(url).path.lstrip("/") or "postgres")


# --- the Supabase shim (spec 105) ---

# As Supabase documents them: anon and authenticated never bypass row
# security, service_role always does. None of them logs in; a session
# becomes one with SET ROLE.
SHIM_ROLES: tuple[tuple[str, str], ...] = (
    ("anon", "NOLOGIN NOINHERIT NOBYPASSRLS"),
    ("authenticated", "NOLOGIN NOINHERIT NOBYPASSRLS"),
    ("service_role", "NOLOGIN NOINHERIT BYPASSRLS"),
)

SHIM_STATEMENTS: tuple[str, ...] = (
    "CREATE SCHEMA auth",
    "CREATE TABLE auth.users (id uuid PRIMARY KEY)",
    # The body Supabase's auth server ships (migration
    # 20220224000811_update_auth_functions): the old single claim first,
    # then the sub of the claims document, as uuid. Null without either.
    # Spec 115's staging run found the same body on a project (A3).
    """
    CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
        SELECT coalesce(
            nullif(current_setting('request.jwt.claim.sub', true), ''),
            (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
        )::uuid
    $$
    """,
    "GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role",
    # What Supabase gives postgres in auth, as spec 115's staging run found
    # it (2026-10-11): on auth.users INSERT, UPDATE, DELETE, TRUNCATE,
    # REFERENCES, TRIGGER and MAINTAIN, and SELECT with the grant option
    # (A1, A8); on auth.uid() nothing beyond PUBLIC's EXECUTE (A2); and
    # USAGE on auth without the grant option, so migration 10's grants on
    # auth to the tenant role do nothing here, as on Supabase (A2, refuted;
    # spec 105, amendment of 2026-10-11). The auth.users grants are
    # conditional, so a partial shim without auth.users installs.
    f"GRANT USAGE ON SCHEMA auth TO {MIGRATOR_ROLE}",
    f"""
    DO $$
    BEGIN
        IF to_regclass('auth.users') IS NOT NULL THEN
            GRANT INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER, MAINTAIN
                ON auth.users TO {MIGRATOR_ROLE};
            GRANT SELECT ON auth.users TO {MIGRATOR_ROLE} WITH GRANT OPTION;
        END IF;
    END
    $$
    """,
    # Supabase's default privileges: every table, sequence and function the
    # migrating role creates in public is granted to its three roles. Set
    # for the role the tests migrate with, so the baseline's tables arrive
    # with these grants, as they would on a Supabase project, and a revoke
    # that migration 10 forgot shows in the catalog test (spec 105,
    # post-merge review).
    "GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role",
    *(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} IN SCHEMA public "
        f"GRANT ALL ON {kind} TO anon, authenticated, service_role"
        for kind in ("TABLES", "SEQUENCES", "FUNCTIONS")
    ),
    # The same grants with no schema named, which apply in every schema. A
    # revoke in public alone leaves these in place, so the catalog test sees
    # one that migration 14 forgot (spec 105, second post-merge review).
    *(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {MIGRATOR_ROLE} "
        f"GRANT ALL ON {kind} TO anon, authenticated, service_role"
        for kind in ("TABLES", "SEQUENCES", "FUNCTIONS")
    ),
)


def _create_role(conn: PgConnection, name: str, attributes: str) -> None:
    """Create a role unless it exists. Roles belong to the whole server and
    outlive each test database, so another test, or another run, may have
    made it already, or may be making it at this moment."""
    conn.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{name}') THEN
                CREATE ROLE {name} {attributes};
            END IF;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
        """.encode()
    )


def _grant_to_migrator(conn: PgConnection, role: str, options: str) -> None:
    """Make the migrator a member of `role` with `options`, when `role`
    exists and the migrator does not already hold it with the admin option
    and SET. Another run on the same server may be granting it now. Roles
    are matched by name, not cast with ::regrole, which fails while `role`
    is absent (a fresh server has no anon before the first shim)."""
    conn.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') AND NOT EXISTS (
                SELECT 1 FROM pg_auth_members m
                JOIN pg_roles r ON r.oid = m.roleid
                JOIN pg_roles u ON u.oid = m.member
                WHERE r.rolname = '{role}' AND u.rolname = '{MIGRATOR_ROLE}'
                    AND m.admin_option AND m.set_option
            ) THEN
                GRANT {role} TO {MIGRATOR_ROLE} WITH {options};
            END IF;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
        """.encode()
    )


def ensure_migrator(conn: PgConnection) -> None:
    """Create `harrier_migrator` unless it exists, and give it the
    memberships Supabase gives postgres. Run as the superuser.

    The tenant role is made by migration 10, by whichever role migrates
    first, and outlives each database. Its creator holds the admin option on
    it, as Supabase's postgres does; a migrator that did not create it is
    given the same. SET on it lets a test act as the tenant: the migrator
    gets it from `createrole_self_grant` when it creates the role, or from
    the grant here. Supabase leaves `createrole_self_grant` empty, so its
    postgres cannot SET ROLE to the tenant role it created, and staging
    mode grants that the same way (`open_staging`, spec 115).
    """
    conn.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{MIGRATOR_ROLE}') THEN
                CREATE ROLE {MIGRATOR_ROLE} {MIGRATOR_ATTRIBUTES}
                    PASSWORD '{MIGRATOR_PASSWORD}';
                ALTER ROLE {MIGRATOR_ROLE} SET createrole_self_grant = 'set';
            END IF;
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
        """.encode()
    )
    for role in MIGRATOR_MEMBER_OF:
        _grant_to_migrator(conn, role, "ADMIN OPTION, INHERIT TRUE, SET TRUE")
    _grant_to_migrator(conn, TENANT_ROLE, "ADMIN OPTION, INHERIT FALSE, SET TRUE")


def install_shim(url: str, statements: tuple[str, ...] = SHIM_STATEMENTS) -> None:
    """Give the database `url` names the auth objects migration 10 needs.

    Installed by the superuser whatever role `url` names, as Supabase's own
    roles own these on a project. A test that needs part of the shim passes
    the statements it wants. The roles are always made: they belong to the
    whole server, and other databases on it need them.
    """
    import psycopg

    with psycopg.connect(superuser_url(url), autocommit=True) as conn:
        for name, attributes in SHIM_ROLES:
            _create_role(conn, name, attributes)
        ensure_migrator(conn)
        with conn.transaction():
            for statement in statements:
                conn.execute(statement.encode())


def new_owner(conn: PgConnection) -> str:
    """A synthetic owner: a random id in auth.users. Inserting it fires
    harrier_new_owner(), which gives the owner track 1 on a migrated store."""
    owner = str(uuid.uuid4())
    conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (owner,))
    return owner


def claims(owner: str) -> str:
    """The JWT claims a request for `owner` carries, as harrier_owner_id()
    reads them (spec 105, amendment of 2026-10-11)."""
    return json.dumps({"sub": owner, "role": "authenticated"})


@contextmanager
def as_role(conn: PgConnection, role: str, owner: str | None = None) -> Generator[PgConnection]:
    """A transaction as `role`, with `owner`'s claims when one is given.

    Both settings are local, so they end with the transaction. An error
    inside rolls the transaction back; a test that expects a refusal and
    goes on wraps the statement in its own `conn.transaction()`, a savepoint.
    """
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {role}".encode())
        if owner is not None:
            conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims(owner),))
        yield conn


@contextmanager
def as_owner(conn: PgConnection, owner: str) -> Generator[PgConnection]:
    """A transaction as `owner`, policed: the role is the tenant role, which
    cannot bypass row security, unlike the migrating role the tests connect
    as."""
    with as_role(conn, TENANT_ROLE, owner) as policed:
        # The proof that a test is policed at all: were the role switch ever
        # lost, every isolation test would run as the migrating role, which
        # bypasses row security, and pass.
        role = policed.execute("SELECT current_user").fetchone()
        assert role == (TENANT_ROLE,), f"as_owner runs as {role}, not {TENANT_ROLE}"
        yield policed


def set_session_owner(conn: PgConnection, owner: str) -> None:
    """Claims for the whole session, without changing role.

    The migrating role still bypasses the policy (BYPASSRLS); the claims
    only make each owner_id default to `owner`. The parity test needs that:
    it compares shapes and defaults, and the catalog test owns the policy
    (spec 105).
    """
    conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims(owner),))


# --- the migrating role (spec 115) ---

# The roles a default privilege or a new function must not reach.
DATA_API_ROLES: tuple[str, ...] = ("anon", "authenticated", "service_role")


def non_superuser_migrator_problems(conn: PgConnection) -> list[str]:
    """What breaks spec 115's A11 for the role `conn` runs as: the store
    reached target_version() as a role that is not a superuser, that role
    holds no default privilege in public or global granting to anon,
    authenticated or service_role, and a function it creates in public is
    executable by none of them nor by PUBLIC. The function lives in a
    transaction that is rolled back."""
    problems: list[str] = []
    row = conn.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert row is not None
    role, is_superuser = str(row[0]), bool(row[1])
    if is_superuser:
        problems.append(f"{role} is a superuser")
    found = postgres_version(conn)
    if found != target_version():
        problems.append(f"the store is at version {found}, not {target_version()}")
    for scope, kind, grantee, privilege in conn.execute(
        "SELECT CASE WHEN d.defaclnamespace = 0 THEN 'global' ELSE 'in public' END, "
        "d.defaclobjtype, pg_get_userbyid(x.grantee), x.privilege_type "
        "FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) x "
        "WHERE d.defaclrole = current_user::regrole "
        "AND d.defaclnamespace IN (0, 'public'::regnamespace) AND x.grantee <> 0 "
        "AND pg_get_userbyid(x.grantee) = ANY(%s)",
        (list(DATA_API_ROLES),),
    ).fetchall():
        problems.append(f"default privileges ({scope}): {grantee} gets {privilege} on {kind}")
    name = f"public.harrier_probe_{uuid.uuid4().hex[:12]}()"
    with conn.transaction(force_rollback=True):
        conn.execute(f"CREATE FUNCTION {name} RETURNS integer LANGUAGE sql AS 'SELECT 1'".encode())
        by_public = conn.execute(
            "SELECT proacl IS NULL OR EXISTS (SELECT 1 FROM aclexplode(proacl) x "
            "WHERE x.grantee = 0 AND x.privilege_type = 'EXECUTE') "
            "FROM pg_proc WHERE oid = %s::regprocedure",
            (name,),
        ).fetchone()
        if by_public != (False,):
            problems.append("a new function in public is executable by PUBLIC")
        for grantee in DATA_API_ROLES:
            executable = conn.execute(
                "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
                (grantee, name),
            ).fetchone()
            if executable != (False,):
                problems.append(f"a new function in public is executable by {grantee}")
    return problems


# --- databases ---


@contextmanager
def fresh_database(*, shim: bool = True) -> Generator[str]:
    """Create an empty database, yield its URL as `harrier_migrator`, then
    drop it.

    The migrator owns the database, as Supabase's postgres owns a
    project's, so it may create in public. With the shim unless
    `shim=False`, which is a Postgres without Supabase's auth objects.
    """
    import psycopg

    admin = admin_url()
    name = f"harrier_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            ensure_migrator(conn)
            conn.execute(f"CREATE DATABASE {name} OWNER {MIGRATOR_ROLE}".encode())
    except psycopg.OperationalError as error:
        message = f"cannot reach the Postgres test server: {type(error).__name__}"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        url = _with_user(_with_database(admin, name), MIGRATOR_ROLE, MIGRATOR_PASSWORD)
        if shim:
            install_shim(url)
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)".encode())


@pytest.fixture
def postgres_url() -> Iterator[str]:
    """An empty Postgres database for one test."""
    with fresh_database() as url:
        yield url


# --- the dual-store fixture's Postgres side (spec 126) ---


@contextmanager
def migrated_template() -> Generator[str]:
    """A database `fresh_database` creates, migrated by
    `apply_postgres_migrations` as `harrier_migrator`, for the session.

    The migrating connection is closed before this yields, so the first
    copy finds no other connection to the template; Postgres refuses
    `CREATE DATABASE ... TEMPLATE` while there is one.
    """
    from contextlib import closing

    from harrier.pgstore import apply_postgres_migrations, postgres_connect

    with fresh_database() as url:
        with closing(postgres_connect(url)) as conn:
            apply_postgres_migrations(conn)
        yield url


@contextmanager
def database_copy(template_url: str) -> Generator[str]:
    """A copy of the template for one test, owned by `harrier_migrator`,
    yielded as the migrator's URL, then dropped. Created and dropped by the
    server's superuser, as `fresh_database` does."""
    import psycopg

    template = urlsplit(template_url).path.lstrip("/")
    name = f"harrier_test_{uuid.uuid4().hex[:12]}"
    admin = admin_url()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name} TEMPLATE {template} OWNER {MIGRATOR_ROLE}".encode())
    try:
        yield _with_user(_with_database(admin, name), MIGRATOR_ROLE, MIGRATOR_PASSWORD)
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)".encode())


def require_tenant(conn: Connection) -> None:
    """Fail the test unless `conn` runs as `harrier_tenant`, read in a
    transaction. A lost role switch would run the test as the migrating
    role, which bypasses the row policy, for the reason `as_owner` gives."""
    with conn.transaction():
        row = conn.execute("SELECT current_user").fetchone()
    role = str(row[0]) if row is not None else None
    if role != TENANT_ROLE:
        pytest.fail(f"the store fixture runs as {role}, not {TENANT_ROLE}", pytrace=False)


def open_owner_store(url: str, owner: str | None) -> Connection:
    """`open_store` on `url` acting as `owner`, once it is known to run as
    `harrier_tenant`. Closed again when it is not."""
    from harrier.connection import URL_VARIABLE, open_store

    conn = open_store({URL_VARIABLE: url}, owner=owner)
    try:
        require_tenant(conn)
    except BaseException:
        conn.close()
        raise
    return conn


# --- staging mode: a real Supabase project (spec 115) ---

SUPABASE_URL_VARIABLE = "HARRIER_SUPABASE_TEST_URL"
SUPABASE_API_URL_VARIABLE = "HARRIER_SUPABASE_API_TEST_URL"
# The custom login role the project's pooler is checked with (A5, A10).
PROBE_ROLE = "harrier_api_probe"
# Supabase's shared pooler: session mode on 5432, transaction mode on 6543.
SESSION_POOLER_PORT = 5432
TRANSACTION_POOLER_PORT = 6543

STAGING_HAS_USERS = (
    "staging mode refuses a project whose auth.users holds a user with an email: "
    "someone has signed in to it, so it is no test target (spec 115)"
)


class StagingProjectRefused(Exception):
    """Staging mode will not run against this project. Names no URL."""


def supabase_urls() -> tuple[str, str]:
    """The migrator's URL and the probe role's URL, or skip naming the one
    that is missing. Skips in CI too: the module needs a real project and
    its credentials, which CI holds neither of (spec 115)."""
    found: list[str] = []
    for variable in (SUPABASE_URL_VARIABLE, SUPABASE_API_URL_VARIABLE):
        value = os.environ.get(variable, "").strip()
        if not value:
            pytest.skip(f"{variable} is not set; the Supabase project checks need a real project")
        found.append(value)
    try:
        import psycopg  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        pytest.skip("psycopg is not installed; run uv sync (the dev group includes postgres)")
    return found[0], found[1]


def with_port(url: str, port: int) -> str:
    """`url` with its port replaced, its user and password kept."""
    parts = urlsplit(url)
    authority, _, _ = parts.netloc.rpartition("@")
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{authority}@{host}:{port}" if authority else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def open_staging(conn: PgConnection) -> None:
    """Make the project `conn` reaches ready for staging mode, or refuse it.

    First, before any write: refuse a project whose auth.users holds a user
    with an email, since someone has signed in to it, and one not at
    target_version(). Then let the connecting role SET ROLE to the tenant
    role: it holds only the admin option on the role it created, because
    Supabase leaves `createrole_self_grant` empty (spec 115's staging
    findings). Synthetic owners have no email, so a re-run is not refused.
    """
    has_email = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = 'auth' AND table_name = 'users' AND column_name = 'email')"
    ).fetchone()
    if has_email == (True,):
        used = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM auth.users WHERE email IS NOT NULL)"
        ).fetchone()
        if used != (False,):
            raise StagingProjectRefused(STAGING_HAS_USERS)
    found = postgres_version(conn)
    if found != target_version():
        raise StagingProjectRefused(
            f"the project is at version {found}; run harrier store migrate "
            f"to version {target_version()} first"
        )
    conn.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_auth_members
                WHERE roleid = '{TENANT_ROLE}'::regrole AND member = current_user::regrole
                    AND set_option
            ) THEN
                EXECUTE format(
                    'GRANT {TENANT_ROLE} TO %I WITH INHERIT FALSE, SET TRUE', current_user
                );
            END IF;
        END
        $$
        """.encode()
    )
