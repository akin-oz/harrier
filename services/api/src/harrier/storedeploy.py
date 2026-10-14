"""The deploy's store steps: grant a login role, check the store (spec 110).

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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from typing import Any, cast

from harrier.connection import Connection, StoreDeployRefused, StoreError
from harrier.loginrole import CANNOT_SET_TENANT, can_set_role, login_role_problems
from harrier.pgstore import check_postgres_version, postgres_connect
from harrier.tracker.schema import OWNED_TABLES, SCHEDULE_READER_ROLE, TENANT_ROLE

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
    problems = login_role_problems(conn, role)
    if group == TENANT_ROLE:
        # The check `harrier store grant-tenant` exists to satisfy.
        problems = [problem for problem in problems if problem.check != CANNOT_SET_TENANT]
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
    read (spec 105, N2)."""
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
        "AND pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE') "
        "ORDER BY 1, 3, 2",
        (*DATA_API_ROLES, *DATA_API_ROLES, *DATA_API_ROLES),
    ).fetchall()
    if held:
        role, name, kind = (str(part) for part in held[0])
        more = f" (and {len(held) - 1} more)" if len(held) > 1 else ""
        raise StoreDeployRefused(
            f"check-deploy: data api roles: {role} holds a privilege on {kind} {name}{more}"
        )
    return f"data api roles: {', '.join(DATA_API_ROLES)} hold nothing in public"


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
    problems = login_role_problems(conn, role)
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
    missing = sorted(login.reaches - reached)
    if missing:
        raise StoreDeployRefused(
            f"check-deploy: login role {role} is not a member of {', '.join(missing)}"
        )
    return f"{role}: member of {', '.join(sorted(reached))}"


def _check_storage_policies(conn: Connection) -> str:
    """No policy on `storage.objects` but harrier's own (spec 107). A project
    without the table has nothing to check; every Supabase project has it."""
    table = conn.execute("SELECT to_regclass('storage.objects') IS NOT NULL").fetchone()
    if table is None or not table[0]:
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
    return f"storage.objects: no policy but {STORAGE_POLICY}"
