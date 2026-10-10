"""Every personal table is owned and policed (spec 105, ADR-013 decision 1).

Migration 10 gives every owned table an owner_id and one forced row policy,
makes the global keys per owner, keeps references inside one owner, and
gives every owner a track 1. These tests prove it on plain Postgres with the
shim from `tests/pg_support.py` standing in for Supabase's auth objects.

Policed statements run as the tenant role through `as_owner`, never as the
superuser the tests connect as: a superuser bypasses row security whatever
FORCE says, so a test run as one would prove nothing about the policy.
Owners are random UUIDs; no personal data is involved.

The SQLite test runs everywhere. The Postgres tests need a server and skip
locally without one (`tests/pg_support.py`).
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from pg_support import (
    SHIM_STATEMENTS,
    as_owner,
    as_role,
    fresh_database,
    install_shim,
    new_owner,
)

from harrier.db import connect, schema_version
from harrier.pgstore import URL_VARIABLE, StoreMigrationRefused, migrate_postgres
from harrier.tracker import schema
from harrier.tracker.schema import (
    GLOBAL_ID_TABLES,
    MIGRATION_10_NEEDS_AUTH,
    MIGRATION_12_TENANT_HAS_A_ROLE,
    MIGRATIONS,
    OWNED_TABLES,
    POSTGRES_BASELINE_VERSION,
    SUPABASE_ROLES,
    TENANT_ROLE,
    UNOWNED_TABLES,
    auth_precheck,
)
from harrier_cli.main import main

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

# SQLSTATEs, so a refusal is told apart by its class rather than its text.
INSUFFICIENT_PRIVILEGE = "42501"  # a missing grant, or a row the policy refuses
NOT_NULL = "23502"
FOREIGN_KEY = "23503"
UNIQUE = "23505"
RAISED = "P0001"  # harrier_refuse()

# The policy's expression, as Postgres 17 prints it back.
POLICY_EXPRESSION = "(owner_id = ( SELECT auth.uid() AS uid))"

# What the tenant role may do on each whole table: spec 105's Grants list,
# with the tenant role in the place of `authenticated` (spec 105 amendment).
# On a table with a global id it writes columns, not the table (below).
EXPECTED_GRANTS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ("jobs", "contacts", "profile_documents", "user_config"),
        frozenset({"SELECT", "DELETE"}),
    ),
    "job_runs": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "job_events": frozenset({"SELECT"}),
    "tracks": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "schema_version": frozenset({"SELECT"}),
}

# What the tenant role may write on every column but id of a table whose id
# is global: the writes its Grants list gives, so an owner can neither pick
# an id nor change one (spec 105, post-merge review).
EXPECTED_COLUMN_GRANTS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ("jobs", "contacts", "profile_documents", "user_config"),
        frozenset({"INSERT", "UPDATE"}),
    ),
    "job_events": frozenset({"INSERT"}),
}

# Roles that must hold nothing on any harrier table, column or sequence.
# anon and authenticated are the Data API's roles; service_role bypasses the
# policy.
NO_PRIVILEGE_ROLES = ("anon", "authenticated", "service_role")

# The attributes the tenant role must not have. INHERIT would hand it the
# privileges of any role it were made a member of.
TENANT_ATTRIBUTES = (
    ("rolsuper", "SUPERUSER"),
    ("rolbypassrls", "BYPASSRLS"),
    ("rolcanlogin", "LOGIN"),
    ("rolinherit", "INHERIT"),
    ("rolcreatedb", "CREATEDB"),
    ("rolcreaterole", "CREATEROLE"),
    ("rolreplication", "REPLICATION"),
)

# pg_default_acl's object kinds.
DEFAULT_ACL_KINDS = {
    "r": "tables",
    "S": "sequences",
    "f": "functions",
    "T": "types",
    "n": "schemas",
}

# The columns each owned table requires, with synthetic values. A job
# event also names a job of its owner, filled in by `values_for`.
REQUIRED: dict[str, dict[str, object]] = {
    "job_events": {"kind": "created", "actor": "system", "to_status": "prospect"},
    "jobs": {},
    "contacts": {},
    "profile_documents": {"kind": "truth", "name": "synthetic"},
    "user_config": {"kind": "synthetic"},
    "job_runs": {"job": "synthetic", "last_success_at": "2026-10-10 00:00:00"},
    "tracks": {"slug": "synthetic", "kind": "industry", "label": "Synthetic"},
}


# --- helpers ---


@pytest.fixture
def store_url() -> Iterator[str]:
    """A fresh shimmed database at the latest version."""
    with fresh_database() as url:
        migrate_postgres(url)
        yield url


@pytest.fixture
def store(store_url: str) -> Iterator[PgConnection]:
    """A superuser connection to `store_url`, in autocommit."""
    import psycopg

    with psycopg.connect(store_url, autocommit=True) as conn:
        yield conn


def values_for(table: str, job_id: int | None = None) -> dict[str, object]:
    values = dict(REQUIRED[table])
    if table == "job_events":
        values["job_id"] = job_id
    return values


def insert(conn: PgConnection, table: str, values: dict[str, object]) -> None:
    if not values:
        conn.execute(f"INSERT INTO {table} DEFAULT VALUES".encode())
        return
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})".encode(),
        tuple(values.values()),
    )


def fill(conn: PgConnection, owner: str) -> int:
    """One row in every owned table, written as `owner`. Returns the job's id.

    The tracks row is the owner's second track; track 1 came with the owner.
    """
    with as_owner(conn, owner):
        row = conn.execute("INSERT INTO jobs DEFAULT VALUES RETURNING id").fetchone()
        assert row is not None
        job_id = int(row[0])
        for table in OWNED_TABLES:
            if table != "jobs":
                insert(conn, table, values_for(table, job_id))
    return job_id


def refusal(conn: PgConnection, statement: str, params: tuple[object, ...] = ()) -> Any:
    """The error `statement` raises, run in a savepoint so the transaction
    around it goes on. Fails the test if the statement is accepted."""
    import psycopg

    try:
        with conn.transaction():
            conn.execute(statement.encode(), params)
    except psycopg.Error as error:
        return error
    pytest.fail(f"accepted, expected a refusal: {statement}")


def snapshot(conn: PgConnection) -> dict[str, list[tuple[Any, ...]]]:
    """Every owned table's rows, as the superuser sees them."""
    return {
        table: sorted(
            conn.execute(f"SELECT * FROM {table}".encode()).fetchall(), key=lambda row: repr(row)
        )
        for table in OWNED_TABLES
    }


