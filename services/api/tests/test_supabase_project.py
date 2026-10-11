"""Spec 105 holds on a real Supabase project (spec 115).

One test per assumption A1 to A11 that spec 105 rests on, run against a
real Supabase project migrated with `harrier store migrate`, plus spec
105's isolation tests from `tests/test_owner_policy.py` in staging mode:
the same test functions, with `store` and `store_url` yielding the project
instead of a fresh shimmed database.

Two URLs are needed, and the module skips, naming the one missing, without
either, locally and in CI:

- `HARRIER_SUPABASE_TEST_URL`: the project's `postgres` role through the
  session pooler, which migrated the store;
- `HARRIER_SUPABASE_API_TEST_URL`: the custom login role
  `harrier_api_probe` through the session pooler.

The module is a recorded check, not a gate (spec 039): CI has no project.
Staging mode refuses, before it writes anything, a project whose
auth.users holds a user with an email. The owners it creates are random
ids with no email, and they stay: the owner reference, append-only events
and tracks that are never deleted keep them (spec 115).

A failure here is a finding, recorded in spec 115's "Amendment (staging
findings)", not something to retry or work around.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
import test_owner_policy as owner_policy
from pg_support import (
    PROBE_ROLE,
    SESSION_POOLER_PORT,
    SHIM_STATEMENTS,
    STAGING_HAS_USERS,
    SUPABASE_API_URL_VARIABLE,
    SUPABASE_URL_VARIABLE,
    TRANSACTION_POOLER_PORT,
    StagingProjectRefused,
    as_role,
    claims,
    fresh_database,
    new_owner,
    non_superuser_migrator_problems,
    open_staging,
    supabase_urls,
    superuser_url,
    with_port,
)

from harrier.pgstore import migrate_postgres, postgres_version, target_version
from harrier.tracker.schema import TENANT_ROLE

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]


# --- staging mode ---


@pytest.fixture
def store_url() -> Iterator[str]:
    """The project, as its postgres role, once `open_staging` accepts it."""
    import psycopg

    url, _ = supabase_urls()
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        try:
            open_staging(conn)
        except StagingProjectRefused as refused:
            pytest.fail(str(refused), pytrace=False)
    yield url


@pytest.fixture
def store(store_url: str) -> Iterator[PgConnection]:
    """A connection to the project as postgres, in autocommit."""
    import psycopg

    with psycopg.connect(store_url, autocommit=True, connect_timeout=10) as conn:
        yield conn


@pytest.fixture
def api_url(store: PgConnection) -> str:
    """The probe role's URL, once postgres has granted it the tenant role
    (A10's statement; the probe needs it to SET ROLE in A5 and A7)."""
    _, url = supabase_urls()
    assert (urlsplit(url).username or "").split(".")[0] == PROBE_ROLE
    store.execute(f"GRANT {TENANT_ROLE} TO {PROBE_ROLE}".encode())
    return url


# The isolation tests spec 115 runs on the project, as test_owner_policy.py
# defines them. Collected here, they take this module's fixtures. Not run
# here, each for its reason: the migration 10 refusal tests need a store at
# version 9 or without the auth schema, and a project has one database with
# Supabase's schema in it; the catalog breach test drops a policy and FORCE,
# a schema change to a shared project; the SQLite test has nothing to do
# with Supabase.
test_every_table_is_owned_or_declared = owner_policy.test_every_table_is_owned_or_declared
test_owner_a_reads_nothing_of_owner_b = owner_policy.test_owner_a_reads_nothing_of_owner_b
test_owner_a_cannot_write_as_owner_b = owner_policy.test_owner_a_cannot_write_as_owner_b
test_an_insert_without_an_owner_is_refused = owner_policy.test_an_insert_without_an_owner_is_refused
test_the_data_api_role_reaches_no_harrier_table = (
    owner_policy.test_the_data_api_role_reaches_no_harrier_table
)
test_uniqueness_is_per_owner = owner_policy.test_uniqueness_is_per_owner
test_references_stay_inside_one_owner = owner_policy.test_references_stay_inside_one_owner
test_every_owner_starts_with_track_one = owner_policy.test_every_owner_starts_with_track_one
test_an_owner_files_a_job_with_the_defaults = (
    owner_policy.test_an_owner_files_a_job_with_the_defaults
)
test_a_bypassrls_role_sees_every_owner = owner_policy.test_a_bypassrls_role_sees_every_owner
test_truncate_cannot_empty_events_or_tracks = (
    owner_policy.test_truncate_cannot_empty_events_or_tracks
)


def observed(conn: PgConnection, statement: str, params: tuple[object, ...] = ()) -> Any:
    """The statement's rows, or the error it raised, as a value an assertion
    can show. Run in a savepoint, so the transaction around it goes on."""
    import psycopg

    try:
        with conn.transaction():
            return conn.execute(statement.encode(), params).fetchall()
    except psycopg.Error as error:
        return f"{error.sqlstate} {error.diag.message_primary}"


# --- the assumptions ---


def test_the_migrator_can_trigger_on_auth_users(store: PgConnection) -> None:
    """A1: migration 10 applied as postgres, so it created its trigger on
    auth.users, a table Supabase's auth role owns."""
    assert postgres_version(store) >= 10
    trigger = store.execute(
        "SELECT tgenabled FROM pg_trigger "
        "WHERE tgrelid = 'auth.users'::regclass AND tgname = 'harrier_new_owner'"
    ).fetchall()
    assert trigger == [("O",)]


def test_the_tenant_role_reaches_auth_uid(store: PgConnection) -> None:
    """A2: migration 10 grants the tenant role USAGE on auth and EXECUTE on
    auth.uid(). The policy and owner_id's default reach the function by its
    stored id; the trigger that numbers a track, and any statement that
    names it, look it up through the schema."""
    held = store.execute(
        "SELECT has_schema_privilege(%s, 'auth', 'USAGE'), "
        "has_function_privilege(%s, 'auth.uid()', 'EXECUTE')",
        (TENANT_ROLE, TENANT_ROLE),
    ).fetchone()
    assert held == (True, True), f"{TENANT_ROLE}: (USAGE on auth, EXECUTE on auth.uid()) = {held}"


def _normalized(body: str) -> str:
    return re.sub(r"\s+", "", body).lower()


def test_auth_uid_reads_the_claims_sub(store: PgConnection) -> None:
    """A3: the deployed auth.uid() has the body the shim copies, and with
    only request.jwt.claims set it returns the claims' sub, as
    authenticated and as the tenant role."""
    deployed = store.execute("SELECT prosrc FROM pg_proc WHERE oid = 'auth.uid()'::regprocedure")
    row = deployed.fetchone()
    assert row is not None
    shim = next(statement for statement in SHIM_STATEMENTS if "FUNCTION auth.uid()" in statement)
    shim_body = shim.split("$$")[1]
    assert _normalized(str(row[0])) == _normalized(shim_body)

    owner = str(uuid.uuid4())
    for role in ("authenticated", TENANT_ROLE):
        with as_role(store, role, owner):
            unset = store.execute("SELECT current_setting('request.jwt.claim.sub', true)")
            assert unset.fetchone() in ((None,), ("",))
            uid = observed(store, "SELECT auth.uid()::text")
        assert uid == [(owner,)], f"auth.uid() as {role}: {uid}"


def test_postgres_bypasses_but_is_not_superuser(store: PgConnection) -> None:
    """A4: the role that migrates is not a superuser, and has BYPASSRLS and
    CREATEROLE."""
    role = store.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
        "WHERE rolname = current_user"
    ).fetchone()
    assert role == ("postgres", False, True, True)


