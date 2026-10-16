"""The Storage bucket and its policy: migration 23 (spec 107).

Supabase's storage objects are a shim (`tests/pg_support.py`,
`STORAGE_SHIM_STATEMENTS`) on the CI Postgres service: the policy SQL is
proved here on plain Postgres, acting as `authenticated` with a user's
claims, as Storage runs a user's request (spec 107, Honest limitations,
assumption 1). Every owner is a synthetic id.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, LiteralString

import pytest
from pg_support import (
    AUTH_SHIM_STATEMENTS,
    STORAGE_SHIM_STATEMENTS,
    as_role,
    fresh_database,
    install_shim,
    new_owner,
)

from harrier import filestore
from harrier.connection import URL_VARIABLE
from harrier.pgstore import migrate_postgres, target_version
from harrier.tracker import schema
from harrier.tracker.schema import (
    MIGRATION_23_BUCKET,
    MIGRATION_23_MIME_TYPES,
    MIGRATION_23_NEEDS_STORAGE,
    MIGRATION_23_OTHER_BUCKET,
    MIGRATION_23_SIZE_LIMIT,
    STORAGE_POLICY,
    STORAGE_USER_FUNCTION,
)
from harrier_cli.main import main

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]

# The policy's expression, as Postgres 17 prints it back.
POLICY_EXPRESSION = (
    "((bucket_id = 'harrier-files'::text) AND "
    "((storage.foldername(name))[1] = (( SELECT auth.uid() AS uid))::text) AND "
    "( SELECT harrier_storage_user_active() AS harrier_storage_user_active))"
)

INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def store() -> Iterator[PgConnection]:
    """A shimmed store migrated to the target, as the migrating role."""
    import psycopg

    with fresh_database() as url:
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            yield conn


def test_the_migration_keeps_the_seams_settings() -> None:
    """Migration 23's frozen copies equal the seam's live constants."""
    assert MIGRATION_23_BUCKET == filestore.BUCKET
    assert MIGRATION_23_SIZE_LIMIT == filestore.SIZE_LIMIT
    assert sorted(MIGRATION_23_MIME_TYPES) == sorted(filestore.CONTENT_TYPES.values())


def test_the_storage_migration_needs_supabase_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the shim, `harrier store migrate` takes a fresh store to the
    target. Without Supabase's storage objects it exits 1 with the
    precheck's text, and the store stays at version 22."""
    import psycopg

    with fresh_database() as url:
        monkeypatch.setenv(URL_VARIABLE, url)
        assert main(["store", "migrate"]) == 0
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == (f"postgres 0 -> {target_version()}\n", "")

    # No storage schema at all, and one without storage.foldername().
    without_folders = tuple(
        statement for statement in STORAGE_SHIM_STATEMENTS if "foldername" not in statement
    )
    for shim in (AUTH_SHIM_STATEMENTS, (*AUTH_SHIM_STATEMENTS, *without_folders)):
        with fresh_database(shim=False) as url:
            install_shim(url, shim)
            monkeypatch.setenv(URL_VARIABLE, url)
            assert main(["store", "migrate"]) == 1
            captured = capsys.readouterr()
            assert (captured.out, captured.err) == ("", f"error: {MIGRATION_23_NEEDS_STORAGE}\n")
            with psycopg.connect(url, autocommit=True) as conn:
                version = conn.execute("SELECT max(version) FROM schema_version").fetchone()
                function = conn.execute(
                    "SELECT to_regprocedure(%s)", (f"public.{STORAGE_USER_FUNCTION}",)
                ).fetchone()
            assert version == (22,)
            assert function == (None,)
    assert MIGRATION_23_NEEDS_STORAGE == (
        "migration 23 needs Supabase's storage schema (storage.buckets, storage.objects, "
        "storage.foldername()); this store has none"
    )


def test_the_migration_creates_no_storage_object() -> None:
    """No migration statement creates a schema, table or function in
    `storage`: only the shim does, as Supabase does on a project."""
    pattern = re.compile(r"create\s+(schema|table|(or\s+replace\s+)?function)\s+storage\b", re.I)
    for version, statements in schema.POSTGRES_MIGRATIONS:
        for statement in statements:
            assert not pattern.search(statement), version
            assert "alter table storage" not in statement.lower(), version