def migration(version: int) -> list[str]:
    """One Postgres migration's statements."""
    return next(
        statements for number, statements in schema.POSTGRES_MIGRATIONS if number == version
    )


def migrate_to(url: str, version: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrate the store at `url` up to `version` and no further."""
    real = list(schema.POSTGRES_MIGRATIONS)
    with monkeypatch.context() as patch:
        patch.setattr(schema, "POSTGRES_MIGRATIONS", [m for m in real if m[0] <= version])
        migrate_postgres(url)


def owners_seen(conn: PgConnection, table: str) -> set[UUID]:
    rows = conn.execute(f"SELECT DISTINCT owner_id FROM {table}".encode()).fetchall()
    return {row[0] for row in rows}


# --- the local store ---


def test_the_local_schema_names_no_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite records version 10 and changes nothing: no owner column, no
    users table, fresh or brought forward from version 9 (ADR-012 point 3).
    Later migrations are left out, so "nothing else" is migration 10's; a
    fresh store at the newest version names no tenant either."""
    with closing(connect(tmp_path / "newest.db")) as conn:
        for table in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table[0]})")}
            assert not columns & {"owner_id", "owner", "tenant_id"}, table[0]
    monkeypatch.setattr(schema, "MIGRATIONS", [(v, s) for v, s in MIGRATIONS if v <= 10])
    latest = 10
    at_nine = tmp_path / "nine.db"
    with closing(sqlite3.connect(at_nine)) as raw:
        raw.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for version, statements in MIGRATIONS:
            if version > POSTGRES_BASELINE_VERSION:
                continue
            for statement in statements:
                raw.execute(statement)
            raw.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
        raw.commit()
        schema_at_nine = raw.execute("SELECT type, name, sql FROM sqlite_master").fetchall()

    for path in (tmp_path / "fresh.db", at_nine):
        with closing(connect(path)) as conn:
            assert schema_version(conn) == latest
            assert 10 in {row[0] for row in conn.execute("SELECT version FROM schema_version")}
            tables = [
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            ]
            assert "users" not in tables
            for table in tables:
                columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                assert not columns & {"owner_id", "owner", "tenant_id"}, table

    # Bringing it forward wrote a version row and nothing else.
    with closing(sqlite3.connect(at_nine)) as raw:
        after = raw.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    assert after == schema_at_nine


# --- migration 10's refusals ---


def test_migration_10_needs_supabase_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import psycopg

    with fresh_database(shim=False) as url:
        monkeypatch.setenv(URL_VARIABLE, url)
        assert main(["store", "migrate"]) == 1
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", f"error: {MIGRATION_10_NEEDS_AUTH}\n")
        with psycopg.connect(url, autocommit=True) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
            owner_columns = conn.execute(
                "SELECT count(*) FROM information_schema.columns WHERE column_name = 'owner_id'"
            ).fetchone()
        assert versions == [POSTGRES_BASELINE_VERSION]
        assert owner_columns == (0,)