def test_the_pooler_keeps_a_custom_role_and_set_local(api_url: str) -> None:
    """A5: the probe role connects through the session pooler and the
    transaction pooler, and spec 104's read-back holds inside a
    transaction: SET LOCAL ROLE and the local claims are seen by the next
    statement, and end with the transaction."""
    import psycopg

    owner = str(uuid.uuid4())
    for port in (SESSION_POOLER_PORT, TRANSACTION_POOLER_PORT):
        with psycopg.connect(
            with_port(api_url, port), autocommit=True, connect_timeout=10, prepare_threshold=None
        ) as conn:
            assert conn.execute("SELECT current_user").fetchone() == (PROBE_ROLE,), port
            with conn.transaction():
                conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}".encode())
                conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims(owner),))
                read_back = conn.execute(
                    "SELECT current_user, "
                    "current_setting('request.jwt.claims', true)::jsonb ->> 'sub'"
                ).fetchone()
            assert read_back == (TENANT_ROLE, owner), port
            after = conn.execute(
                "SELECT current_user, coalesce(current_setting('request.jwt.claims', true), '')"
            ).fetchone()
            assert after == (PROBE_ROLE, ""), port


def test_unqualified_names_resolve_to_public_for_both_roles(
    store: PgConnection, api_url: str
) -> None:
    """A7: the migrator and the request role (the probe, as the tenant
    role) resolve the unqualified name `jobs` to public.jobs."""
    import psycopg

    query = (
        "SELECT current_user, current_schemas(true)::text, "
        "to_regclass('jobs') = 'public.jobs'::regclass"
    )
    migrator = store.execute(query.encode()).fetchone()
    with (
        psycopg.connect(api_url, autocommit=True, connect_timeout=10) as conn,
        conn.transaction(),
    ):
        conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}".encode())
        request = conn.execute(query.encode()).fetchone()
    configs = store.execute(
        "SELECT rolname, rolconfig::text FROM pg_roles WHERE rolname = ANY(%s) ORDER BY rolname",
        (["postgres", PROBE_ROLE, TENANT_ROLE],),
    ).fetchall()
    assert migrator is not None and request is not None
    assert (migrator[2], request[2]) == (True, True), (migrator, request, configs)
    assert request[0] == TENANT_ROLE