def test_a_bucket_with_other_settings_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The migration does not change a bucket it did not create, and a
    rerun on its own bucket changes nothing."""
    import psycopg

    from harrier.connection import StoreMigrationRefused

    with fresh_database() as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO storage.buckets (id, name, public) "
                "VALUES ('harrier-files', 'harrier-files', true)"
            )
        with pytest.raises(StoreMigrationRefused) as refused:
            migrate_postgres(url)
        assert str(refused.value) == MIGRATION_23_OTHER_BUCKET
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (22,)
            assert conn.execute("SELECT public FROM storage.buckets").fetchone() == (True,)
            conn.execute(
                "UPDATE storage.buckets SET public = false, file_size_limit = %s, "
                "allowed_mime_types = %s",
                (MIGRATION_23_SIZE_LIMIT, list(reversed(MIGRATION_23_MIME_TYPES))),
            )
        assert migrate_postgres(url) == (22, target_version())


@pytest.mark.parametrize(
    "settings",
    [
        "public = false, file_size_limit = 6000000, allowed_mime_types = NULL",
        "public = false, file_size_limit = NULL",
        "public = NULL, file_size_limit = 6000000",
    ],
)
def test_a_bucket_with_a_missing_setting_is_refused(settings: LiteralString) -> None:
    """A NULL setting is another setting: a hand-made bucket with no type
    list, no size limit or no public flag is refused, not adopted
    (data-integrity review of PR #363)."""
    import psycopg

    from harrier.connection import StoreMigrationRefused

    with fresh_database() as url:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO storage.buckets (id, name, public, file_size_limit, "
                "allowed_mime_types) VALUES ('harrier-files', 'harrier-files', false, "
                "%s, %s)",
                (MIGRATION_23_SIZE_LIMIT, list(MIGRATION_23_MIME_TYPES)),
            )
            conn.execute("UPDATE storage.buckets SET " + settings)
        with pytest.raises(StoreMigrationRefused) as refused:
            migrate_postgres(url)
        assert str(refused.value) == MIGRATION_23_OTHER_BUCKET
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (22,)


def test_storage_objects_carries_only_the_harrier_policy(store: PgConnection) -> None:
    """One policy on storage.objects: `harrier_files_owner_only`,
    permissive, for every command, to `authenticated` only, with the same
    expression for reads and writes. The bucket is private, with the limit
    and the five types."""
    policies = store.execute(
        "SELECT polname, polpermissive, polcmd::text, polroles::regrole[]::text[], "
        "pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
        "FROM pg_policy WHERE polrelid = 'storage.objects'::regclass"
    ).fetchall()
    assert policies == [
        (STORAGE_POLICY, True, "*", ["authenticated"], POLICY_EXPRESSION, POLICY_EXPRESSION)
    ]
    bucket = store.execute(
        "SELECT id, name, public, file_size_limit, allowed_mime_types FROM storage.buckets"
    ).fetchall()
    assert bucket == [
        (
            "harrier-files",
            "harrier-files",
            False,
            6000000,
            [
                "application/pdf",
                "text/markdown",
                "text/html",
                "application/json",
                "application/x-ndjson",
            ],
        )
    ]


# --- acting as Storage acts for a user ---


def _object(conn: PgConnection, name: str, bucket: str = "harrier-files") -> None:
    conn.execute("INSERT INTO storage.objects (bucket_id, name) VALUES (%s, %s)", (bucket, name))


def _refused(conn: PgConnection, statement: str, params: tuple[object, ...]) -> bool:
    """Whether the statement is refused or touches no row, inside a
    savepoint, so the transaction goes on."""
    import psycopg

    try:
        with conn.transaction():
            cursor = conn.execute(statement.encode(), params)
            return cursor.rowcount == 0
    except psycopg.errors.InsufficientPrivilege:
        return True


def _visible(conn: PgConnection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM storage.objects").fetchall()}


def test_the_bucket_policy_confines_an_owner_to_their_prefix(store: PgConnection) -> None:
    """As `authenticated` with owner A's claims: insert, select, update and
    delete under A's prefix in the bucket succeed; each is refused under
    B's prefix, under no prefix, and in another bucket. `anon` is refused
    everything."""
    a, b = new_owner(store), new_owner(store)
    store.execute(
        "INSERT INTO storage.buckets (id, name, public) VALUES ('another', 'another', false)"
    )
    # Placed by the migrating role, which the policy does not bind.
    for name in (f"{a}/resumes/x.pdf", f"{b}/resumes/x.pdf", "loose.pdf"):
        _object(store, name)
    _object(store, f"{a}/resumes/x.pdf", "another")

    with as_role(store, "authenticated", a) as conn:
        assert _visible(conn) == {f"{a}/resumes/x.pdf"}
        _object(conn, f"{a}/answers/y.md")
        assert _visible(conn) == {f"{a}/resumes/x.pdf", f"{a}/answers/y.md"}
        assert not _refused(
            conn,
            "UPDATE storage.objects SET version = 'v2' WHERE name = %s",
            (f"{a}/answers/y.md",),
        )
        assert not _refused(
            conn, "DELETE FROM storage.objects WHERE name = %s", (f"{a}/answers/y.md",)
        )
        for name, bucket in (
            (f"{b}/answers/y.md", "harrier-files"),
            ("loose.md", "harrier-files"),
            (f"{a}/answers/y.md", "another"),
        ):
            assert _refused(
                conn,
                "INSERT INTO storage.objects (bucket_id, name) VALUES (%s, %s)",
                (bucket, name),
            ), (name, bucket)
        for name, bucket in (
            (f"{b}/resumes/x.pdf", "harrier-files"),
            ("loose.pdf", "harrier-files"),
            (f"{a}/resumes/x.pdf", "another"),
        ):
            for statement in (
                "UPDATE storage.objects SET version = 'v2' WHERE name = %s AND bucket_id = %s",
                "DELETE FROM storage.objects WHERE name = %s AND bucket_id = %s",
            ):
                assert _refused(conn, statement, (name, bucket)), (statement, name, bucket)
        # Moving one's own object into another prefix is a write there.
        assert _refused(
            conn,
            "UPDATE storage.objects SET name = %s WHERE name = %s",
            (f"{b}/resumes/stolen.pdf", f"{a}/resumes/x.pdf"),
        )

    with as_role(store, "anon") as conn:
        assert _visible(conn) == set()
        assert _refused(
            conn,
            "INSERT INTO storage.objects (bucket_id, name) VALUES ('harrier-files', %s)",
            (f"{a}/anon.md",),
        )
    with as_role(store, "anon", a) as conn:
        assert _visible(conn) == set()

    left = {str(row[0]) for row in store.execute("SELECT name FROM storage.objects").fetchall()}
    assert left == {f"{a}/resumes/x.pdf", f"{b}/resumes/x.pdf", "loose.pdf"}


def test_the_storage_policy_refuses_a_banned_or_deleted_user(store: PgConnection) -> None:
    """With a still valid token: a write under the owner's prefix succeeds;
    after the user is banned it is refused, and so is a read; after the ban
    lapses it succeeds again; after the user is deleted it is refused."""
    a = new_owner(store)
    _object(store, f"{a}/resumes/x.pdf")

    def write(name: str) -> bool:
        with as_role(store, "authenticated", a) as conn:
            return not _refused(
                conn,
                "INSERT INTO storage.objects (bucket_id, name) VALUES ('harrier-files', %s)",
                (name,),
            )

    def reads() -> set[str]:
        with as_role(store, "authenticated", a) as conn:
            return _visible(conn)

    assert write(f"{a}/answers/1.md")
    store.execute(
        "UPDATE auth.users SET banned_until = now() + interval '100 years' WHERE id = %s", (a,)
    )
    assert not write(f"{a}/answers/2.md")
    assert reads() == set()
    store.execute(
        "UPDATE auth.users SET banned_until = now() - interval '1 minute' WHERE id = %s", (a,)
    )
    assert write(f"{a}/answers/3.md")
    store.execute("DELETE FROM storage.objects WHERE name LIKE %s", (f"{a}/%",))
    with as_role(store, "service_role") as conn:
        conn.execute("SELECT * FROM harrier_erase_owner(%s)", (a,))
    store.execute("DELETE FROM auth.users WHERE id = %s", (a,))
    assert not write(f"{a}/answers/4.md")
    assert reads() == set()


def test_the_storage_policy_function_is_narrow(store: PgConnection) -> None:
    """The definer function returns a boolean, takes no argument, has an
    empty search_path, and is executable by `authenticated` and by no other
    grantee: not PUBLIC, `anon`, `service_role` or the tenant role."""
    row = store.execute(
        "SELECT prosecdef, prorettype::regtype::text, pronargs, proconfig, "
        "pg_get_userbyid(proowner) FROM pg_proc WHERE oid = %s::regprocedure",
        (f"public.{STORAGE_USER_FUNCTION}",),
    ).fetchone()
    assert row is not None
    definer, returns, arguments, config, owner = row
    assert (definer, returns, arguments, config) == (True, "boolean", 0, ['search_path=""'])
    grantees = {
        str(grantee)
        for (grantee,) in store.execute(
            "SELECT CASE WHEN x.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END "
            "FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x "
            "WHERE p.oid = %s::regprocedure AND x.privilege_type = 'EXECUTE'",
            (f"public.{STORAGE_USER_FUNCTION}",),
        ).fetchall()
    }
    assert grantees == {"authenticated", owner}
    for role in ("anon", "service_role", "harrier_tenant"):
        executable = store.execute(
            "SELECT has_function_privilege(%s, %s::regprocedure, 'EXECUTE')",
            (role, f"public.{STORAGE_USER_FUNCTION}"),
        ).fetchone()
        assert executable == (False,), role
    # It answers for the session's own user only.
    a = new_owner(store)
    with as_role(store, "authenticated", a) as conn:
        assert conn.execute(f"SELECT public.{STORAGE_USER_FUNCTION}").fetchone() == (True,)
    with as_role(store, "authenticated", str(uuid.uuid4())) as conn:
        assert conn.execute(f"SELECT public.{STORAGE_USER_FUNCTION}").fetchone() == (False,)
