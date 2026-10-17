"""The deploy's store steps: grant a login role, check the store (spec 110),
and make the Storage bucket and policy a restored store lacks (spec 155).

`harrier store grant-tenant`, `harrier store grant-schedule-reader` and
`harrier store check-deploy` run as the migrating role, in the deploy
workflow, after `harrier store migrate` and before the API is deployed. Each
refuses in words a person can act on and prints no URL, host or password:
role names and check names only.

The login role checks are `harrier.loginrole.login_role_problems`, the
function the hosted API's start calls, so the deploy refuses what a machine
would refuse and a machine that refuses never passes its health check.
`check-deploy` writes nothing: every statement it runs is a read, in one
transaction that is rolled back.

`harrier store restore-storage` runs as the migrator on a store restored from
a backup (spec 153), which carries nothing of `storage`. It runs migration
23's own bucket and policy statements (`POSTGRES_STORAGE`), never the
migration, and makes only what is absent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any, cast

from harrier.connection import (
    Connection,
    StoreDeployRefused,
    StoreError,
    StoreIntegrityError,
)
from harrier.loginrole import (
    CANNOT_SET_READER,
    CANNOT_SET_TENANT,
    LISTS_SCHEDULES,
    can_set_role,
    login_role_problems,
)
from harrier.pgstore import check_postgres_version, postgres_connect
from harrier.tracker.schema import (
    ERASE_FUNCTION,
    MIGRATION_23_BUCKET,
    MIGRATION_23_NEEDS_STORAGE,
    MIGRATION_23_OTHER_BUCKET,
    OWNED_TABLES,
    POSTGRES_MIGRATIONS,
    POSTGRES_STORAGE,
    SCHEDULE_READER_ROLE,
    SERVICE_ROLE,
    STORAGE_USER_FUNCTION,
    TENANT_ROLE,
)

# The roles Supabase's Data API uses. Spec 105 grants them nothing in
# `public`; `check-deploy` reads that from the live catalog.
DATA_API_ROLES: tuple[str, ...] = ("anon", "authenticated", "service_role")

# The one policy harrier puts on `storage.objects` (spec 107).
STORAGE_POLICY = "harrier_files_owner_only"

# What each kind of login role may reach: exactly these, and nothing else
# (spec 110, "Amendment (2026-10-11): login roles reach no role past the
# policy"). The API's and the run's login hold the tenant role; the
# scheduler's also holds the schedule reader.
TENANT_LOGIN_ROLES: frozenset[str] = frozenset({TENANT_ROLE})
SCHEDULER_LOGIN_ROLES: frozenset[str] = frozenset({TENANT_ROLE, SCHEDULE_READER_ROLE})


@dataclass(frozen=True)
class Login:
    """A login role `check-deploy` checks, and the roles it may reach."""

    role: str
    reaches: frozenset[str] = TENANT_LOGIN_ROLES

    @property
    def lists_schedules(self) -> bool:
        """Whether this login is the scheduler's, which holds the schedule
        reader: spec 108's `login_role_problems(..., lists_schedules=True)`
        requires the right to set it and does not count the membership."""
        return SCHEDULE_READER_ROLE in self.reaches


def quote_identifier(name: str) -> str:
    """`name` as a quoted SQL identifier: every `"` doubled. A name holding
    a NUL byte cannot be one, and an empty one is not a role."""
    if not name or "\x00" in name:
        raise StoreDeployRefused("a role name is empty or holds a NUL byte")
    return '"' + name.replace('"', '""') + '"'


def _open(url: str) -> Connection:
    return Connection.postgres(cast(Any, postgres_connect(url)))


def _store_version(conn: Connection) -> int:
    exists = conn.execute("SELECT to_regclass('public.schema_version') IS NOT NULL").fetchone()
    if exists is None or not exists[0]:
        return 0
    row = conn.execute("SELECT MAX(version) FROM public.schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _role_exists(conn: Connection, role: str) -> bool:
    row = conn.execute("SELECT to_regrole(?) IS NOT NULL", (quote_identifier(role),)).fetchone()
    return row is not None and bool(row[0])


def _refusal(command: str, role: str, checks: Sequence[object]) -> StoreDeployRefused:
    return StoreDeployRefused(
        f"harrier store {command} refuses {role}: " + "; ".join(map(str, checks))
    )


def grant_tenant(url: str, role: str) -> str:
    """`harrier store grant-tenant <role>`: the line to print.

    Refuses a role that fails the login role checks, `cannot set role
    harrier_tenant` excepted, which is what the grant gives. Idempotent: a
    role that already holds `harrier_tenant` with the right to set it is
    reported, and nothing is granted again.
    """
    with closing(_open(url)) as conn:
        return _grant(conn, role, TENANT_ROLE, "grant-tenant")


def grant_schedule_reader(url: str, role: str) -> str:
    """`harrier store grant-schedule-reader <role>`: the line to print.

    Refuses when the store has no `harrier_schedule_reader`, which spec
    108's migration creates, and when `login_role_problems` returns
    anything, `cannot set role harrier_tenant` included: this runs after
    `grant-tenant` for the same role.
    """
    with closing(_open(url)) as conn:
        return _grant(conn, role, SCHEDULE_READER_ROLE, "grant-schedule-reader")


def _grant(conn: Connection, role: str, group: str, command: str) -> str:
    if group == SCHEDULE_READER_ROLE and not _role_exists(conn, group):
        raise StoreDeployRefused(f"{group} does not exist; spec 108's migration creates it")
    check_postgres_version(_store_version(conn))
    # The reader's grant is for the scheduler's login, which may hold it; the
    # tenant grant is run for every kind of login, and `check-deploy` holds
    # each kind to its own set (spec 108, spec 134).
    problems = login_role_problems(conn, role, lists_schedules=group == SCHEDULE_READER_ROLE)
    # What each grant exists to give, which the role cannot have before it.
    expected = {
        TENANT_ROLE: {CANNOT_SET_TENANT, LISTS_SCHEDULES},
        SCHEDULE_READER_ROLE: {CANNOT_SET_READER},
    }[group]
    problems = [problem for problem in problems if problem.check not in expected]
    if problems:
        raise _refusal(command, role, problems)
    if can_set_role(conn, role, group):
        return f"{group} already granted to {role}"
    with conn.transaction():
        conn.execute(f"GRANT {quote_identifier(group)} TO {quote_identifier(role)}")
    if not can_set_role(conn, role, group):
        # An existing grant made without the right to set the role stays as
        # it was: a plain GRANT does not change its options.
        raise StoreDeployRefused(
            f"{group} is granted to {role}, which still cannot set it; "
            f"revoke the grant and run harrier store {command} again"
        )
    return f"{group} granted to {role}"


def check_deploy(url: str, logins: Sequence[Login], report: Callable[[str], None]) -> None:
    """`harrier store check-deploy`: `report` is called with one line per
    check that passes, in order, as it passes.

    Raises StoreDeployRefused at the first check that fails, naming it, so
    the lines reported before it are the checks that passed. Run as the
    migrator after the grants. Reads the catalog and writes nothing.
    """
    with closing(_open(url)) as conn:
        try:
            with conn.transaction():
                # The server refuses a write, so "writes nothing" is not a
                # promise this module keeps by care alone.
                conn.execute("SET TRANSACTION READ ONLY")
                report(_check_version(conn))
                report(_check_data_api_roles(conn))
                report(_check_row_security(conn))
                for login in logins:
                    report(_check_login(conn, login))
                report(_check_storage_policies(conn))
                raise _NothingWritten
        except _NothingWritten:
            pass


class _NothingWritten(Exception):
    """Ends the check's transaction by rolling it back."""