@pytest.mark.parametrize("table", OWNED_TABLES)
def test_migration_10_refuses_ownerless_rows(table: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store at version 9 holding a row nobody owns stays at version 9.
    The seeded track 1 alone is not such a row: every other test migrates
    a store that has it."""
    import psycopg

    real = list(schema.POSTGRES_MIGRATIONS)
    with fresh_database() as url:
        monkeypatch.setattr(
            schema,
            "POSTGRES_MIGRATIONS",
            [migration for migration in real if migration[0] <= POSTGRES_BASELINE_VERSION],
        )
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            job_id = None
            if table == "job_events":
                row = conn.execute("INSERT INTO jobs DEFAULT VALUES RETURNING id").fetchone()
                assert row is not None
                job_id = int(row[0])
            insert(conn, table, values_for(table, job_id))
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", real)

        with pytest.raises(StoreMigrationRefused) as refused:
            migrate_postgres(url)
        assert str(refused.value) == (
            f"migration 10 found rows in {table} with no owner; it cannot choose one"
        )
        with psycopg.connect(url, autocommit=True) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [POSTGRES_BASELINE_VERSION]


@pytest.mark.parametrize("missing", ["CREATE TABLE auth.users", "CREATE FUNCTION auth.uid()"])
def test_migration_10_needs_both_auth_objects(missing: str) -> None:
    """A partial shim: the roles, and one of auth.users and auth.uid().
    Refused with the precheck's message, before any statement that would
    fail on the missing object with a driver error of its own."""
    import psycopg

    partial = tuple(statement for statement in SHIM_STATEMENTS if missing not in statement)
    assert len(partial) == len(SHIM_STATEMENTS) - 1
    with fresh_database(shim=False) as url:
        install_shim(url, partial)
        with pytest.raises(StoreMigrationRefused) as refused:
            migrate_postgres(url)
        assert str(refused.value) == MIGRATION_10_NEEDS_AUTH
        with psycopg.connect(url, autocommit=True) as conn:
            versions = [row[0] for row in conn.execute("SELECT version FROM schema_version")]
        assert versions == [POSTGRES_BASELINE_VERSION]


def test_migration_10_needs_every_supabase_role() -> None:
    """Roles belong to the whole server, so a test cannot drop a real one.
    The precheck takes the role names instead; naming one that does not
    exist, in each position, is refused with the precheck's message."""
    import psycopg

    assert migration(10)[0] == auth_precheck()
    absent = f"harrier_test_absent_{uuid.uuid4().hex[:12]}"
    with fresh_database() as url, psycopg.connect(url, autocommit=True) as conn:
        conn.execute(auth_precheck().encode())
        for position in range(len(SUPABASE_ROLES)):
            roles = (*SUPABASE_ROLES[:position], absent, *SUPABASE_ROLES[position + 1 :])
            with pytest.raises(psycopg.errors.RaiseException) as raised:
                conn.execute(auth_precheck(roles).encode())
            assert raised.value.diag.message_primary == MIGRATION_10_NEEDS_AUTH, roles


def test_migration_10_gives_existing_users_track_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user in auth.users before migration 10 gets no trigger's track 1:
    the trigger comes with the migration. Migration 10 seeds it instead."""
    import psycopg

    with fresh_database() as url:
        migrate_to(url, POSTGRES_BASELINE_VERSION, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            owner = new_owner(conn)
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            every_track = conn.execute("SELECT owner_id, id FROM tracks").fetchall()
            with as_owner(conn, owner):
                tracks = conn.execute("SELECT id, slug, kind, label FROM tracks").fetchall()
                job = conn.execute(
                    "INSERT INTO jobs DEFAULT VALUES RETURNING owner_id, track_id"
                ).fetchone()
        assert every_track == [(UUID(owner), 1)]
        assert tracks == [(1, "job", "industry", "Job search")]
        assert job == (UUID(owner), 1)


def catalog_fingerprint(conn: PgConnection) -> dict[str, list[tuple[Any, ...]]]:
    """What a migration can change: relations, columns, keys, functions,
    triggers, policies, grants, default privileges, and the seeded rows."""
    queries = {
        "relations": "SELECT relname, relkind, relacl::text, relrowsecurity, "
        "relforcerowsecurity, pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace",
        "columns": "SELECT attrelid::regclass::text, attname, format_type(atttypid, atttypmod), "
        "attnotnull, attidentity, attacl::text FROM pg_attribute WHERE attrelid IN "
        "(SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace) "
        "AND attnum > 0 AND NOT attisdropped",
        "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
        "FROM pg_constraint WHERE connamespace = 'public'::regnamespace",
        "functions": "SELECT proname, prosrc, prosecdef, proconfig, proacl::text FROM pg_proc "
        "WHERE pronamespace = 'public'::regnamespace",
        "triggers": "SELECT tgrelid::regclass::text, tgname, tgenabled FROM pg_trigger "
        "WHERE NOT tgisinternal",
        "policies": "SELECT polrelid::regclass::text, polname FROM pg_policy",
        "default privileges": "SELECT defaclrole::regrole::text, "
        "defaclnamespace::regnamespace::text, defaclobjtype, defaclacl::text "
        "FROM pg_default_acl",
        "schemas": "SELECT nspname, nspacl::text FROM pg_namespace "
        "WHERE nspname IN ('auth', 'public')",
        "auth.uid()": "SELECT proacl::text FROM pg_proc WHERE oid = 'auth.uid()'::regprocedure",
        "tracks": "SELECT row_to_json(t)::text FROM tracks t",
    }
    return {
        name: sorted(conn.execute(query.encode()).fetchall(), key=repr)
        for name, query in queries.items()
    }


@pytest.mark.parametrize("version", [10, 13])
def test_a_failed_migration_leaves_no_trace_of_itself(
    version: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every statement of the migration runs, then one more fails: the
    hardest point to roll back from. The store stays at the version before,
    with its catalog exactly as it was (spec 090's rule, as spec 103 applies
    it)."""
    import psycopg

    real = list(schema.POSTGRES_MIGRATIONS)
    with fresh_database() as url:
        migrate_to(url, version - 1, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            new_owner(conn)
            before = catalog_fingerprint(conn)
        broken = [
            (number, [*statements, "SELECT 1 / 0"] if number == version else statements)
            for number, statements in real
        ]
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", broken)
        with pytest.raises(psycopg.errors.DivisionByZero):
            migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            latest = conn.execute("SELECT max(version) FROM schema_version").fetchone()
            after = catalog_fingerprint(conn)
        assert latest == (version - 1,)
        assert after == before


# --- migration 13: the tenant role ---


def tenant_attributes(conn: PgConnection) -> tuple[bool, ...]:
    row = conn.execute(
        f"SELECT {', '.join(column for column, _ in TENANT_ATTRIBUTES)} "
        "FROM pg_roles WHERE rolname = %s".encode(),
        (TENANT_ROLE,),
    ).fetchone()
    assert row is not None
    return tuple(bool(value) for value in row)


def test_migration_13_resets_the_tenant_roles_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migration 10 creates harrier_tenant only when it is absent, so a role
    someone made first keeps whatever it has. Migration 13 sets it back.
    The role belongs to the whole server: the bad attributes live inside a
    transaction that is rolled back, so no other database or test sees
    them, and the real role is checked unchanged afterwards."""
    import psycopg

    none = (False,) * len(TENANT_ATTRIBUTES)
    every = " ".join(attribute for _, attribute in TENANT_ATTRIBUTES)
    with fresh_database() as url:
        migrate_to(url, 11, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            assert tenant_attributes(conn) == none
            with conn.transaction(force_rollback=True):
                conn.execute(f"ALTER ROLE {TENANT_ROLE} {every}".encode())
                assert tenant_attributes(conn) == (True,) * len(TENANT_ATTRIBUTES)
                for statement in migration(13):
                    conn.execute(statement.encode())
                assert tenant_attributes(conn) == none
            assert tenant_attributes(conn) == none


def test_migration_13_refuses_a_tenant_role_that_is_a_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NOINHERIT keeps a member role's privileges from flowing in, but the
    tenant could still SET ROLE to it. Migration 13 refuses rather than
    guess whether the grant was meant. The other role and the grant live in
    a transaction that is rolled back."""
    import psycopg

    other = f"harrier_test_{uuid.uuid4().hex[:12]}"
    with fresh_database() as url:
        migrate_to(url, 11, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            with conn.transaction(force_rollback=True):
                conn.execute(f"CREATE ROLE {other} NOLOGIN".encode())
                conn.execute(f"GRANT {other} TO {TENANT_ROLE}".encode())
                with pytest.raises(psycopg.errors.RaiseException) as raised:
                    for statement in migration(13):
                        conn.execute(statement.encode())
                assert raised.value.diag.message_primary == MIGRATION_12_TENANT_HAS_A_ROLE
            left = conn.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (other,))
            assert left.fetchone() == (0,)


# --- the catalog ---


def catalog_problems(conn: PgConnection) -> list[str]:
    """Every way the store's catalog breaks spec 105's rules, each naming
    the table, column, role or function.

    Run as the role that migrated the store, which owns every table."""
    problems: list[str] = []
    migrator_row = conn.execute("SELECT current_user").fetchone()
    assert migrator_row is not None
    migrator = str(migrator_row[0])
    relations = conn.execute(
        "SELECT relname, relkind, relrowsecurity, relforcerowsecurity, "
        "pg_get_userbyid(relowner) FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p', 'f', 'v', 'm')"
    ).fetchall()
    tables: set[str] = set()
    for name, kind, secured, forced, owner in relations:
        if kind in ("v", "m"):
            # A view made by the migrating role runs with its rights and
            # reads past the policy.
            problems.append(f"{name}: a view in public")
            continue
        tables.add(name)
        if name not in OWNED_TABLES and name not in UNOWNED_TABLES:
            problems.append(f"{name}: in neither OWNED_TABLES nor UNOWNED_TABLES")
        if not secured:
            problems.append(f"{name}: row security is not enabled")
        if not forced:
            problems.append(f"{name}: row security is not forced")
        # A table's owner may turn its row security off. Only the role that
        # runs the migrations may own one.
        if owner != migrator:
            problems.append(f"{name}: owned by {owner}, not {migrator}")
    for name in (*OWNED_TABLES, *UNOWNED_TABLES):
        if name not in tables:
            problems.append(f"{name}: declared, but no such table")

    for table in sorted(tables & set(OWNED_TABLES)):
        column = conn.execute(
            "SELECT format_type(a.atttypid, a.atttypmod), a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE a.attrelid = %s::regclass AND a.attname = 'owner_id' AND NOT a.attisdropped",
            (table,),
        ).fetchone()
        if column != ("uuid", True, "auth.uid()"):
            problems.append(f"{table}: owner_id is {column}, not uuid NOT NULL DEFAULT auth.uid()")
        references = conn.execute(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f' "
            "AND pg_get_constraintdef(oid) = 'FOREIGN KEY (owner_id) REFERENCES auth.users(id)'",
            (table,),
        ).fetchone()
        if references != (1,):
            problems.append(f"{table}: owner_id does not reference auth.users (id)")
        policies = conn.execute(
            "SELECT polname, polpermissive, polcmd::text, polroles::regrole[]::text[], "
            "pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
            "FROM pg_policy WHERE polrelid = %s::regclass",
            (table,),
        ).fetchall()
        expected = ("owner_only", True, "*", [TENANT_ROLE], POLICY_EXPRESSION, POLICY_EXPRESSION)
        if [tuple(policy) for policy in policies] != [expected]:
            problems.append(f"{table}: policies are {policies}, not exactly owner_only")
        indexed = conn.execute(
            "SELECT count(*) FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_am am ON am.oid = ic.relam "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            "WHERE i.indrelid = %s::regclass AND i.indpred IS NULL "
            "AND am.amname = 'btree' AND a.attname = 'owner_id'",
            (table,),
        ).fetchone()
        if indexed is None or indexed[0] == 0:
            problems.append(f"{table}: no whole-table btree index leads with owner_id")
        # A unique key checks every owner's rows, so a key without owner_id
        # first tells one owner what another holds. The one exception is a
        # global id the tenant role cannot write (the column grants below).
        for index, primary, columns in conn.execute(
            "SELECT ic.relname, i.indisprimary, "
            "array(SELECT coalesce(a.attname, '(expression)') FROM unnest(i.indkey) WITH "
            "ORDINALITY k(attnum, n) LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "AND a.attnum = k.attnum ORDER BY k.n) "
            "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
            "WHERE i.indrelid = %s::regclass AND i.indisunique",
            (table,),
        ).fetchall():
            if columns[:1] == ["owner_id"]:
                continue
            if primary and columns == ["id"] and table in GLOBAL_ID_TABLES:
                continue
            problems.append(f"{table}: unique index {index} does not lead with owner_id")

    if "schema_version" in tables:
        policies = conn.execute(
            "SELECT polname, polpermissive, polcmd::text, polroles::regrole[]::text[], "
            "pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
            "FROM pg_policy WHERE polrelid = 'schema_version'::regclass"
        ).fetchall()
        expected_version = ("version_readable", True, "r", [TENANT_ROLE], "true", None)
        if [tuple(policy) for policy in policies] != [expected_version]:
            problems.append(f"schema_version: policies are {policies}")

    # A rule rewrites a statement before the policy sees it.
    for table, rule in conn.execute(
        "SELECT c.relname, r.rulename FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p')"
    ).fetchall():
        problems.append(f"{table}: rule {rule}")

    # Privileges on every table and sequence. Grantee 0 is PUBLIC.
    granted: dict[tuple[str, str], set[str]] = {}
    for name, kind, grantee, privilege in conn.execute(
        "SELECT c.relname, c.relkind, CASE WHEN a.grantee = 0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END, a.privilege_type "
        "FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'S') "
        "AND a.grantee <> c.relowner"
    ).fetchall():
        granted.setdefault(
            (str(grantee), f"{name} ({'sequence' if kind == 'S' else 'table'})"), set()
        ).add(str(privilege))
    for (grantee, relation), privileges in sorted(granted.items()):
        if grantee in (*NO_PRIVILEGE_ROLES, "PUBLIC"):
            problems.append(f"{relation}: {grantee} holds {sorted(privileges)}")
    for table in sorted(tables):
        held = frozenset(granted.get((TENANT_ROLE, f"{table} (table)"), set()))
        if held != EXPECTED_GRANTS.get(table, frozenset()):
            problems.append(f"{table}: {TENANT_ROLE} holds {sorted(held)}")
    for (grantee, relation), privileges in sorted(granted.items()):
        if grantee == TENANT_ROLE and relation.endswith("(sequence)"):
            problems.append(f"{relation}: {TENANT_ROLE} holds {sorted(privileges)}")

    # Privileges on single columns, which the table's ACL does not show.
    column_grants: dict[tuple[str, str, str], set[str]] = {}
    every_column = [
        (str(table), str(column))
        for table, column in conn.execute(
            "SELECT c.relname, a.attname FROM pg_class c "
            "JOIN pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ).fetchall()
    ]
    for table, column, grantee, privilege in conn.execute(
        "SELECT c.relname, a.attname, CASE WHEN x.grantee = 0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(x.grantee) END, x.privilege_type FROM pg_class c "
        "JOIN pg_attribute a ON a.attrelid = c.oid CROSS JOIN LATERAL aclexplode(a.attacl) x "
        "WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p') "
        "AND a.attnum > 0 AND NOT a.attisdropped AND x.grantee <> c.relowner"
    ).fetchall():
        column_grants.setdefault((str(grantee), str(table), str(column)), set()).add(str(privilege))
    for (grantee, table, column), privileges in sorted(column_grants.items()):
        if grantee in (*NO_PRIVILEGE_ROLES, "PUBLIC"):
            problems.append(f"{table}.{column}: {grantee} holds {sorted(privileges)}")
    for table, column in sorted(every_column):
        held = frozenset(column_grants.get((TENANT_ROLE, table, column), set()))
        wanted = (
            frozenset[str]()
            if column == "id"
            else EXPECTED_COLUMN_GRANTS.get(table, frozenset[str]())
        )
        if held != wanted:
            problems.append(f"{table}.{column}: {TENANT_ROLE} holds {sorted(held)}")

    # The tenant role: no attribute that widens it, and no role it could
    # SET ROLE to.
    role = conn.execute(
        f"SELECT {', '.join(column for column, _ in TENANT_ATTRIBUTES)} "
        "FROM pg_roles WHERE rolname = %s".encode(),
        (TENANT_ROLE,),
    ).fetchone()
    if role is None:
        problems.append(f"{TENANT_ROLE}: no such role")
    else:
        for (_, attribute), has in zip(TENANT_ATTRIBUTES, role, strict=True):
            if has:
                problems.append(f"{TENANT_ROLE}: has {attribute}")
    for (member_of,) in conn.execute(
        "SELECT roleid::regrole::text FROM pg_auth_members WHERE member = %s::regrole",
        (TENANT_ROLE,),
    ).fetchall():
        problems.append(f"{TENANT_ROLE}: a member of {member_of}")

    # The schema itself: nobody but its owner creates in it, and nothing
    # the migrating role creates later is granted to a policed or Data API
    # role by default.
    for grantee in (*NO_PRIVILEGE_ROLES, TENANT_ROLE):
        creates = conn.execute(
            "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (grantee,)
        ).fetchone()
        if creates != (False,):
            problems.append(f"public: {grantee} may CREATE")
    for kind, grantee, privilege in conn.execute(
        "SELECT d.defaclobjtype, pg_get_userbyid(x.grantee), x.privilege_type "
        "FROM pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) x "
        "WHERE d.defaclrole = current_user::regrole "
        "AND d.defaclnamespace IN (0, 'public'::regnamespace) AND x.grantee <> 0"
    ).fetchall():
        if grantee in (*NO_PRIVILEGE_ROLES, TENANT_ROLE):
            problems.append(
                f"default privileges: {grantee} gets {privilege} "
                f"on new {DEFAULT_ACL_KINDS.get(str(kind), str(kind))}"
            )

    for name, definer, config in conn.execute(
        "SELECT proname, prosecdef, proconfig FROM pg_proc "
        "WHERE pronamespace = 'public'::regnamespace"
    ).fetchall():
        if not definer:
            continue
        if name != "harrier_new_owner":
            problems.append(f"{name}(): SECURITY DEFINER in public")
        # Without its own empty search_path, a definer function resolves
        # names through the caller's, which the caller controls.
        if 'search_path=""' not in (config or []):
            problems.append(f"{name}(): SECURITY DEFINER without an empty search_path")
    for grantee in (*NO_PRIVILEGE_ROLES, TENANT_ROLE):
        for (name,) in conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace "
            "AND has_function_privilege(%s, oid, 'EXECUTE')",
            (grantee,),
        ).fetchall():
            problems.append(f"{name}(): executable by {grantee}")
    return sorted(problems)


def test_every_table_is_owned_or_declared(store: PgConnection) -> None:
    assert catalog_problems(store) == []
    # FORCE is set on every owned table, though the superuser running this
    # bypasses it (spec 105, Honest limitations).
    forced = store.execute(
        "SELECT relname, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
    ).fetchall()
    assert {name for name, is_forced in forced if is_forced} >= set(OWNED_TABLES)


@pytest.mark.parametrize(
    ("breach", "named"),
    [
        (
            "ALTER TABLE contacts NO FORCE ROW LEVEL SECURITY",
            "contacts: row security is not forced",
        ),
        ("DROP POLICY owner_only ON user_config", "user_config: policies are []"),
        ("CREATE TABLE notes (id bigint PRIMARY KEY)", "notes: in neither"),
        (
            "CREATE POLICY wide ON jobs FOR SELECT TO harrier_tenant USING (true)",
            "jobs: policies are",
        ),
        ("CREATE VIEW every_job AS SELECT * FROM jobs", "every_job: a view in public"),
        (
            "CREATE FUNCTION peek() RETURNS bigint LANGUAGE sql SECURITY DEFINER "
            "AS 'SELECT count(*) FROM jobs'",
            "peek(): SECURITY DEFINER in public",
        ),
        ("GRANT SELECT ON jobs TO authenticated", "jobs (table): authenticated holds"),
        ("GRANT SELECT ON jobs TO anon", "jobs (table): anon holds"),
        ("GRANT TRUNCATE ON job_events TO harrier_tenant", "job_events: harrier_tenant holds"),
        # The post-merge review's gaps (spec 105).
        ("GRANT UPDATE (url) ON jobs TO authenticated", "jobs.url: authenticated holds"),
        ("GRANT SELECT (url) ON jobs TO service_role", "jobs.url: service_role holds"),
        ("GRANT INSERT (id) ON jobs TO harrier_tenant", "jobs.id: harrier_tenant holds"),
        ("GRANT INSERT ON contacts TO harrier_tenant", "contacts: harrier_tenant holds"),
        (
            "GRANT EXECUTE ON FUNCTION harrier_number_track() TO service_role",
            "harrier_number_track(): executable by service_role",
        ),
        (
            "CREATE UNIQUE INDEX global_url ON jobs (url)",
            "jobs: unique index global_url does not lead with owner_id",
        ),
        ("ALTER TABLE contacts OWNER TO authenticated", "contacts: owned by authenticated"),
        ("ALTER ROLE harrier_tenant INHERIT", "harrier_tenant: has INHERIT"),
        ("GRANT authenticated TO harrier_tenant", "harrier_tenant: a member of authenticated"),
        ("GRANT CREATE ON SCHEMA public TO anon", "public: anon may CREATE"),
        (
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO anon",
            "default privileges: anon gets SELECT on new tables",
        ),
        ("CREATE RULE keep AS ON DELETE TO contacts DO INSTEAD NOTHING", "contacts: rule keep"),
        (
            "ALTER FUNCTION harrier_new_owner() RESET search_path",
            "harrier_new_owner(): SECURITY DEFINER without an empty search_path",
        ),
    ],
)
def test_the_catalog_names_each_breach(store: PgConnection, breach: str, named: str) -> None:
    """Each rule of the catalog test, broken once: the check names it."""
    with store.transaction(force_rollback=True):
        store.execute(breach.encode())
        problems = catalog_problems(store)
    assert any(problem.startswith(named) for problem in problems), problems


# --- isolation ---


def test_owner_a_reads_nothing_of_owner_b(store: PgConnection) -> None:
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    fill(store, b)
    before = snapshot(store)
    # Refused by a missing grant before the policy is reached; every other
    # update and delete runs and finds nothing of B's.
    not_granted = {("job_events", "UPDATE"), ("job_events", "DELETE"), ("tracks", "DELETE")}

    with as_owner(store, a):
        for table in OWNED_TABLES:
            assert owners_seen(store, table) == {UUID(a)}, table
            for verb, statement in (
                ("UPDATE", f"UPDATE {table} SET owner_id = owner_id WHERE owner_id = %s"),
                ("DELETE", f"DELETE FROM {table} WHERE owner_id = %s"),
            ):
                if (table, verb) in not_granted:
                    error = refusal(store, statement, (b,))
                    assert error.sqlstate == INSUFFICIENT_PRIVILEGE, (table, verb)
                    assert error.diag.message_primary == f"permission denied for table {table}"
                    continue
                cursor = store.execute(statement.encode(), (b,))
                assert cursor.rowcount == 0, (table, verb)

    assert snapshot(store) == before


def test_owner_a_cannot_write_as_owner_b(store: PgConnection) -> None:
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    b_job = fill(store, b)
    before = snapshot(store)

    with as_owner(store, a):
        for table in OWNED_TABLES:
            values = {**values_for(table, b_job), "owner_id": b}
            if table == "tracks":
                values["slug"] = "as-other-owner"
            columns = ", ".join(values)
            placeholders = ", ".join(["%s"] * len(values))
            error = refusal(
                store,
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
            assert error.sqlstate == INSUFFICIENT_PRIVILEGE, table
            assert error.diag.message_primary == (
                f'new row violates row-level security policy for table "{table}"'
            )

            error = refusal(store, f"UPDATE {table} SET owner_id = %s", (b,))
            assert error.sqlstate == INSUFFICIENT_PRIVILEGE, table
            # job_events has no UPDATE grant, so the update is refused
            # before the policy is asked.
            assert error.diag.message_primary == (
                "permission denied for table job_events"
                if table == "job_events"
                else f'new row violates row-level security policy for table "{table}"'
            )

    assert snapshot(store) == before


def insert_returning_id(conn: PgConnection, table: str, values: dict[str, object]) -> Any:
    if not values:
        return conn.execute(f"INSERT INTO {table} DEFAULT VALUES RETURNING id".encode()).fetchone()
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    return conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) RETURNING id".encode(),
        tuple(values.values()),
    ).fetchone()


