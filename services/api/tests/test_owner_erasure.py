"""One owner's erasure, in the database (spec 111, "Deletion" and "How
`harrier_refuse()` stands aside").

The erasure migration (22) lets `harrier_refuse()` stand aside for the
delete of one owner's rows, adds `harrier_erase_owner(uuid)` for
`service_role`, and drops, disables and recreates no trigger. These tests
prove each on plain Postgres with spec 105's shim (`tests/pg_support.py`).

Owners are random UUIDs and rows are synthetic (`tests/test_owner_policy.py`'s
`fill`). A statement that must be refused runs in a savepoint, so the
transaction around it goes on.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from pg_support import (
    MIGRATOR_ROLE,
    admin_url,
    as_owner,
    fresh_database,
    new_owner,
    superuser_url,
)
from test_owner_policy import (
    FOREIGN_KEY,
    INSUFFICIENT_PRIVILEGE,
    RAISED,
    fill,
    migrate_to,
    migration,
    refusal,
    snapshot,
)

from harrier.connection import URL_VARIABLE, StoreMigrationRefused
from harrier.db import connect
from harrier.pgstore import migrate_postgres, postgres_version, target_version
from harrier.tracker import schema
from harrier.tracker.schema import (
    ERASE_FUNCTION,
    ERASE_NEEDS_ROW_SECURITY_BYPASS,
    ERASE_SETTING,
    ERASURE_MIXED_OWNERS,
    MIGRATION_22_OWNED_TABLES,
    MIGRATIONS,
    OWNED_TABLES,
    POSTGRES_MIGRATIONS,
    SERVICE_ROLE,
    SINGLE_DIALECT_MIGRATIONS,
    TENANT_ROLE,
)
from harrier_cli.main import main

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

ERASURE_VERSION = 22

# The refusals the store already gave, by table: what a delete says.
APPEND_ONLY = {
    "job_events": "job_events is append-only",
    "job_eligibility": "job_eligibility is append-only",
    "job_verdicts": "job_verdicts is append-only",
    "tracks": "tracks are archived, never deleted",
}


@pytest.fixture
def store_url() -> Iterator[str]:
    """A fresh shimmed database at the latest version."""
    with fresh_database() as url:
        migrate_postgres(url)
        yield url


@pytest.fixture
def store(store_url: str) -> Iterator[PgConnection]:
    """A connection as the role that migrated the store, in autocommit: the
    table owner, which bypasses row security as Supabase's postgres does."""
    import psycopg

    with psycopg.connect(store_url, autocommit=True) as conn:
        yield conn


@pytest.fixture
def superuser(store_url: str) -> Iterator[PgConnection]:
    import psycopg

    with psycopg.connect(superuser_url(store_url), autocommit=True) as conn:
        yield conn


def erase(conn: PgConnection, owner: str) -> list[tuple[str, int]]:
    """`SELECT * FROM harrier_erase_owner(owner)` as `service_role`, in the
    transaction the caller opened."""
    conn.execute("SET LOCAL ROLE service_role")
    return [
        (str(name), int(count))
        for name, count in conn.execute(
            "SELECT table_name, erased FROM harrier_erase_owner(%s)", (owner,)
        ).fetchall()
    ]


