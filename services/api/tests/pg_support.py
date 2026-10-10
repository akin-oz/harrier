"""A throwaway Postgres database per test (spec 103).

The Postgres tests need a server. `HARRIER_TEST_POSTGRES_URL` names one
whose user may create databases; each test gets its own database, dropped
afterwards. Locally, without the URL, the tests skip and pytest's summary
says so. In CI (`CI=true`) a missing URL or an unreachable server fails the
run: a gate that skips in CI has not run (spec 039).

To run them locally:

    docker run -d --rm --name harrier-pg -e POSTGRES_PASSWORD=postgres \\
        -p 127.0.0.1:55432:5432 postgres:17
    HARRIER_TEST_POSTGRES_URL=postgresql://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run pytest -q

Every fresh database gets a shim of Supabase's auth objects before anything
migrates it (spec 105): the roles anon, authenticated and service_role, a
schema auth with a table auth.users, and auth.uid(). Migration 10 needs them
and creates none of them. `as_owner` is how a test acts as one owner: the
same role (harrier_tenant, which migration 10 creates) and claims a
request's transaction will carry (ADR-013 decision 3, spec 104).
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

from harrier.tracker.schema import TENANT_ROLE

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

TEST_URL_VARIABLE = "HARRIER_TEST_POSTGRES_URL"


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
    """
    CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $$
        SELECT coalesce(
            nullif(current_setting('request.jwt.claim.sub', true), ''),
            (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
        )::uuid
    $$
    """,
    "GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role",
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


def install_shim(url: str) -> None:
    """Give the database at `url` the auth objects migration 10 needs."""
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for name, attributes in SHIM_ROLES:
            _create_role(conn, name, attributes)
        with conn.transaction():
            for statement in SHIM_STATEMENTS:
                conn.execute(statement.encode())


def new_owner(conn: PgConnection) -> str:
    """A synthetic owner: a random id in auth.users. Inserting it fires
    harrier_new_owner(), which gives the owner track 1 on a migrated store."""
    owner = str(uuid.uuid4())
    conn.execute("INSERT INTO auth.users (id) VALUES (%s)", (owner,))
    return owner


def claims(owner: str) -> str:
    """The JWT claims a request for `owner` carries, as auth.uid() reads them."""
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
    cannot bypass row security, unlike the superuser the tests connect as."""
    with as_role(conn, TENANT_ROLE, owner) as policed:
        yield policed


def set_session_owner(conn: PgConnection, owner: str) -> None:
    """Claims for the whole session, without changing role.

    The superuser still bypasses the policy; the claims only make each
    owner_id default to `owner`. The parity test needs that: it compares
    shapes and defaults, and the catalog test owns the policy (spec 105).
    """
    conn.execute("SELECT set_config('request.jwt.claims', %s, false)", (claims(owner),))


# --- databases ---


@contextmanager
def fresh_database(*, shim: bool = True) -> Generator[str]:
    """Create an empty database, yield its URL, then drop it.

    With the shim unless `shim=False`, which is a Postgres without
    Supabase's auth objects.
    """
    import psycopg

    admin = admin_url()
    name = f"harrier_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f"CREATE DATABASE {name}".encode())
    except psycopg.OperationalError as error:
        message = f"cannot reach the Postgres test server: {type(error).__name__}"
        if _in_ci():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    try:
        url = _with_database(admin, name)
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