def test_an_owner_cannot_choose_or_change_an_id(store: PgConnection) -> None:
    """These tables number every owner's rows from one sequence. An owner
    who could write an id would learn another owner's ids from the
    duplicate refusal, and could take the id another owner's next insert
    is due. The tenant role writes every column but id (spec 105,
    post-merge review)."""
    a, b = new_owner(store), new_owner(store)
    a_job = fill(store, a)
    b_job = fill(store, b)
    # Values A has not used, so only the id could refuse the row.
    unused: dict[str, dict[str, object]] = {
        "profile_documents": {"name": "explicit-id"},
        "user_config": {"kind": "explicit-id"},
    }
    due: dict[str, int] = {}
    for table in GLOBAL_ID_TABLES:
        row = store.execute(f"SELECT max(id) FROM {table}".encode()).fetchone()
        assert row is not None
        due[table] = int(row[0]) + 1
    before = snapshot(store)

    with as_owner(store, a):
        for table in GLOBAL_ID_TABLES:
            values = {**values_for(table, a_job), **unused.get(table, {}), "id": due[table]}
            columns = ", ".join(values)
            placeholders = ", ".join(["%s"] * len(values))
            for statement, params in (
                (
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                ),
                (f"UPDATE {table} SET id = %s", (due[table],)),
            ):
                error = refusal(store, statement, params)
                assert error.sqlstate == INSUFFICIENT_PRIVILEGE, statement
                assert error.diag.message_primary == f"permission denied for table {table}"
    assert snapshot(store) == before

    # B's next row in each table gets the id A tried to take.
    with as_owner(store, b):
        for table in GLOBAL_ID_TABLES:
            values = {**values_for(table, b_job), **unused.get(table, {})}
            assert insert_returning_id(store, table, values) == (due[table],), table