def _check_version(conn: Connection) -> str:
    found = _store_version(conn)
    try:
        check_postgres_version(found)
    except StoreError as error:
        raise StoreDeployRefused(f"check-deploy: store version: {error}") from None
    return f"store version: postgres {found}"


_TABLE_PRIVILEGES = "SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER"
_COLUMN_PRIVILEGES = "SELECT,INSERT,UPDATE,REFERENCES"


def _check_data_api_roles(conn: Connection) -> str:
    """`anon`, `authenticated` and `service_role` hold no effective privilege
    on any table, sequence or function in `public`: a grant to them, to
    `PUBLIC`, or through a role they are members of. Supabase's event
    trigger function and extension members are not harrier's and are not
    read (spec 105, N2). The one exception is `service_role`'s `EXECUTE` on
    `harrier_erase_owner(uuid)`, the door `harrier operator erase` uses and
    nothing else (spec 111), and `authenticated`'s `EXECUTE` on
    `harrier_storage_user_active()`, which the Storage policy calls for the
    user Storage runs a request as (spec 107). Either function held by any
    other of these roles or by `PUBLIC` is still a finding."""
    placeholders = ", ".join("?" for _ in DATA_API_ROLES)
    held = conn.execute(
        "SELECT r.rolname, c.relname, 'table' FROM pg_catalog.pg_roles r "
        "CROSS JOIN pg_catalog.pg_class c "
        f"WHERE r.rolname IN ({placeholders}) AND c.relnamespace = 'public'::regnamespace "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') "
        f"AND (pg_catalog.has_table_privilege(r.oid, c.oid, '{_TABLE_PRIVILEGES}') "
        f"OR pg_catalog.has_any_column_privilege(r.oid, c.oid, '{_COLUMN_PRIVILEGES}')) "
        "UNION ALL "
        "SELECT r.rolname, c.relname, 'sequence' FROM pg_catalog.pg_roles r "
        "CROSS JOIN pg_catalog.pg_class c "
        f"WHERE r.rolname IN ({placeholders}) AND c.relnamespace = 'public'::regnamespace "
        "AND c.relkind = 'S' "
        "AND pg_catalog.has_sequence_privilege(r.oid, c.oid, 'USAGE,SELECT,UPDATE') "
        "UNION ALL "
        "SELECT r.rolname, p.proname || '()', 'function' FROM pg_catalog.pg_roles r "
        "CROSS JOIN pg_catalog.pg_proc p "
        f"WHERE r.rolname IN ({placeholders}) AND p.pronamespace = 'public'::regnamespace "
        "AND p.prokind IN ('f', 'p') AND p.prorettype <> 'event_trigger'::regtype "
        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
        "WHERE d.classid = 'pg_catalog.pg_proc'::regclass AND d.objid = p.oid "
        "AND d.deptype = 'e') "
        "AND NOT (r.rolname = ? AND p.oid = pg_catalog.to_regprocedure(?)) "
        "AND NOT (r.rolname = ? AND p.oid = pg_catalog.to_regprocedure(?)) "
        "AND pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE') "
        "ORDER BY 1, 3, 2",
        (
            *DATA_API_ROLES,
            *DATA_API_ROLES,
            *DATA_API_ROLES,
            SERVICE_ROLE,
            f"public.{ERASE_FUNCTION}",
            "authenticated",
            f"public.{STORAGE_USER_FUNCTION}",
        ),
    ).fetchall()
    if held:
        role, name, kind = (str(part) for part in held[0])
        more = f" (and {len(held) - 1} more)" if len(held) > 1 else ""
        raise StoreDeployRefused(
            f"check-deploy: data api roles: {role} holds a privilege on {kind} {name}{more}"
        )
    return (
        f"data api roles: {', '.join(DATA_API_ROLES)} hold nothing in public "
        f"but {SERVICE_ROLE}'s EXECUTE on {ERASE_FUNCTION} "
        f"and authenticated's EXECUTE on {STORAGE_USER_FUNCTION}"
    )