def rows_of(conn: PgConnection, owner: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of `owner` in every owned table, as the table owner sees them."""
    return {
        table: sorted(
            conn.execute(
                f"SELECT * FROM {table} WHERE owner_id = %s".encode(), (owner,)
            ).fetchall(),
            key=repr,
        )
        for table in OWNED_TABLES
    }


def set_erase_owner(conn: PgConnection, owner: str) -> None:
    conn.execute("SELECT set_config(%s, %s, true)", (ERASE_SETTING, owner))


def trigger_state(conn: PgConnection) -> list[tuple[Any, ...]]:
    """Every user trigger: its oid (a recreated trigger has a new one), name,
    table, enabled state and function."""
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT t.oid::int, t.tgname, t.tgrelid::regclass::text, t.tgenabled::text, "
            "t.tgfoid::regproc::text FROM pg_trigger t WHERE NOT t.tgisinternal "
            "ORDER BY t.tgname, t.tgrelid::regclass::text"
        ).fetchall()
    ]


# --- the migration ---


def test_the_erasure_migration_is_postgres_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """On Postgres, `harrier store migrate` takes a store from the version
    before the erasure migration to it and prints both; a fresh store
    reaches the target. On SQLite the version is recorded and nothing else
    changes."""
    real = list(MIGRATIONS)
    # Later migrations follow it (spec 107's is 23).
    assert target_version() >= ERASURE_VERSION
    assert SINGLE_DIALECT_MIGRATIONS[ERASURE_VERSION][0] == "postgres"
    assert SINGLE_DIALECT_MIGRATIONS[ERASURE_VERSION][1].strip()
    assert dict(MIGRATIONS)[ERASURE_VERSION] == []
    assert dict(POSTGRES_MIGRATIONS)[ERASURE_VERSION]

    with fresh_database() as url:
        monkeypatch.setenv(URL_VARIABLE, url)
        migrate_to(url, ERASURE_VERSION - 1, monkeypatch)
        assert main(["store", "migrate"]) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == (
            f"postgres {ERASURE_VERSION - 1} -> {target_version()}\n",
            "",
        )
    with fresh_database() as url:
        assert migrate_postgres(url) == (0, target_version())

    # SQLite: the version is recorded, the schema is the one before.
    monkeypatch.setattr(schema, "MIGRATIONS", [m for m in real if m[0] < ERASURE_VERSION])
    before = tmp_path / "before.db"
    with closing(connect(before)) as conn:
        assert postgres_free_version(conn) == ERASURE_VERSION - 1
        shape = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    monkeypatch.setattr(schema, "MIGRATIONS", [m for m in real if m[0] <= ERASURE_VERSION])
    with closing(connect(before)) as conn:
        assert postgres_free_version(conn) == ERASURE_VERSION
        after = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in shape]


def postgres_free_version(conn: Any) -> int:
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    assert row is not None
    return int(row[0])


def test_the_erasure_migration_leaves_every_trigger_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No statement of the migration disables, drops or alters a trigger or
    a table, and every trigger present before it is present after it, enabled
    and not recreated (a recreated trigger has a new oid)."""
    import psycopg

    for statement in migration(ERASURE_VERSION):
        text = " ".join(statement.upper().split())
        for forbidden in ("DISABLE TRIGGER", "DROP TRIGGER", "ALTER TABLE", "CREATE TRIGGER"):
            assert forbidden not in text, forbidden
    with fresh_database() as url:
        migrate_to(url, ERASURE_VERSION - 1, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            before = trigger_state(conn)
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            after = trigger_state(conn)
    assert before
    assert after == before
    assert {row[3] for row in after} == {"O"}


def test_the_erase_function_holds_only_the_grant_the_migration_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store whose migrating role grants `EXECUTE` on every new function to
    PUBLIC and the Data API's roles, as Supabase's defaults do, still ends
    with `service_role` as the only role that may execute the erase: the
    migration revokes in its own transaction."""
    import psycopg

    roles = ("PUBLIC", "anon", "authenticated", TENANT_ROLE)
    with fresh_database() as url:
        migrate_to(url, ERASURE_VERSION - 1, monkeypatch)
        with psycopg.connect(url, autocommit=True) as conn:
            for scope in ("IN SCHEMA public", ""):
                conn.execute(
                    f"ALTER DEFAULT PRIVILEGES {scope} GRANT EXECUTE ON FUNCTIONS "
                    f"TO {', '.join(roles)}".encode()
                )
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            executes = {
                role: conn.execute(
                    "SELECT has_function_privilege(%s, to_regprocedure(%s), 'EXECUTE')",
                    (role, f"public.{ERASE_FUNCTION}"),
                ).fetchone()
                for role in ("anon", "authenticated", TENANT_ROLE, "service_role")
            }
            by_public = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x "
                "WHERE p.oid = to_regprocedure(%s) AND x.grantee = 0)",
                (f"public.{ERASE_FUNCTION}",),
            ).fetchone()
    assert executes == {
        "anon": (False,),
        "authenticated": (False,),
        TENANT_ROLE: (False,),
        "service_role": (True,),
    }
    assert by_public == (False,)


def test_the_erasure_migration_refuses_mixed_table_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store whose `job_eligibility` has another owner refuses the
    migration before creating the function, naming the table and not the
    role, and stays at the version before."""
    import psycopg

    other = f"harrier_test_other_{uuid.uuid4().hex[:12]}"
    try:
        with fresh_database() as url:
            migrate_to(url, ERASURE_VERSION - 1, monkeypatch)
            with psycopg.connect(superuser_url(url), autocommit=True) as admin:
                admin.execute(f"CREATE ROLE {other} NOLOGIN".encode())
                admin.execute(f"GRANT CREATE ON SCHEMA public TO {other}".encode())
                admin.execute(f"ALTER TABLE job_eligibility OWNER TO {other}".encode())
            with pytest.raises(StoreMigrationRefused) as refused:
                migrate_postgres(url)
            message = str(refused.value)
            assert message.startswith(ERASURE_MIXED_OWNERS)
            assert "job_eligibility" in message
            assert other not in message and MIGRATOR_ROLE not in message
            with psycopg.connect(url, autocommit=True) as conn:
                assert postgres_version(conn) == ERASURE_VERSION - 1
                assert conn.execute(
                    "SELECT to_regprocedure('public.harrier_erase_owner(uuid)')"
                ).fetchone() == (None,)
    finally:
        # After the database is dropped: a role that owns an object cannot be.
        with psycopg.connect(admin_url(), autocommit=True) as admin:
            admin.execute(f"DROP ROLE IF EXISTS {other}".encode())


# --- the refusals still hold ---


@pytest.mark.parametrize("table", sorted(APPEND_ONLY))
def test_refusals_hold_without_the_erase_setting(store: PgConnection, table: str) -> None:
    """The table's owner, with no setting, still cannot delete a row of an
    append-only table or a track, and cannot empty one."""
    owner = new_owner(store)
    fill(store, owner)
    before = snapshot(store)
    error = refusal(store, f"DELETE FROM {table}")
    assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY[table])
    # A setting that is present but empty, as a finished erase leaves it, is
    # no setting either.
    with store.transaction():
        set_erase_owner(store, "")
        error = refusal(store, f"DELETE FROM {table}")
        assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY[table])
    truncate = "TRUNCATE tracks CASCADE" if table == "tracks" else f"TRUNCATE {table}"
    error = refusal(store, truncate)
    assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY[table])
    assert snapshot(store) == before