def test_an_insert_without_an_owner_is_refused(store: PgConnection) -> None:
    """No claims, so auth.uid() is null and owner_id defaults to null."""
    new_owner(store)
    before = snapshot(store)

    for table in OWNED_TABLES:
        values = values_for(table, job_id=1)
        columns = ", ".join(values) or ""
        statement = (
            f"INSERT INTO {table} ({columns}) VALUES ({', '.join(['%s'] * len(values))})"
            if values
            else f"INSERT INTO {table} DEFAULT VALUES"
        )
        params = tuple(values.values())

        # As the tenant role: the policy's WITH CHECK runs before NOT NULL,
        # and null = null is not true, so the policy refuses it.
        with as_role(store, TENANT_ROLE):
            uid = store.execute("SELECT auth.uid()").fetchone()
            assert uid == (None,)
            error = refusal(store, statement, params)
            assert error.sqlstate == INSUFFICIENT_PRIVILEGE, table
            assert "row-level security" in error.diag.message_primary

        # As service_role: it holds no privilege, so nothing is reached.
        with as_role(store, "service_role"):
            error = refusal(store, statement, params)
            assert error.diag.message_primary == f"permission denied for table {table}"

        # As service_role given the privilege a later spec might grant, and
        # as the superuser: both bypass the policy, and NOT NULL refuses.
        with store.transaction(force_rollback=True):
            # SELECT too: numbering a track reads the owner's highest id.
            store.execute(f"GRANT SELECT, INSERT ON {table} TO service_role".encode())
            store.execute("SET LOCAL ROLE service_role")
            error = refusal(store, statement, params)
            assert error.sqlstate == NOT_NULL, table
            assert error.diag.column_name == "owner_id"
        error = refusal(store, statement, params)
        assert error.sqlstate == NOT_NULL, table
        assert error.diag.column_name == "owner_id"

    assert snapshot(store) == before