def _check_row_security(conn: Connection) -> str:
    placeholders = ", ".join("?" for _ in OWNED_TABLES)
    rows = conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_catalog.pg_class "
        f"WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') "
        f"AND relname IN ({placeholders})",
        OWNED_TABLES,
    ).fetchall()
    found = {str(name): (bool(secured), bool(forced)) for name, secured, forced in rows}
    for table in OWNED_TABLES:
        if table not in found:
            raise StoreDeployRefused(f"check-deploy: row security: {table} does not exist")
        secured, forced = found[table]
        if not secured:
            raise StoreDeployRefused(f"check-deploy: row security: {table} is not enabled")
        if not forced:
            raise StoreDeployRefused(f"check-deploy: row security: {table} is not forced")
    return f"row security: enabled and forced on {len(OWNED_TABLES)} tables"


def reached_roles(conn: Connection, role: str) -> set[str]:
    """Every role `role` is a member of, directly or through other roles,
    whatever the grants' `INHERIT` and `SET` options say. The one reading
    `check-deploy` and the recorded check of the deployed project share
    (`tests/test_deployed_login_roles.py`)."""
    return {
        str(name)
        for (name,) in conn.execute(
            "SELECT r.rolname FROM pg_catalog.pg_roles r WHERE r.rolname <> ? "
            "AND pg_catalog.pg_has_role(?, r.oid, 'MEMBER') ORDER BY r.rolname",
            (role, role),
        ).fetchall()
    }