def test_the_erase_setting_lets_through_only_its_owners_deletes(store: PgConnection) -> None:
    """With the setting naming owner A, the table owner's delete of owner B's
    row is refused on each guarded table, an update of A's event or a track
    id is refused, and TRUNCATE is refused. A's own deletes pass."""
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    fill(store, b)
    b_before = rows_of(store, b)
    with store.transaction(force_rollback=True):
        set_erase_owner(store, a)
        for table in sorted(APPEND_ONLY):
            error = refusal(store, f"DELETE FROM {table} WHERE owner_id = %s", (b,))
            assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY[table])
        error = refusal(store, "UPDATE job_events SET actor = 'changed' WHERE owner_id = %s", (a,))
        assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY["job_events"])
        error = refusal(store, "UPDATE tracks SET id = id + 1000 WHERE owner_id = %s", (a,))
        assert (error.sqlstate, error.diag.message_primary) == (RAISED, "a track id never changes")
        for statement, table in (
            ("TRUNCATE job_events", "job_events"),
            ("TRUNCATE job_eligibility", "job_eligibility"),
            ("TRUNCATE job_verdicts", "job_verdicts"),
            ("TRUNCATE tracks CASCADE", "tracks"),
        ):
            error = refusal(store, statement)
            assert (error.sqlstate, error.diag.message_primary) == (RAISED, APPEND_ONLY[table])
        assert rows_of(store, b) == b_before

        # A's own rows go: the events and assessments of the job, then the
        # second track (no job is on it).
        for table in ("job_eligibility", "job_verdicts", "job_events"):
            deleted = store.execute(
                f"DELETE FROM {table} WHERE owner_id = %s".encode(), (a,)
            ).rowcount
            assert deleted >= 1, table
        store.execute("DELETE FROM tracks WHERE owner_id = %s AND slug = 'synthetic'", (a,))
        assert rows_of(store, b) == b_before


def test_the_erase_setting_alone_lets_no_tenant_delete(store: PgConnection) -> None:
    """As the tenant with the setting naming itself, a delete on each guarded
    table is refused by the missing privilege. With DELETE granted to the
    tenant inside the test, the trigger still refuses: the role is not the
    table's owner."""
    a = new_owner(store)
    fill(store, a)
    before = snapshot(store)
    tables = sorted(APPEND_ONLY)
    with store.transaction(force_rollback=True):
        with as_owner(store, a):
            set_erase_owner(store, a)
            for table in tables:
                error = refusal(store, f"DELETE FROM {table}")
                assert error.sqlstate == INSUFFICIENT_PRIVILEGE, table
        # The grant is made by the table's owner, then the tenant is again.
        store.execute("RESET ROLE")
        store.execute(f"GRANT DELETE ON {', '.join(tables)} TO {TENANT_ROLE}".encode())
        with as_owner(store, a):
            set_erase_owner(store, a)
            for table in tables:
                error = refusal(store, f"DELETE FROM {table}")
                assert (error.sqlstate, error.diag.message_primary) == (
                    RAISED,
                    APPEND_ONLY[table],
                ), table
    assert snapshot(store) == before


