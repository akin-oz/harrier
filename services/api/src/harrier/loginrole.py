"""The login role checks: can this role step past the row policy? (spec 134)

The hosted API connects as a dedicated login role, `harrier_api` in the
documents, that is a member of `harrier_tenant` and of nothing else, and
sets `harrier_tenant` for each request. A statement inside a request can
`SET ROLE` to any role the login role is a member of, directly or through
other roles, whatever `NOINHERIT` says, so a login role that can reach a
role past the policy hands that role to SQL injected into a request (spec
104, "Amendment (2026-10-11): migration 16's follow-ups", Why).

`login_role_problems` reads the catalog and returns every check a login
role fails. The API's start calls it and refuses to serve when it returns
anything; spec 110's `harrier store grant-tenant` and `harrier store
check-deploy` call it too, so the deploy refuses the same roles. It sits in
`harrier`, not `harrier_api`, because the CLI calls it.

It returns check and role names, never a URL or a password.
"""

from __future__ import annotations

from dataclasses import dataclass

from harrier.connection import Connection
from harrier.tracker.schema import SCHEDULE_READER_ROLE, TENANT_ROLE

# The checks, by the names a refusal prints.
NO_SUCH_ROLE = "no such role"
SUPERUSER = "superuser"
BYPASSRLS = "bypassrls"
OWNS_A_TABLE_IN_PUBLIC = "owns a table in public"
CANNOT_SET_TENANT = f"cannot set role {TENANT_ROLE}"
# A login role that can list every owner's schedule (spec 108): refused for
# the API and for runs, required of the scheduler.
LISTS_SCHEDULES = f"member of {SCHEDULE_READER_ROLE}"
CANNOT_SET_READER = f"cannot set role {SCHEDULE_READER_ROLE}"
MEMBER_PAST_THE_POLICY = "member of a role past the policy"

# Why a role the login role reaches is past the policy.
REACHED_SUPERUSER = "superuser"
REACHED_BYPASSRLS = "bypassrls"
REACHED_OWNS_PUBLIC = "owns a table in public"
REACHED_OWNS_POLICED = "owns a table with row security"
REACHED_BY_NAME = "named"

# Supabase's role that bypasses row security, and its owner of everything.
# Refused by name whatever their attributes say on a given server.
NAMED_ROLES: tuple[str, ...] = ("service_role", "postgres")

# Ordinary and partitioned tables: the relations row security applies to.
_TABLE_KINDS = "('r', 'p')"


@dataclass(frozen=True)
class LoginRoleProblem:
    """One check a login role fails. `reached` is the role a membership
    reaches, and `why` what makes that role past the policy."""

    check: str
    reached: str = ""
    why: tuple[str, ...] = ()

    def __str__(self) -> str:
        if not self.reached:
            return self.check
        return f"{self.check}: {self.reached} ({', '.join(self.why)})"