def test_the_data_api_role_reaches_no_harrier_table(store: PgConnection) -> None:
    """`authenticated` is the role Supabase's Data API runs a request as. With
    an owner's valid claims it still holds no privilege on any owned table,
    so the Data API cannot read or write them past harrier.tracker."""
    owner = new_owner(store)
    job_id = fill(store, owner)
    before = snapshot(store)

    with as_role(store, "authenticated", owner):
        uid = store.execute("SELECT auth.uid()").fetchone()
        assert uid == (UUID(owner),)
        for table in OWNED_TABLES:
            values = values_for(table, job_id)
            insert_statement = (
                f"INSERT INTO {table} ({', '.join(values)}) "
                f"VALUES ({', '.join(['%s'] * len(values))})"
                if values
                else f"INSERT INTO {table} DEFAULT VALUES"
            )
            for statement, params in (
                (f"SELECT * FROM {table}", ()),
                (insert_statement, tuple(values.values())),
                (f"UPDATE {table} SET owner_id = owner_id", ()),
                (f"DELETE FROM {table}", ()),
            ):
                error = refusal(store, statement, params)
                assert error.sqlstate == INSUFFICIENT_PRIVILEGE, statement
                assert error.diag.message_primary == f"permission denied for table {table}"

    assert snapshot(store) == before


