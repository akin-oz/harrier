"""What the deployed project's login roles reach (spec 110, "Amendment
(2026-10-11): login roles reach no role past the policy").

A recorded check, not a gate: `harrier store check-deploy` is the gate (spec
039). It connects with the environment's migrator URL, which the operator
exports as `HARRIER_DEPLOYED_MIGRATE_URL` in their shell and never in `.env`,
and it writes nothing, so it runs against production as well as staging.
Without the variable it skips, naming it, locally and in CI.

It reads each login role's reach with the function `check-deploy` uses
(`harrier.storedeploy.reached_roles`), so the CI tests of that function
(`tests/test_store_deploy.py`) show it failing. The pull request that first
deploys an environment records this module's output for it, with role names
and no URL, host, project reference or password.
"""

# Pyright strict cannot resolve the driver's untyped members.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import os
from contextlib import closing

import pytest

from harrier.connection import Connection
from harrier.pgstore import postgres_connect
from harrier.storedeploy import SCHEDULER_LOGIN_ROLES, TENANT_LOGIN_ROLES, reached_roles
from harrier.tracker.schema import TENANT_ROLE

URL_VARIABLE = "HARRIER_DEPLOYED_MIGRATE_URL"

# The login roles the documents name, and what each reaches. A role that does
# not exist on the project is skipped: a project may not have made it yet.
EXPECTED: dict[str, frozenset[str]] = {
    "harrier_api": TENANT_LOGIN_ROLES,
    "harrier_run": TENANT_LOGIN_ROLES,
    "harrier_scheduler": SCHEDULER_LOGIN_ROLES,
}


def _past_the_policy(conn: Connection, role: str) -> list[str]:
    """Why `role` is past the policy, as words: superuser, BYPASSRLS, an
    owner of a table in `public` or of a table with row security enabled in
    any schema, or named `service_role` or `postgres`."""
    why: list[str] = []
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = ?", (role,)
    ).fetchone()
    assert row is not None, f"{role} does not exist"
    if row[0]:
        why.append("superuser")
    if row[1]:
        why.append("bypassrls")
    owns_public = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_roles r ON r.oid = c.relowner WHERE r.rolname = ? "
        "AND c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p'))",
        (role,),
    ).fetchone()
    if owns_public is not None and owns_public[0]:
        why.append("owns a table in public")
    owns_policed = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_roles r ON r.oid = c.relowner WHERE r.rolname = ? "
        "AND c.relkind IN ('r', 'p') AND c.relrowsecurity)",
        (role,),
    ).fetchone()
    if owns_policed is not None and owns_policed[0]:
        why.append("owns a table with row security")
    if role in ("service_role", "postgres"):
        why.append("named")
    return why


def test_the_deployed_login_roles_reach_only_their_grants() -> None:
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        pytest.skip(f"{URL_VARIABLE} is not set; the deployed project is not checked")
    checked: list[str] = []
    with closing(postgres_connect(url)) as driver:
        conn = Connection.postgres(driver)
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            for login, expected in EXPECTED.items():
                exists = conn.execute("SELECT to_regrole(?) IS NOT NULL", (login,)).fetchone()
                if exists is None or not exists[0]:
                    continue
                reached = reached_roles(conn, login)
                assert reached == expected, (
                    f"{login} reaches {sorted(reached)}; the deploy grants {sorted(expected)}"
                )
                for role in (login, *sorted(reached)):
                    assert _past_the_policy(conn, role) == [], (
                        f"{role} is past the policy: {_past_the_policy(conn, role)}"
                    )
                # The tenant role is what a request becomes.
                assert TENANT_ROLE in reached
                checked.append(f"{login}: member of {', '.join(sorted(reached))}")
    # Role names only: the pull request records these lines.
    print("\n".join(checked))
