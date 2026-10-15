"""The deploy's store steps: `harrier store grant-tenant`,
`grant-schedule-reader` and `check-deploy` (spec 110).

Each runs as the migrating role on a copy of the migrated template, through
`harrier_cli.main.main` as the deploy workflow runs it, with the roles the
test makes dropped after it (`tests/test_hosted_requests.py`). Every role,
URL and password here is synthetic. A refusal prints role and check names
and never the URL, which each case asserts.
"""

# Pyright strict cannot resolve the helpers' untyped members, which is why the
# Postgres test files carry these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import closing

import pytest
from pg_support import fresh_database
from test_hosted_requests import LoginRole, Server, copied_server
from test_owner_policy import migrate_to

from harrier import storedeploy
from harrier.connection import URL_VARIABLE, Connection, StoreDatabaseError
from harrier.loginrole import (
    CANNOT_LOG_IN,
    CANNOT_SET_TENANT,
    INHERITS,
    login_role_problems,
)
from harrier.pgstore import postgres_connect, target_version
from harrier.tracker.schema import (
    OWNED_TABLES,
    POSTGRES_MIGRATIONS,
    SCHEDULE_READER_ROLE,
    TENANT_ROLE,
)
from harrier_cli.main import main

SetUp = Callable[[Server], LoginRole]


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@pytest.fixture(autouse=True)
def migrator_store(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store every command here runs on: the copy, as the migrator."""
    monkeypatch.setenv(URL_VARIABLE, server.url)


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def holds(server: Server, role: str, group: str) -> bool:
    """Whether `role` may set `group`, read as the migrator."""
    with closing(postgres_connect(server.url)) as driver:
        return storedeploy.can_set_role(Connection.postgres(driver), role, group)


def memberships(server: Server, role: str, group: str) -> int:
    """How many direct grants of `group` to `role` the catalog holds."""
    return int(
        server.one(
            "SELECT count(*) FROM pg_auth_members m "
            f"WHERE m.roleid = '{group}'::regrole AND m.member = '{role}'::regrole"
        )
    )


def ungranted(server: Server) -> LoginRole:
    return server.login_role(tenant="none")


def never_the_url(server: Server, *texts: str) -> None:
    """Nothing printed holds the store's URL, host or password."""
    printed = "".join(texts)
    assert server.url not in printed
    assert "postgresql://" not in printed
    assert "harrier_migrator" not in printed
    assert "127.0.0.1" not in printed


# --- grant-tenant: what it refuses ---


def no_such_role(server: Server) -> LoginRole:
    return LoginRole(f"harrier_t_{uuid.uuid4().hex[:12]}", "", "")


def nologin(server: Server) -> LoginRole:
    login = ungranted(server)
    server.sql(f"ALTER ROLE {login.name} NOLOGIN")
    return login


def inherits(server: Server) -> LoginRole:
    login = ungranted(server)
    server.sql(f"ALTER ROLE {login.name} INHERIT")
    return login


def member_of_service_role(server: Server) -> LoginRole:
    login = ungranted(server)
    server.sql(f"GRANT service_role TO {login.name}")
    return login


def member_of_postgres_through_a_noinherit_role(server: Server) -> LoginRole:
    login = ungranted(server)
    between = server.role("NOLOGIN NOINHERIT")
    server.sql(f"GRANT postgres TO {between}", f"GRANT {between} TO {login.name}")
    return login


def owns_a_public_table(server: Server) -> LoginRole:
    login = ungranted(server)
    table = f"public.harrier_t_{uuid.uuid4().hex[:12]}"
    server.sql(f"CREATE TABLE {table} (body text)", f"ALTER TABLE {table} OWNER TO {login.name}")
    return login


REFUSED: dict[str, tuple[SetUp, str]] = {
    "no such role": (no_such_role, "no such role"),
    "a role that cannot log in": (nologin, CANNOT_LOG_IN),
    "a role that inherits": (inherits, INHERITS),
    "a superuser": (lambda server: server.login_role("SUPERUSER", tenant="none"), "superuser"),
    "a role with bypassrls": (
        lambda server: server.login_role("BYPASSRLS", tenant="none"),
        "bypassrls",
    ),
    "a member of service_role": (
        member_of_service_role,
        "member of a role past the policy: service_role (bypassrls, named)",
    ),
    "a member of postgres through another role": (
        member_of_postgres_through_a_noinherit_role,
        "member of a role past the policy: postgres (superuser, bypassrls, named)",
    ),
    "an owner of a table in public": (owns_a_public_table, "owns a table in public"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_grant_tenant_refuses_a_role_that_can_bypass(
    case: str, server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    set_up, check = REFUSED[case]
    login = set_up(server)
    code, out, err = run(["store", "grant-tenant", login.name], capsys)
    assert (code, out) == (1, "")
    assert err == f"error: harrier store grant-tenant refuses {login.name}: {check}\n"
    never_the_url(server, out, err)
    # Nothing was granted.
    if case != "no such role":
        assert memberships(server, login.name, TENANT_ROLE) == 0


def test_grant_tenant_refuses_a_role_for_every_check_it_fails(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both `NOLOGIN` and `INHERIT` at once are both named, in the order the
    shared function reports them."""
    login = ungranted(server)
    server.sql(f"ALTER ROLE {login.name} NOLOGIN INHERIT")
    code, _, err = run(["store", "grant-tenant", login.name], capsys)
    assert code == 1
    assert err == (
        f"error: harrier store grant-tenant refuses {login.name}: {CANNOT_LOG_IN}; {INHERITS}\n"
    )


def test_grant_tenant_does_not_expect_what_it_is_about_to_give(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """`cannot set role harrier_tenant` is what the grant fixes, so a role
    with that alone is granted, not refused."""
    login = ungranted(server)
    with closing(postgres_connect(server.url)) as driver:
        before = login_role_problems(Connection.postgres(driver), login.name)
    assert [problem.check for problem in before] == [CANNOT_SET_TENANT]
    assert run(["store", "grant-tenant", login.name], capsys)[0] == 0


# --- grant-tenant: what it grants ---


def test_grant_tenant_grants_once(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    login = ungranted(server)
    assert not holds(server, login.name, TENANT_ROLE)

    assert run(["store", "grant-tenant", login.name], capsys) == (
        0,
        f"harrier_tenant granted to {login.name}\n",
        "",
    )
    assert holds(server, login.name, TENANT_ROLE)
    assert memberships(server, login.name, TENANT_ROLE) == 1
    # What the API's start would find now: nothing.
    with closing(postgres_connect(server.url)) as driver:
        assert login_role_problems(Connection.postgres(driver), login.name) == []

    # A second run reports the grant and changes nothing.
    assert run(["store", "grant-tenant", login.name], capsys) == (
        0,
        f"harrier_tenant already granted to {login.name}\n",
        "",
    )
    assert memberships(server, login.name, TENANT_ROLE) == 1


def test_grant_tenant_quotes_the_role_name_as_an_identifier(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """A role name is data: one that holds a quote, a space and a semicolon is
    granted to, as itself, and the rest of the name is not run as SQL."""
    name = f'harrier_t_{uuid.uuid4().hex[:8]} "odd"; DROP TABLE jobs; --'
    quoted = '"' + name.replace('"', '""') + '"'
    server.roles.append(quoted)
    server.sql(f"CREATE ROLE {quoted} LOGIN NOINHERIT")
    assert run(["store", "grant-tenant", name], capsys) == (
        0,
        f"harrier_tenant granted to {name}\n",
        "",
    )
    assert server.one(f"SELECT pg_has_role('{name}', 'harrier_tenant', 'SET')")
    # The table the name tried to drop is there.
    assert server.one("SELECT to_regclass('public.jobs') IS NOT NULL")


def test_grant_tenant_repairs_an_existing_grant_that_cannot_set_the_role(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """A role granted `harrier_tenant` without the right to set it is not
    "already granted": it is granted again, and then it can."""
    login = ungranted(server)
    server.sql(f"GRANT {TENANT_ROLE} TO {login.name} WITH SET FALSE")
    assert not holds(server, login.name, TENANT_ROLE)
    assert run(["store", "grant-tenant", login.name], capsys) == (
        0,
        f"harrier_tenant granted to {login.name}\n",
        "",
    )
    assert holds(server, login.name, TENANT_ROLE)


def test_a_grant_that_leaves_the_role_unable_to_set_it_is_not_reported_as_made(
    server: Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the grant runs and the role still cannot set the group, the command
    says so and exits 1 instead of printing `granted`."""
    login = ungranted(server)

    def cannot(_conn: Connection, _role: str, _group: str) -> bool:
        return False

    monkeypatch.setattr(storedeploy, "can_set_role", cannot)
    code, out, err = run(["store", "grant-tenant", login.name], capsys)
    assert (code, out) == (1, "")
    assert err == (
        f"error: harrier_tenant is granted to {login.name}, which still cannot set it; "
        "revoke the grant and run harrier store grant-tenant again\n"
    )


@pytest.mark.parametrize("command", ["grant-tenant", "grant-schedule-reader"])
def test_a_grant_command_refuses_a_store_that_is_not_at_the_target(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    previous = max(v for v, _ in POSTGRES_MIGRATIONS if v < target_version())
    with fresh_database() as url:
        migrate_to(url, previous, monkeypatch)
        monkeypatch.setenv(URL_VARIABLE, url)
        code, out, err = run(["store", command, "anyone"], capsys)
    assert (code, out) == (1, "")
    assert err == (
        f"error: the Postgres store is at version {previous}; this code needs "
        f"{target_version()}. Run harrier store migrate.\n"
    )


@pytest.mark.parametrize("command", ["grant-tenant", "grant-schedule-reader"])
def test_the_grant_commands_refuse_a_sqlite_store(
    command: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from harrier.db import default_db_path

    monkeypatch.setenv(URL_VARIABLE, "")
    code, out, err = run(["store", command, "anyone"], capsys)
    assert (code, out, err) == (1, "", f"error: harrier store {command} needs a Postgres store\n")
    # Refused before anything opened the local database.
    assert not default_db_path().exists()


def test_check_deploy_refuses_a_sqlite_store(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(URL_VARIABLE, "")
    assert run(["store", "check-deploy", "--login", "anyone"], capsys) == (
        1,
        "",
        "error: harrier store check-deploy needs a Postgres store\n",
    )


# --- grant-schedule-reader ---


def tenant_granted(server: Server) -> LoginRole:
    login = server.login_role()
    return login


def test_grant_schedule_reader_grants_once(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    login = tenant_granted(server)
    assert not holds(server, login.name, SCHEDULE_READER_ROLE)

    assert run(["store", "grant-schedule-reader", login.name], capsys) == (
        0,
        f"harrier_schedule_reader granted to {login.name}\n",
        "",
    )
    assert holds(server, login.name, SCHEDULE_READER_ROLE)
    assert memberships(server, login.name, SCHEDULE_READER_ROLE) == 1

    assert run(["store", "grant-schedule-reader", login.name], capsys) == (
        0,
        f"harrier_schedule_reader already granted to {login.name}\n",
        "",
    )
    assert memberships(server, login.name, SCHEDULE_READER_ROLE) == 1


def test_grant_schedule_reader_refuses_a_store_without_the_role(
    server: Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 108's migration makes the role, and a role belongs to the whole
    server, so a test cannot drop it without breaking every other test on
    it. The role the command asks for is a name that exists nowhere
    instead; the text names whatever the command asks for."""
    assert SCHEDULE_READER_ROLE == "harrier_schedule_reader"
    missing = f"harrier_t_missing_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(storedeploy, "SCHEDULE_READER_ROLE", missing)
    code, out, err = run(["store", "grant-schedule-reader", tenant_granted(server).name], capsys)
    assert (code, out) == (1, "")
    assert err == f"error: {missing} does not exist; spec 108's migration creates it\n"
    never_the_url(server, out, err)


def test_grant_schedule_reader_refuses_what_it_cannot_grant(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    # A login role `login_role_problems` names, each in its words and never
    # the URL.
    for make, check in (
        (nologin, CANNOT_LOG_IN),
        (inherits, INHERITS),
        (
            member_of_service_role,
            "member of a role past the policy: service_role (bypassrls, named)",
        ),
    ):
        login = make(server)
        # `grant-tenant` ran for the role first, as the deploy does.
        server.sql(f"GRANT {TENANT_ROLE} TO {login.name}")
        code, out, err = run(["store", "grant-schedule-reader", login.name], capsys)
        assert (code, out) == (1, "")
        assert err == f"error: harrier store grant-schedule-reader refuses {login.name}: {check}\n"
        never_the_url(server, out, err)
        assert memberships(server, login.name, SCHEDULE_READER_ROLE) == 0

    # A role that never got the tenant role: grant-tenant has not run.
    login = ungranted(server)
    code, _, err = run(["store", "grant-schedule-reader", login.name], capsys)
    assert code == 1
    assert err == (
        f"error: harrier store grant-schedule-reader refuses {login.name}: {CANNOT_SET_TENANT}\n"
    )

    # A role that does not exist.
    code, _, err = run(["store", "grant-schedule-reader", no_such_role(server).name], capsys)
    assert code == 1 and err.endswith(": no such role\n")


# --- check-deploy ---


def three_logins(server: Server, capsys: pytest.CaptureFixture[str]) -> tuple[str, str, str]:
    """An API, a run and a scheduler login, granted as the deploy grants
    them: `harrier_tenant` to each, the schedule reader to the scheduler."""
    api, run_login, scheduler = ungranted(server), ungranted(server), ungranted(server)
    for login in (api, run_login, scheduler):
        assert run(["store", "grant-tenant", login.name], capsys)[0] == 0
    assert run(["store", "grant-schedule-reader", scheduler.name], capsys)[0] == 0
    return api.name, run_login.name, scheduler.name


def bare(server: Server) -> list[str]:
    """`check-deploy` for one login role that passes, so the checks before and
    after the login roles are what each test reads."""
    return ["store", "check-deploy", "--login", server.login_role().name]


def check_deploy_argv(api: str, run_login: str, scheduler: str) -> list[str]:
    return [
        "store",
        "check-deploy",
        "--login",
        api,
        "--login",
        run_login,
        "--scheduler-login",
        scheduler,
    ]


def test_check_deploy_passes_on_a_migrated_granted_store(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    api, run_login, scheduler = three_logins(server, capsys)
    code, out, err = run(check_deploy_argv(api, run_login, scheduler), capsys)
    assert (code, err) == (0, "")
    assert out.splitlines() == [
        f"store version: postgres {target_version()}",
        "data api roles: anon, authenticated, service_role hold nothing in public "
        "but service_role's EXECUTE on harrier_erase_owner(uuid)",
        f"row security: enabled and forced on {len(OWNED_TABLES)} tables",
        f"{api}: member of {TENANT_ROLE}",
        f"{run_login}: member of {TENANT_ROLE}",
        f"{scheduler}: member of {SCHEDULE_READER_ROLE}, {TENANT_ROLE}",
        "storage.objects: absent, no policy to check",
    ]
    never_the_url(server, out, err)


def failed_check(
    server: Server, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[list[str], str]:
    """The lines printed before the failure, and the failure."""
    code, out, err = run(argv, capsys)
    assert code == 1, (out, err)
    assert err.startswith("error: check-deploy: ")
    assert err.count("\n") == 1
    never_the_url(server, out, err)
    return out.splitlines(), err.removeprefix("error: ").rstrip("\n")


def test_check_deploy_names_the_failed_check(
    server: Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    api, run_login, scheduler = three_logins(server, capsys)
    argv = check_deploy_argv(api, run_login, scheduler)

    # A grant to anon: the checks before it passed and are printed.
    server.migrator("GRANT SELECT ON jobs TO anon")
    printed, failure = failed_check(server, argv, capsys)
    assert failure == "check-deploy: data api roles: anon holds a privilege on table jobs"
    assert printed == [f"store version: postgres {target_version()}"]
    server.migrator("REVOKE SELECT ON jobs FROM anon")

    # A table without forced row security.
    server.migrator("ALTER TABLE contacts NO FORCE ROW LEVEL SECURITY")
    _, failure = failed_check(server, argv, capsys)
    assert failure == "check-deploy: row security: contacts is not forced"
    server.migrator("ALTER TABLE contacts FORCE ROW LEVEL SECURITY")
    server.migrator("ALTER TABLE contacts DISABLE ROW LEVEL SECURITY")
    _, failure = failed_check(server, argv, capsys)
    assert failure == "check-deploy: row security: contacts is not enabled"
    server.migrator("ALTER TABLE contacts ENABLE ROW LEVEL SECURITY")

    # A login role outside harrier_tenant.
    outside = ungranted(server)
    _, failure = failed_check(server, check_deploy_argv(outside.name, run_login, scheduler), capsys)
    assert failure == f"check-deploy: login role {outside.name}: {CANNOT_SET_TENANT}"

    # Back to a store that passes, so no step above failed for another reason.
    assert run(argv, capsys)[0] == 0

    # A store behind: the version is the first check.
    previous = max(v for v, _ in POSTGRES_MIGRATIONS if v < target_version())
    with fresh_database() as url:
        migrate_to(url, previous, monkeypatch)
        monkeypatch.setenv(URL_VARIABLE, url)
        printed, failure = failed_check(server, ["store", "check-deploy", "--login", "x"], capsys)
    assert printed == []
    assert failure == (
        f"check-deploy: store version: the Postgres store is at version {previous}; "
        f"this code needs {target_version()}. Run harrier store migrate."
    )


def public_select(server: Server) -> str:
    server.migrator("GRANT SELECT ON jobs TO PUBLIC")
    return "anon holds a privilege on table jobs (and 2 more)"


def every_table_privilege(privilege: str) -> Callable[[Server], str]:
    def grant(server: Server) -> str:
        server.migrator(f"GRANT {privilege} ON jobs TO anon")
        return "anon holds a privilege on table jobs"

    grant.__name__ = f"table_{privilege.lower()}"
    return grant


def column_update(server: Server) -> str:
    server.migrator("GRANT UPDATE (url) ON jobs TO authenticated")
    return "authenticated holds a privilege on table jobs"


def a_partitioned_table(server: Server) -> str:
    server.migrator(
        "CREATE TABLE harrier_probe_part (n integer) PARTITION BY RANGE (n)",
        "GRANT SELECT ON harrier_probe_part TO anon",
    )
    return "anon holds a privilege on table harrier_probe_part"


def sequence_privilege(privilege: str) -> Callable[[Server], str]:
    def grant(server: Server) -> str:
        server.migrator(
            "CREATE SEQUENCE harrier_probe_seq",
            f"GRANT {privilege} ON SEQUENCE harrier_probe_seq TO anon",
        )
        return "anon holds a privilege on sequence harrier_probe_seq"

    grant.__name__ = f"sequence_{privilege.lower()}"
    return grant


def column_grant(server: Server) -> str:
    server.migrator("GRANT SELECT (url) ON jobs TO authenticated")
    return "authenticated holds a privilege on table jobs"


def function_execute(server: Server) -> str:
    server.migrator("GRANT EXECUTE ON FUNCTION harrier_number_track() TO service_role")
    return "service_role holds a privilege on function harrier_number_track()"


def erase_door_to_authenticated(server: Server) -> str:
    """Only `service_role` may hold the erase door (spec 111)."""
    server.migrator("GRANT EXECUTE ON FUNCTION harrier_erase_owner(uuid) TO authenticated")
    return "authenticated holds a privilege on function harrier_erase_owner()"


def erase_door_to_public(server: Server) -> str:
    server.migrator("GRANT EXECUTE ON FUNCTION harrier_erase_owner(uuid) TO PUBLIC")
    return "anon holds a privilege on function harrier_erase_owner() (and 1 more)"


def sequence_usage(server: Server) -> str:
    server.migrator(
        "CREATE SEQUENCE harrier_probe_seq", "GRANT USAGE ON SEQUENCE harrier_probe_seq TO anon"
    )
    return "anon holds a privilege on sequence harrier_probe_seq"


def view_select(server: Server) -> str:
    server.migrator(
        "CREATE VIEW every_job AS SELECT id FROM jobs", "GRANT SELECT ON every_job TO anon"
    )
    return "anon holds a privilege on table every_job"


@pytest.mark.parametrize(
    "case",
    [
        public_select,
        column_grant,
        function_execute,
        erase_door_to_authenticated,
        erase_door_to_public,
        sequence_usage,
        view_select,
        every_table_privilege("INSERT"),
        every_table_privilege("UPDATE"),
        every_table_privilege("DELETE"),
        every_table_privilege("TRUNCATE"),
        every_table_privilege("REFERENCES"),
        every_table_privilege("TRIGGER"),
        column_update,
        a_partitioned_table,
        sequence_privilege("SELECT"),
        sequence_privilege("UPDATE"),
    ],
    ids=lambda case: case.__name__,
)
def test_check_deploy_reads_the_data_api_roles_effective_privileges(
    case: Callable[[Server], str], server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    named = case(server)
    _, failure = failed_check(server, bare(server), capsys)
    assert failure == f"check-deploy: data api roles: {named}"


def test_check_deploy_leaves_supabases_own_event_trigger_function_alone(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec 115's N2: `rls_auto_enable()` is Supabase's, executable by every
    role, and returns `event_trigger`. It is not harrier's grant."""
    server.migrator(
        "CREATE FUNCTION public.rls_auto_enable() RETURNS event_trigger LANGUAGE plpgsql "
        "AS $$ BEGIN END $$",
        "GRANT EXECUTE ON FUNCTION public.rls_auto_enable() TO anon, authenticated, service_role",
    )
    assert server.one(
        "SELECT has_function_privilege('anon', 'public.rls_auto_enable()', 'EXECUTE')"
    )
    code, out, err = run(bare(server), capsys)
    assert (code, err) == (0, "")
    assert (
        "data api roles: anon, authenticated, service_role hold nothing in public "
        "but service_role's EXECUTE on harrier_erase_owner(uuid)"
    ) in out


def test_check_deploy_names_a_login_role_that_reaches_another_role(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    api, run_login, scheduler = three_logins(server, capsys)

    def argv_for(login: str) -> list[str]:
        return check_deploy_argv(login, run_login, scheduler)

    def undo(*roles: str) -> None:
        for role in roles:
            server.sql(f"REVOKE {role} FROM {api}")

    # Of a BYPASSRLS role, through a NOINHERIT role.
    bypassing = server.role("NOLOGIN BYPASSRLS")
    between = server.role("NOLOGIN NOINHERIT")
    server.sql(f"GRANT {bypassing} TO {between}", f"GRANT {between} TO {api}")
    _, failure = failed_check(server, argv_for(api), capsys)
    assert failure == (
        f"check-deploy: login role {api}: member of a role past the policy: {bypassing} (bypassrls)"
    )
    undo(between)

    # Of a role without BYPASSRLS that owns a table in public, through one.
    owner = server.role("NOLOGIN")
    table = f"public.harrier_t_{uuid.uuid4().hex[:12]}"
    between = server.role("NOLOGIN NOINHERIT")
    server.sql(
        f"CREATE TABLE {table} (body text)",
        f"ALTER TABLE {table} OWNER TO {owner}",
        f"GRANT {owner} TO {between}",
        f"GRANT {between} TO {api}",
    )
    _, failure = failed_check(server, argv_for(api), capsys)
    assert failure == (
        f"check-deploy: login role {api}: member of a role past the policy: "
        f"{owner} (owns a table in public)"
    )
    undo(between)

    # Of a role that bypasses nothing: not past the policy, and still not
    # granted by the deploy.
    plain = server.role("NOLOGIN")
    server.sql(f"GRANT {plain} TO {api}")
    _, failure = failed_check(server, argv_for(api), capsys)
    assert failure == (
        f"check-deploy: login role {api} is a member of {plain}, which the deploy did not grant"
    )
    undo(plain)

    # The scheduler's login holds the reader; the API's must not.
    server.sql(f"GRANT {SCHEDULE_READER_ROLE} TO {api}")
    _, failure = failed_check(server, argv_for(api), capsys)
    assert failure == f"check-deploy: login role {api}: member of {SCHEDULE_READER_ROLE}"
    undo(SCHEDULE_READER_ROLE)

    # The run login is held to the same set.
    server.sql(f"GRANT {plain} TO {run_login}")
    _, failure = failed_check(server, argv_for(api), capsys)
    assert failure == (
        f"check-deploy: login role {run_login} is a member of {plain}, "
        "which the deploy did not grant"
    )
    server.sql(f"REVOKE {plain} FROM {run_login}")

    # Passing, it prints each login role's memberships.
    code, out, _ = run(argv_for(api), capsys)
    assert code == 0
    assert f"{api}: member of {TENANT_ROLE}" in out.splitlines()
    assert f"{scheduler}: member of {SCHEDULE_READER_ROLE}, {TENANT_ROLE}" in out.splitlines()


def test_check_deploy_holds_run_and_scheduler_logins_to_their_roles(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    api, run_login, scheduler = three_logins(server, capsys)

    # The run login that fails a login role check.
    server.sql(f"ALTER ROLE {run_login} INHERIT")
    _, failure = failed_check(server, check_deploy_argv(api, run_login, scheduler), capsys)
    assert failure == f"check-deploy: login role {run_login}: {INHERITS}"
    server.sql(f"ALTER ROLE {run_login} NOINHERIT")

    # A run login without harrier_tenant.
    outside = ungranted(server)
    _, failure = failed_check(server, check_deploy_argv(api, outside.name, scheduler), capsys)
    assert failure == f"check-deploy: login role {outside.name}: {CANNOT_SET_TENANT}"

    # A scheduler login without the schedule reader.
    no_reader = server.login_role()
    _, failure = failed_check(server, check_deploy_argv(api, run_login, no_reader.name), capsys)
    assert failure == (
        f"check-deploy: login role {no_reader.name}: cannot set role {SCHEDULE_READER_ROLE}"
    )

    # A scheduler login that fails a login role check.
    server.sql(f"ALTER ROLE {scheduler} NOLOGIN")
    _, failure = failed_check(server, check_deploy_argv(api, run_login, scheduler), capsys)
    assert failure == f"check-deploy: login role {scheduler}: {CANNOT_LOG_IN}"


def storage_objects(server: Server, *policies: str) -> None:
    server.sql(
        "CREATE SCHEMA storage",
        # As Supabase's `postgres` may use it.
        "GRANT USAGE ON SCHEMA storage TO harrier_migrator",
        "CREATE TABLE storage.objects (id uuid, owner_id uuid)",
        "ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY",
        *[f"CREATE POLICY {name} ON storage.objects FOR SELECT USING (true)" for name in policies],
    )


def test_check_deploy_names_a_policy_on_storage_objects_that_is_not_harriers(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    storage_objects(server, "harrier_files_owner_only")
    code, out, err = run(bare(server), capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[-1] == "storage.objects: no policy but harrier_files_owner_only"

    server.sql("CREATE POLICY anyone_reads ON storage.objects FOR SELECT USING (true)")
    server.sql("CREATE POLICY another_one ON storage.objects FOR INSERT WITH CHECK (true)")
    _, failure = failed_check(server, bare(server), capsys)
    assert failure == (
        "check-deploy: storage policies: storage.objects carries another_one, anyone_reads"
    )


def test_check_deploy_writes_nothing(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its transaction is read only, so a statement that wrote would be
    refused by the server rather than trusted away."""

    def writes(conn: Connection) -> str:
        conn.execute("CREATE TABLE public.harrier_check_deploy_wrote (n integer)")
        return "unreachable"

    monkeypatch.setattr(storedeploy, "_check_storage_policies", writes)
    with pytest.raises(StoreDatabaseError, match="read-only transaction"):
        storedeploy.check_deploy(server.url, [], lambda _line: None)
    assert not server.one("SELECT to_regclass('public.harrier_check_deploy_wrote') IS NOT NULL")


def test_check_deploy_refuses_to_run_with_no_login_role_to_check(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deploy that names no login role would pass having checked none."""
    code, out, err = run(["store", "check-deploy"], capsys)
    assert (code, out) == (1, "")
    assert (
        err == "error: harrier store check-deploy needs at least one --login or --scheduler-login\n"
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["store", "grant-tenant", "harrier_api"],
        ["store", "grant-schedule-reader", "harrier_scheduler"],
        ["store", "check-deploy", "--login", "harrier_api"],
    ],
    ids=lambda argv: argv[1],
)
def test_a_store_that_cannot_be_reached_is_reported_without_its_host(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """These run in a public log, where only the whole URL is masked: the
    refusal names no host, port, user or driver text."""
    monkeypatch.setenv(
        URL_VARIABLE, "postgresql://synthetic_user:synthetic_pw@127.0.0.1:1/synthetic_db"
    )
    code, out, err = run(argv, capsys)
    assert (code, out) == (1, "")
    assert err == (
        f"error: harrier store {argv[1]} could not connect to the store; check the migrator's URL\n"
    )
    for word in ("127.0.0.1", "synthetic", "5432", "refused"):
        assert word not in err


def test_the_scheduler_login_passes_every_deploy_it_holds_the_reader_through(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """A second deploy runs `grant-tenant` for the scheduler's login again,
    when it already holds the reader (spec 108's check refuses that for any
    other login): it is granted nothing new and still passes."""
    api, run_login, scheduler = three_logins(server, capsys)
    for _ in range(2):
        assert run(["store", "grant-tenant", scheduler], capsys) == (
            0,
            f"harrier_tenant already granted to {scheduler}\n",
            "",
        )
        assert run(["store", "grant-schedule-reader", scheduler], capsys) == (
            0,
            f"harrier_schedule_reader already granted to {scheduler}\n",
            "",
        )
        assert run(check_deploy_argv(api, run_login, scheduler), capsys)[0] == 0