def login_role_problems(
    conn: Connection, role: str, *, lists_schedules: bool = False
) -> list[LoginRoleProblem]:
    """Every check `role` fails on the store `conn` reaches, in a fixed order.

    - `superuser`, `bypassrls`: the role's own attributes.
    - `member of a role past the policy`, once per role reached: membership,
      directly or through other roles, of a role that is a superuser, has
      `BYPASSRLS`, owns a table in `public`, owns a table with row security
      enabled in any schema, or is `service_role` or `postgres`. Read with
      `pg_has_role(..., 'MEMBER')`, which counts a grant whatever its
      `INHERIT` and `SET` options.
    - `owns a table in public`: an owner bypasses row security unless it is
      forced; this does not rely on spec 105 forcing it.
    - `cannot set role harrier_tenant`: no `SET` on the tenant role, or no
      tenant role. `grant-tenant`, which runs before the grant, expects this
      one.
    - `member of harrier_schedule_reader`: the role could list every
      owner's schedule through `harrier_schedule_inputs()` (spec 108, Scope
      item 9). The API's login and the run login are refused it. The
      hosted scheduler's login passes `lists_schedules=True`: it must be
      able to set the reader, and the membership is not a problem.

    A superuser is reported as that alone: `pg_has_role` answers true for
    every role when asked about a superuser, so its memberships say nothing
    more. A role that does not exist is `no such role` alone.

    Runs as the caller; reads only the catalog, so the login role itself
    may call it.
    """
    found = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = ?", (role,)
    ).fetchone()
    if found is None:
        return [LoginRoleProblem(NO_SUCH_ROLE)]
    if found[0]:
        return [LoginRoleProblem(SUPERUSER)]
    problems: list[LoginRoleProblem] = []
    if found[1]:
        problems.append(LoginRoleProblem(BYPASSRLS))
    problems += _memberships_past_the_policy(conn, role)
    owns = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_roles r ON r.oid = c.relowner "
        "WHERE r.rolname = ? AND c.relnamespace = 'public'::regnamespace "
        f"AND c.relkind IN {_TABLE_KINDS})",
        (role,),
    ).fetchone()
    if owns is not None and owns[0]:
        problems.append(LoginRoleProblem(OWNS_A_TABLE_IN_PUBLIC))
    if not _can_set(conn, role, TENANT_ROLE):
        problems.append(LoginRoleProblem(CANNOT_SET_TENANT))
    if lists_schedules:
        if not _can_set(conn, role, SCHEDULE_READER_ROLE):
            problems.append(LoginRoleProblem(CANNOT_SET_READER))
    elif _is_member(conn, role, SCHEDULE_READER_ROLE):
        problems.append(LoginRoleProblem(LISTS_SCHEDULES))
    return problems


def _memberships_past_the_policy(conn: Connection, role: str) -> list[LoginRoleProblem]:
    rows = conn.execute(
        "SELECT r.rolname, r.rolsuper, r.rolbypassrls, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner = r.oid "
        f"AND c.relkind IN {_TABLE_KINDS} AND c.relnamespace = 'public'::regnamespace), "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner = r.oid "
        f"AND c.relkind IN {_TABLE_KINDS} AND c.relrowsecurity) "
        "FROM pg_catalog.pg_roles r "
        "WHERE r.rolname <> ? AND pg_catalog.pg_has_role(?, r.oid, 'MEMBER') "
        "ORDER BY r.rolname",
        (role, role),
    ).fetchall()
    problems: list[LoginRoleProblem] = []
    for name, superuser, bypassrls, owns_public, owns_policed in rows:
        why = [
            reason
            for reason, holds in (
                (REACHED_SUPERUSER, superuser),
                (REACHED_BYPASSRLS, bypassrls),
                (REACHED_OWNS_PUBLIC, owns_public),
                (REACHED_OWNS_POLICED, owns_policed),
                (REACHED_BY_NAME, name in NAMED_ROLES),
            )
            if holds
        ]
        if why:
            problems.append(LoginRoleProblem(MEMBER_PAST_THE_POLICY, str(name), tuple(why)))
    return problems


def _is_member(conn: Connection, role: str, target: str) -> bool:
    """Whether `role` is a member of `target`, directly or through other
    roles, whatever the grant's `INHERIT` and `SET` options say. False when
    `target` does not exist."""
    row = conn.execute(
        "SELECT CASE WHEN to_regrole(?) IS NULL THEN false "
        "ELSE pg_catalog.pg_has_role(?, ?, 'MEMBER') END",
        (target, role, target),
    ).fetchone()
    return row is not None and bool(row[0])


def _can_set(conn: Connection, role: str, target: str) -> bool:
    """Whether `role` may `SET ROLE target`. Postgres 16 split the right to
    set a role from membership; before it, membership was the right."""
    row = conn.execute(
        "SELECT CASE WHEN to_regrole(?) IS NULL THEN false "
        "WHEN current_setting('server_version_num')::int >= 160000 "
        "THEN pg_catalog.pg_has_role(?, ?, 'SET') "
        "ELSE pg_catalog.pg_has_role(?, ?, 'MEMBER') END",
        (target, role, target, role, target),
    ).fetchone()
    return row is not None and bool(row[0])