def test_the_migrator_can_create_an_auth_user(store: PgConnection) -> None:
    """A8: postgres inserts a user into auth.users, as the tests' new_owner
    does, and the trigger gives that owner track 1. The owner stays."""
    owner = new_owner(store)
    track = store.execute("SELECT id FROM tracks WHERE owner_id = %s", (owner,)).fetchall()
    assert track == [(1,)]


def test_the_migrator_can_grant_the_tenant_role(store: PgConnection) -> None:
    """A10: postgres, which created the tenant role and so holds its admin
    option, grants it to a login role. Any earlier grant to the probe is
    revoked first, so the grant here is a real one on every run."""
    import psycopg

    _, url = supabase_urls()
    assert (urlsplit(url).username or "").split(".")[0] == PROBE_ROLE
    store.execute(f"REVOKE {TENANT_ROLE} FROM {PROBE_ROLE}".encode())
    try:
        with store.transaction():
            store.execute(f"GRANT {TENANT_ROLE} TO {PROBE_ROLE}".encode())
        refused = None
    except psycopg.Error as error:
        refused = f"{error.sqlstate} {error.diag.message_primary}"
    assert refused is None, refused
    member = store.execute(
        "SELECT set_option FROM pg_auth_members "
        "WHERE roleid = %s::regrole AND member = %s::regrole",
        (TENANT_ROLE, PROBE_ROLE),
    ).fetchall()
    assert (True,) in member


def test_the_store_migrates_to_the_target_as_a_non_superuser_migrator(
    store: PgConnection,
) -> None:
    """A11: the store is at target_version() as postgres, which is not a
    superuser, including migration 14's role reset and default privilege
    revokes; postgres then holds no default privilege in public or global
    for anon, authenticated or service_role, and a function it creates in
    public is executable by none of them nor by PUBLIC."""
    assert postgres_version(store) == target_version()
    assert non_superuser_migrator_problems(store) == []


# --- staging mode's own rules (these run without a project) ---


def test_supabase_checks_skip_without_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Either URL unset skips the module, naming the variable, in CI too."""
    for ci in ("", "true"):
        monkeypatch.setenv("CI", ci)
        for missing in (SUPABASE_URL_VARIABLE, SUPABASE_API_URL_VARIABLE):
            for variable in (SUPABASE_URL_VARIABLE, SUPABASE_API_URL_VARIABLE):
                monkeypatch.setenv(variable, "postgresql://synthetic@127.0.0.1:1/none")
            monkeypatch.setenv(missing, "")
            with pytest.raises(pytest.skip.Exception) as skipped:
                supabase_urls()
            assert str(skipped.value).startswith(f"{missing} is not set"), (ci, missing)


def test_staging_mode_refuses_a_project_with_real_users() -> None:
    """A project whose auth.users holds a user with an email is refused
    before any write: the refusal leaves no transaction id assigned, which
    Postgres does on the first write, and grants nothing. Checked on a local
    shimmed store with Supabase's email column and one synthetic user, as
    the superuser: it holds no grant of the tenant role, so the grant
    `open_staging` would make were the check after it shows here."""
    import psycopg

    with fresh_database() as url:
        migrate_postgres(url)
        with psycopg.connect(superuser_url(url), autocommit=True) as conn:
            conn.execute("ALTER TABLE auth.users ADD COLUMN email text")
            conn.execute(
                "INSERT INTO auth.users (id, email) VALUES (%s, 'someone@example.com')",
                (str(uuid.uuid4()),),
            )
            members_before = conn.execute(
                "SELECT member, set_option FROM pg_auth_members WHERE roleid = %s::regrole",
                (TENANT_ROLE,),
            ).fetchall()
            with conn.transaction(force_rollback=True):
                with pytest.raises(StagingProjectRefused) as refused:
                    open_staging(conn)
                written = conn.execute("SELECT pg_current_xact_id_if_assigned()").fetchone()
            assert str(refused.value) == STAGING_HAS_USERS
            assert written == (None,)
            members_after = conn.execute(
                "SELECT member, set_option FROM pg_auth_members WHERE roleid = %s::regrole",
                (TENANT_ROLE,),
            ).fetchall()
            assert members_after == members_before