# --- harrier_erase_owner ---


def test_erase_removes_one_owner_from_every_owned_table(store: PgConnection) -> None:
    """With two owners whose rows fill every table in `OWNED_TABLES`, the
    erase of A as `service_role` leaves no row of A in any of them, leaves
    every row of B as it was, returns one count per table in that order, and
    leaves the setting empty."""
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    fill(store, b)
    a_before, b_before = rows_of(store, a), rows_of(store, b)
    assert all(a_before[table] for table in OWNED_TABLES)

    with store.transaction():
        counts = erase(store, a)
        # Inside the caller's transaction a later statement is refused again.
        store.execute("RESET ROLE")
        setting = store.execute("SELECT current_setting(%s, true)", (ERASE_SETTING,)).fetchone()
        assert setting in (("",), (None,))
        error = refusal(store, "DELETE FROM job_events WHERE owner_id = %s", (b,))
        assert error.diag.message_primary == APPEND_ONLY["job_events"]

    assert [name for name, _ in counts] == list(OWNED_TABLES)
    assert counts == [(table, len(a_before[table])) for table in OWNED_TABLES]
    assert dict(counts)["job_eligibility"] >= 1
    assert all(rows == [] for rows in rows_of(store, a).values())
    assert rows_of(store, b) == b_before
    outside = store.execute("SELECT current_setting(%s, true)", (ERASE_SETTING,)).fetchone()
    assert outside in (("",), (None,))


def test_the_erase_lists_every_owned_table_in_order() -> None:
    """The delete list in the function's text is `OWNED_TABLES`, in its
    order. A table a later migration owns fails this until a migration
    replaces the function with it."""
    holders = [
        statement
        for version, statements in POSTGRES_MIGRATIONS
        for statement in statements
        if "CREATE FUNCTION public.harrier_erase_owner" in statement and version >= ERASURE_VERSION
    ]
    assert holders
    listed = re.findall(r"DELETE FROM public\.(\w+) WHERE owner_id = target", holders[-1])
    assert tuple(listed) == OWNED_TABLES
    assert MIGRATION_22_OWNED_TABLES == OWNED_TABLES
    assert listed[0] == "job_eligibility"


def test_an_erased_owner_can_be_deleted(store: PgConnection) -> None:
    """Before the erase, deleting the owner from `auth.users` is refused by
    a foreign key. After it, the delete succeeds, and the other owner's
    user is untouched."""
    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    fill(store, b)
    error = refusal(store, "DELETE FROM auth.users WHERE id = %s", (a,))
    assert error.sqlstate == FOREIGN_KEY
    with store.transaction():
        erase(store, a)
    assert store.execute("DELETE FROM auth.users WHERE id = %s", (a,)).rowcount == 1
    assert store.execute("SELECT count(*) FROM auth.users WHERE id = %s", (b,)).fetchone() == (1,)


def test_erasing_one_owner_never_waits_for_another(store_url: str, store: PgConnection) -> None:
    """While one transaction holds an uncommitted erase of A, owner B inserts
    a job, an event and a track with a lock timeout of 100 ms, and each
    succeeds. The erase takes row locks on A's rows and no table lock."""
    import psycopg

    a, b = new_owner(store), new_owner(store)
    fill(store, a)
    b_job = fill(store, b)
    with (
        psycopg.connect(store_url, autocommit=True) as erasing,
        psycopg.connect(store_url, autocommit=True) as other,
        erasing.transaction(force_rollback=True),
    ):
        assert [name for name, _ in erase(erasing, a)] == list(OWNED_TABLES)
        with as_owner(other, b):
            other.execute("SET LOCAL lock_timeout = '100ms'")
            other.execute("INSERT INTO jobs DEFAULT VALUES")
            other.execute(
                "INSERT INTO job_events (job_id, kind, actor, to_status) "
                "VALUES (%s, 'created', 'system', 'prospect')",
                (b_job,),
            )
            other.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES ('another', 'industry', 'x')"
            )