def _check_login(conn: Connection, login: Login) -> str:
    role = login.role
    problems = login_role_problems(conn, role, lists_schedules=login.lists_schedules)
    if problems:
        raise StoreDeployRefused(
            f"check-deploy: login role {role}: " + "; ".join(map(str, problems))
        )
    reached = reached_roles(conn, role)
    extra = sorted(reached - login.reaches)
    if extra:
        raise StoreDeployRefused(
            f"check-deploy: login role {role} is a member of {', '.join(extra)}, "
            "which the deploy did not grant"
        )
    return f"{role}: member of {', '.join(sorted(reached))}"


def _check_storage_policies(conn: Connection) -> str:
    """No policy on `storage.objects` but harrier's own (spec 107), and both
    the bucket and that policy present (spec 155): a restored store lacks
    them until `restore-storage` runs. A project without the table has
    nothing to check; every Supabase project has it."""
    if not _table_exists(conn, "storage.objects"):
        return "storage.objects: absent, no policy to check"
    names = [
        str(name)
        for (name,) in conn.execute(
            "SELECT polname FROM pg_catalog.pg_policy "
            "WHERE polrelid = 'storage.objects'::regclass ORDER BY polname"
        ).fetchall()
    ]
    other = [name for name in names if name != STORAGE_POLICY]
    if other:
        raise StoreDeployRefused(
            f"check-deploy: storage policies: storage.objects carries {', '.join(other)}"
        )
    missing: list[str] = []
    if not _bucket_exists(conn):
        missing.append(f"bucket {MIGRATION_23_BUCKET}")
    if STORAGE_POLICY not in names:
        missing.append(f"policy {STORAGE_POLICY}")
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        raise StoreDeployRefused(
            f"check-deploy: storage: {' and '.join(missing)} {verb} missing; "
            "run harrier store restore-storage"
        )
    return f"storage.objects: no policy but {STORAGE_POLICY}"


def _table_exists(conn: Connection, name: str) -> bool:
    row = conn.execute("SELECT to_regclass(?) IS NOT NULL", (name,)).fetchone()
    return row is not None and bool(row[0])


def _bucket_exists(conn: Connection) -> bool:
    """Whether `storage.buckets` holds harrier's bucket. A store without the
    table holds no bucket."""
    if not _table_exists(conn, "storage.buckets"):
        return False
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM storage.buckets WHERE id = ?)", (MIGRATION_23_BUCKET,)
    ).fetchone()
    return row is not None and bool(row[0])