# --- keys and references ---

# (rule, the insert, the repeat for the same owner). Run as each owner with
# their claims, so owner_id is the default.
UNIQUENESS_RULES: tuple[tuple[str, str, str], ...] = (
    (
        "idx_jobs_url",
        "INSERT INTO jobs (url) VALUES ('https://example.com/jobs/1')",
        "INSERT INTO jobs (url) VALUES ('https://example.com/jobs/1')",
    ),
    (
        "idx_jobs_external_key",
        "INSERT INTO jobs (external_key) VALUES ('synthetic-key')",
        "INSERT INTO jobs (external_key) VALUES ('synthetic-key')",
    ),
    (
        "tracks_owner_id_slug_key",
        "INSERT INTO tracks (slug, kind, label) VALUES ('second', 'industry', 'Second')",
        "INSERT INTO tracks (slug, kind, label) VALUES ('second', 'industry', 'Second')",
    ),
    (
        "user_config_owner_id_kind_key",
        "INSERT INTO user_config (kind) VALUES ('watchlist')",
        "INSERT INTO user_config (kind) VALUES ('watchlist')",
    ),
    # A shared document and a track's own, each unique per owner (spec 099
    # replaced the one constraint with these two partial indexes).
    (
        "idx_profile_documents_shared",
        "INSERT INTO profile_documents (kind, name) VALUES ('truth', 'synthetic')",
        "INSERT INTO profile_documents (kind, name) VALUES ('truth', 'synthetic')",
    ),
    (
        "idx_profile_documents_owned",
        "INSERT INTO profile_documents (kind, name, track_id) VALUES ('resume_framing', 'x', 1)",
        "INSERT INTO profile_documents (kind, name, track_id) VALUES ('resume_framing', 'x', 1)",
    ),
    (
        "job_runs_pkey",
        "INSERT INTO job_runs (job, last_success_at) VALUES ('daily', '2026-10-10 00:00:00')",
        "INSERT INTO job_runs (job, last_success_at) VALUES ('daily', '2026-10-10 00:00:00')",
    ),
    (
        # The repeat changes the slug, so only the id can refuse it.
        "tracks_pkey",
        "INSERT INTO tracks (id, slug, kind, label) VALUES (7, 'seventh', 'industry', 'Seven')",
        "INSERT INTO tracks (id, slug, kind, label) VALUES (7, 'seventh-b', 'industry', 'Seven')",
    ),
)


@pytest.mark.parametrize(
    ("rule", "first", "repeat"), UNIQUENESS_RULES, ids=[rule for rule, _, _ in UNIQUENESS_RULES]
)
def test_uniqueness_is_per_owner(store: PgConnection, rule: str, first: str, repeat: str) -> None:
    a, b = new_owner(store), new_owner(store)
    with as_owner(store, a):
        store.execute(first.encode())
    # The same value for a second owner: accepted, and no hint that A has it.
    with as_owner(store, b):
        store.execute(first.encode())
    with as_owner(store, a):
        error = refusal(store, repeat)
        assert error.sqlstate == UNIQUE
        assert error.diag.constraint_name == rule


def test_references_stay_inside_one_owner(store: PgConnection) -> None:
    a, b = new_owner(store), new_owner(store)
    with as_owner(store, b):
        track = store.execute(
            "INSERT INTO tracks (slug, kind, label) VALUES ('b-only', 'industry', 'B') RETURNING id"
        ).fetchone()
        job = store.execute("INSERT INTO jobs DEFAULT VALUES RETURNING id").fetchone()
    assert track is not None and job is not None
    b_track, b_job = int(track[0]), int(job[0])
    nowhere = 1_000_000

    with as_owner(store, a):
        other = refusal(store, "INSERT INTO jobs (track_id) VALUES (%s)", (b_track,))
        missing = refusal(store, "INSERT INTO jobs (track_id) VALUES (%s)", (nowhere,))
        assert other.sqlstate == missing.sqlstate == FOREIGN_KEY
        assert other.diag.message_primary == missing.diag.message_primary

        event = (
            "INSERT INTO job_events (job_id, kind, actor, to_status) "
            "VALUES (%s, 'created', 'system', 'prospect')"
        )
        other = refusal(store, event, (b_job,))
        missing = refusal(store, event, (nowhere,))
        assert other.sqlstate == missing.sqlstate == FOREIGN_KEY
        assert other.diag.message_primary == missing.diag.message_primary