def test_only_service_role_can_erase(store: PgConnection) -> None:
    """The tenant role, `authenticated` and `anon` are each refused with
    `permission denied`, and nothing is deleted. `service_role` holds the
    one grant."""
    a = new_owner(store)
    fill(store, a)
    before = snapshot(store)
    for role in (TENANT_ROLE, "authenticated", "anon"):
        with store.transaction(force_rollback=True):
            store.execute(f"SET LOCAL ROLE {role}".encode())
            error = refusal(store, "SELECT * FROM harrier_erase_owner(%s)", (a,))
            assert error.sqlstate == INSUFFICIENT_PRIVILEGE, role
            assert "permission denied" in str(error), role
    assert snapshot(store) == before
    holders = store.execute(
        "SELECT x.grantee::regrole::text FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x "
        "WHERE p.oid = to_regprocedure(%s) AND x.privilege_type = 'EXECUTE' "
        "AND x.grantee <> p.proowner",
        (f"public.{ERASE_FUNCTION}",),
    ).fetchall()
    assert holders == [(SERVICE_ROLE,)]


def test_erase_needs_an_owner(store: PgConnection) -> None:
    error = refusal(store, "SELECT * FROM harrier_erase_owner(NULL)")
    assert (error.sqlstate, error.diag.message_primary) == (
        RAISED,
        "harrier_erase_owner needs an owner",
    )


def test_erase_refuses_a_definer_bound_by_row_security(
    store: PgConnection, superuser: PgConnection
) -> None:
    """A definer that is neither a superuser nor BYPASSRLS would see no row
    under the forced policy, delete none and report success. The function
    raises instead, and nothing is deleted. The owner change lives in a
    transaction that is rolled back."""
    a = new_owner(store)
    fill(store, a)
    before = snapshot(store)
    bound = f"harrier_test_bound_{uuid.uuid4().hex[:12]}"
    with superuser.transaction(force_rollback=True):
        superuser.execute(f"CREATE ROLE {bound} NOLOGIN NOBYPASSRLS".encode())
        superuser.execute(f"GRANT CREATE ON SCHEMA public TO {bound}".encode())
        superuser.execute(f"ALTER FUNCTION public.{ERASE_FUNCTION} OWNER TO {bound}".encode())
        superuser.execute("SET LOCAL ROLE service_role")
        error = refusal(superuser, "SELECT * FROM harrier_erase_owner(%s)", (a,))
        assert (error.sqlstate, error.diag.message_primary) == (
            RAISED,
            ERASE_NEEDS_ROW_SECURITY_BYPASS,
        )
    assert snapshot(store) == before


@pytest.mark.parametrize(
    ("table", "values"),
    [
        (
            "job_events",
            "(owner_id, job_id, kind, actor, to_status) "
            "SELECT target, job, 'created', 'system', 'prospect'",
        ),
        (
            "job_eligibility",
            "(owner_id, job_id, verdict, checked_at, origin) "
            "SELECT target, job, 'unknown', '2026-10-11T00:00:00Z', 'rule'",
        ),
    ],
)
def test_a_failed_erase_deletes_nothing(store: PgConnection, table: str, values: str) -> None:
    """A row inserted for one of A's jobs between the erase's delete of that
    table and its delete of `jobs`, simulated with a trigger, makes the job's
    delete fail its foreign key. The transaction rolls back whole: every one
    of A's rows is still there, and, with the trigger gone, a re-run erases
    them."""
    a = new_owner(store)
    job = fill(store, a)
    before = rows_of(store, a)
    store.execute(
        f"""
        CREATE FUNCTION write_late() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE target uuid := '{a}'; job bigint := {job};
        BEGIN
            INSERT INTO public.{table} {values};
            RETURN NULL;
        END
        $$
        """.encode()
    )
    store.execute(
        f"CREATE TRIGGER write_late AFTER DELETE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION write_late()".encode()
    )
    with pytest.raises(Exception) as failed, store.transaction():
        erase(store, a)
    assert getattr(failed.value, "sqlstate", None) == FOREIGN_KEY
    assert rows_of(store, a) == before

    store.execute(f"DROP TRIGGER write_late ON {table}".encode())
    with store.transaction():
        erase(store, a)
    assert all(rows == [] for rows in rows_of(store, a).values())


def test_a_rollback_undoes_the_erase_and_its_setting(store: PgConnection) -> None:
    """A rollback anywhere in the caller's transaction undoes the deletes and
    the setting together."""
    a = new_owner(store)
    fill(store, a)
    before = rows_of(store, a)
    with store.transaction(force_rollback=True):
        erase(store, a)
    assert rows_of(store, a) == before
    setting = store.execute("SELECT current_setting(%s, true)", (ERASE_SETTING,)).fetchone()
    assert setting in (("",), (None,))