def _policy_exists(conn: Connection) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_policy "
        "WHERE polrelid = 'storage.objects'::regclass AND polname = ?)",
        (STORAGE_POLICY,),
    ).fetchone()
    return row is not None and bool(row[0])


# --- restore-storage (spec 155) ---

# The migration that makes the bucket and the policy: the one whose
# statements are POSTGRES_STORAGE.
STORAGE_MIGRATION = next(
    version for version, statements in POSTGRES_MIGRATIONS if statements is POSTGRES_STORAGE
)

# Each statement of migration 23 that restore-storage reuses, by a phrase
# only that statement holds.
_HAS_STORAGE = "to_regclass('storage.buckets') IS NULL"
_OTHER_BUCKET = "RAISE EXCEPTION 'migration 23 found bucket"
_MAKE_BUCKET = "INSERT INTO storage.buckets"
_MAKE_POLICY = f"CREATE POLICY {STORAGE_POLICY} ON storage.objects"


def migration_23_statement(marker: str) -> str:
    """The one statement of migration 23 that holds `marker`, as the
    migration runs it. Chosen by its text rather than its place, so a change
    to the list cannot hand the command another statement."""
    found = [statement for statement in POSTGRES_STORAGE if marker in statement]
    if len(found) != 1:
        raise StoreDeployRefused(
            f"restore-storage: migration {STORAGE_MIGRATION} has no single statement "
            "this command reuses; the command needs fixing"
        )
    return found[0]


def _run_check(conn: Connection, marker: str, step: str, refusal: str) -> None:
    """Run one of migration 23's checks. The text it raises is the refusal,
    as `harrier store migrate` prints it."""
    try:
        conn.execute(migration_23_statement(marker))
    except StoreIntegrityError as error:
        if error.kind == "refused" and error.message == refusal:
            raise StoreDeployRefused(f"restore-storage: {step}: {refusal}") from None
        raise


def restore_storage(url: str) -> list[str]:
    """`harrier store restore-storage`: the two lines to print.

    In one transaction, as the migrator: refuse a store below migration 23,
    one without Supabase's storage objects, one without the function the
    policy calls, and a bucket of harrier's id with other settings; then
    make the bucket and the policy, each only if absent. A refusal is a
    StoreDeployRefused naming the step, raised inside the transaction, so it
    rolls back and nothing is made.
    """
    with closing(_open(url)) as conn, conn.transaction():
        found = _store_version(conn)
        if found < STORAGE_MIGRATION:
            raise StoreDeployRefused(
                f"restore-storage: store version: the store is at version {found}, below "
                f"{STORAGE_MIGRATION}; run harrier store migrate, which makes the bucket "
                "and the policy"
            )
        _run_check(conn, _HAS_STORAGE, "storage", MIGRATION_23_NEEDS_STORAGE)
        row = conn.execute(
            "SELECT to_regprocedure(?) IS NOT NULL", (f"public.{STORAGE_USER_FUNCTION}",)
        ).fetchone()
        if row is None or not row[0]:
            raise StoreDeployRefused(
                f"restore-storage: function: public.{STORAGE_USER_FUNCTION} is missing, and "
                "the policy calls it; the restore of public is incomplete"
            )
        _run_check(conn, _OTHER_BUCKET, "bucket", MIGRATION_23_OTHER_BUCKET)
        bucket = "already there"
        if not _bucket_exists(conn):
            conn.execute(migration_23_statement(_MAKE_BUCKET))
            bucket = "made"
        policy = "already there"
        if not _policy_exists(conn):
            conn.execute(migration_23_statement(_MAKE_POLICY))
            policy = "made"
    return [f"bucket {MIGRATION_23_BUCKET}: {bucket}", f"policy {STORAGE_POLICY}: {policy}"]