# --- track 1 ---


def test_every_owner_starts_with_track_one(store_url: str, store: PgConnection) -> None:
    import psycopg

    a, b = new_owner(store), new_owner(store)
    for owner in (a, b):
        with as_owner(store, owner):
            tracks = store.execute("SELECT id, slug, kind, label FROM tracks").fetchall()
        assert tracks == [(1, "job", "industry", "Job search")]

    with as_owner(store, a):
        row = store.execute(
            "INSERT INTO tracks (slug, kind, label) VALUES ('second', 'industry', 'Second') "
            "RETURNING id"
        ).fetchone()
    assert row == (2,)

    # Two tracks at once for one owner. The first holds its transaction open
    # until the second is seen waiting on the per-owner numbering lock. Only
    # the second's own backend counts, so a lock wait elsewhere on a shared
    # server cannot end the wait early. Were the lock missing, the second
    # would wait on the first's new key instead, never on an advisory lock,
    # and the wait below would time out.
    ids: list[int] = []
    failures: list[BaseException] = []
    second_backend: list[int] = []
    connected = threading.Event()

    def second_insert() -> None:
        try:
            with psycopg.connect(store_url, autocommit=True) as other:
                second_backend.append(other.info.backend_pid)
                connected.set()
                with as_owner(other, a):
                    found = other.execute(
                        "INSERT INTO tracks (slug, kind, label) "
                        "VALUES ('fourth', 'industry', 'Four') RETURNING id"
                    ).fetchone()
                    assert found is not None
                    ids.append(int(found[0]))
        except BaseException as error:
            failures.append(error)
        finally:
            connected.set()

    with psycopg.connect(store_url, autocommit=True) as first, as_owner(first, a):
        found = first.execute(
            "INSERT INTO tracks (slug, kind, label) VALUES ('third', 'industry', 'Three') "
            "RETURNING id"
        ).fetchone()
        assert found is not None
        ids.append(int(found[0]))
        racer = threading.Thread(target=second_insert)
        racer.start()
        assert connected.wait(timeout=30), "the second connection never opened"
        assert second_backend, failures
        deadline = time.monotonic() + 30
        while True:
            waiting = store.execute(
                "SELECT count(*) FROM pg_locks "
                "WHERE pid = %s AND locktype = 'advisory' AND NOT granted",
                (second_backend[0],),
            ).fetchone()
            if waiting == (1,):
                break
            assert time.monotonic() < deadline, "the second insert never waited on the lock"
            time.sleep(0.05)
    racer.join(timeout=30)

    assert failures == []
    assert sorted(ids) == [3, 4]


def test_an_owner_never_waits_on_another_owners_track(store_url: str, store: PgConnection) -> None:
    """Numbering a track locks on the row's owner, and the trigger runs
    before the policy's check. Were it to lock for a row naming another
    owner, A would wait on B's open insert, and so learn of it. A row for
    an owner other than the session's is left unnumbered and unlocked, and
    the policy refuses it at once (spec 105, post-merge review)."""
    import psycopg

    a, b = new_owner(store), new_owner(store)
    with psycopg.connect(store_url, autocommit=True) as other, as_owner(other, b):
        # B's transaction stays open, holding B's numbering lock.
        other.execute("INSERT INTO tracks (slug, kind, label) VALUES ('b-open', 'industry', 'B')")
        with as_owner(store, a):
            # Were A to wait, give up soon rather than hang the test.
            store.execute("SET LOCAL lock_timeout = '2s'")
            error = refusal(
                store,
                "INSERT INTO tracks (owner_id, slug, kind, label) "
                "VALUES (%s, 'as-b', 'industry', 'As B')",
                (b,),
            )
    assert error.sqlstate == INSUFFICIENT_PRIVILEGE
    assert error.diag.message_primary == (
        'new row violates row-level security policy for table "tracks"'
    )


def test_an_owner_files_a_job_with_the_defaults(store: PgConnection) -> None:
    """The default owner_id, the default track_id 1 and the composite
    reference all resolve for the tenant role, which holds no privilege on
    the identity sequence."""
    a, b = new_owner(store), new_owner(store)
    for owner in (a, b):
        with as_owner(store, owner):
            row = store.execute(
                "INSERT INTO jobs DEFAULT VALUES RETURNING owner_id, track_id"
            ).fetchone()
        assert row == (UUID(owner), 1)


def test_a_bypassrls_role_sees_every_owner(store: PgConnection) -> None:
    """Row security does not bind a BYPASSRLS role, FORCE or not. This is
    why a request must never run as one (spec 104)."""
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    fill(store, b)
    with store.transaction(force_rollback=True):
        # service_role holds no privilege here; give it SELECT for the
        # length of this transaction, as a later operator spec might.
        store.execute(f"GRANT SELECT ON {', '.join(OWNED_TABLES)} TO service_role".encode())
        store.execute("SET LOCAL ROLE service_role")
        bypass = store.execute(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        assert bypass == (True,)
        for table in OWNED_TABLES:
            assert owners_seen(store, table) == {UUID(a), UUID(b)}, table


# --- truncate ---


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("TRUNCATE job_events", "job_events is append-only"),
        ("TRUNCATE tracks CASCADE", "tracks are archived, never deleted"),
    ],
)
def test_truncate_cannot_empty_events_or_tracks(
    store: PgConnection, statement: str, message: str
) -> None:
    """The refusal triggers on these tables are row triggers, which TRUNCATE
    skips, so each also has a statement trigger. It refuses the superuser
    this test runs as, which no grant can."""
    owner = new_owner(store)
    fill(store, owner)
    before = snapshot(store)
    error = refusal(store, statement)
    assert error.sqlstate == RAISED
    assert error.diag.message_primary == message
    assert snapshot(store) == before
