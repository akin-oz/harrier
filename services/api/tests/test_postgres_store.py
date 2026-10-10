"""The Postgres store: choosing it, reaching it, migrating it (spec 103).

Tests that need a server take a throwaway database from `fresh_database`
(tests/pg_support.py). Without `HARRIER_TEST_POSTGRES_URL` they skip
locally and fail in CI. The URL, driver and connection-failure tests need
no server and always run.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
import uuid
from typing import Any, LiteralString

import pytest
from pg_support import fresh_database

from harrier.pgstore import (
    MIGRATION_LOCK_KEY,
    StoreConnectionError,
    StoreDriverMissing,
    StoreUrlError,
    StoreVersionError,
    migrate_postgres,
    open_postgres_store,
    postgres_connect,
    store_target,
)
from harrier.tracker.schema import (
    MIGRATIONS,
    POSTGRES_MIGRATIONS,
    SINGLE_DIALECT_MIGRATIONS,
    Dialect,
    undeclared_dialects,
)

# What a fresh store reaches, and every version it records on the way: the
# baseline (9), then each later migration (spec 105's 10 onwards).
LATEST = max(version for version, _ in POSTGRES_MIGRATIONS)
ALL_VERSIONS = [version for version, _ in POSTGRES_MIGRATIONS]
# A version no migration has yet, for the declaration tests.
NEXT = LATEST + 1

Declared = dict[int, tuple[Dialect, str]]
URL_REFUSAL = "HARRIER_DATABASE_URL must be empty or a postgresql:// URL"


def rows(url: str, query: LiteralString, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


def recorded_versions(url: str) -> list[int]:
    return [int(row[0]) for row in rows(url, "SELECT version FROM schema_version ORDER BY 1")]


def public_tables(url: str) -> list[str]:
    found = rows(
        url,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' ORDER BY 1",
    )
    return [str(row[0]) for row in found]


# --- the store URL ---


@pytest.mark.parametrize(
    "environ", [{}, {"HARRIER_DATABASE_URL": ""}, {"HARRIER_DATABASE_URL": " "}]
)
def test_no_url_chooses_the_local_sqlite_store(environ: dict[str, str]) -> None:
    target = store_target(environ)
    assert target.dialect == "sqlite"
    assert not target.is_postgres


def test_a_postgresql_url_chooses_the_postgres_store() -> None:
    url = "postgresql://harrier@db.example.test:5432/harrier"
    target = store_target({"HARRIER_DATABASE_URL": url})
    assert target.dialect == "postgres"
    assert target.url == url


@pytest.mark.parametrize(
    "url",
    ["mysql://x", "/var/lib/harrier/tracker.db", "postgres://harrier@localhost/harrier"],
)
def test_any_other_url_is_refused(url: str) -> None:
    with pytest.raises(StoreUrlError) as refused:
        store_target({"HARRIER_DATABASE_URL": url})
    assert str(refused.value) == URL_REFUSAL


# --- the driver ---


def test_a_postgres_url_without_psycopg_names_the_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A None entry makes `import psycopg` raise ImportError.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(StoreDriverMissing) as refused:
        postgres_connect("postgresql://harrier@127.0.0.1:1/harrier")
    assert "uv sync --group postgres" in str(refused.value)


# --- a connection failure ---


def test_a_connection_failure_never_prints_the_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Port 1 is closed, so the driver fails at once; no server is needed."""
    # Made per run: distinctive enough to find, and no literal in the source
    # for a secret scanner to read as a credential.
    password = f"synthetic{uuid.uuid4().hex}"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(f"postgresql://harrier:{password}@127.0.0.1:1/harrier")

    message = str(refused.value)
    assert "127.0.0.1" in message
    assert "harrier" in message
    printed = "".join(traceback.format_exception(refused.value))
    assert password not in message
    assert password not in printed
    assert password not in caplog.text


# --- dialect declarations ---


def test_every_new_migration_declares_both_dialects() -> None:
    assert undeclared_dialects(MIGRATIONS, POSTGRES_MIGRATIONS) == []

    later = f"migration {NEXT}"
    sqlite_only = [*MIGRATIONS, (NEXT, ["CREATE TABLE later (id INTEGER PRIMARY KEY)"])]
    problems = undeclared_dialects(sqlite_only, POSTGRES_MIGRATIONS)
    assert problems
    assert all(later in problem for problem in problems)

    empty_postgres = [*POSTGRES_MIGRATIONS, (NEXT, list[str]())]
    problems = undeclared_dialects(sqlite_only, empty_postgres)
    assert problems
    assert all(later in problem for problem in problems)

    postgres_only = [*POSTGRES_MIGRATIONS, (NEXT, ["CREATE TABLE later (id bigint)"])]
    problems = undeclared_dialects(MIGRATIONS, postgres_only)
    assert problems
    assert all(later in problem for problem in problems)


def test_a_hosted_only_migration_leaves_sqlite_empty_on_purpose() -> None:
    """Each case of spec 105's single-dialect table for a Postgres-only
    version after the real ones. The real declarations stay in place, so
    only the synthetic version is judged."""
    real: Declared = dict(SINGLE_DIALECT_MIGRATIONS)
    declared: Declared = {**real, NEXT: ("postgres", "a synthetic reason")}
    postgres = [*POSTGRES_MIGRATIONS, (NEXT, ["CREATE TABLE later (id bigint)"])]
    empty_sqlite = [*MIGRATIONS, (NEXT, list[str]())]
    with_sqlite = [*MIGRATIONS, (NEXT, ["CREATE TABLE later (id INTEGER PRIMARY KEY)"])]

    # Empty SQLite list, not declared: refused, as before spec 105.
    assert undeclared_dialects(empty_sqlite, postgres, single_dialect=real) == [
        f"migration {NEXT} declares no sqlite statements"
    ]
    # Empty SQLite list, declared with a reason: accepted.
    assert undeclared_dialects(empty_sqlite, postgres, single_dialect=declared) == []
    # Declared Postgres only, yet SQLite statements: refused.
    assert undeclared_dialects(with_sqlite, postgres, single_dialect=declared) == [
        f"migration {NEXT} is postgres only but declares sqlite statements"
    ]
    # Declared with an empty reason: refused.
    no_reason: Declared = {**real, NEXT: ("postgres", " ")}
    assert undeclared_dialects(empty_sqlite, postgres, single_dialect=no_reason) == [
        f"migration {NEXT} is postgres only without a reason"
    ]
    # The declared dialect itself empty: refused whatever is declared.
    empty_postgres = [*POSTGRES_MIGRATIONS, (NEXT, list[str]())]
    assert undeclared_dialects(empty_sqlite, empty_postgres, single_dialect=declared) == [
        f"migration {NEXT} declares no postgres statements"
    ]
    # Declared, but missing from the other list altogether: refused, since
    # that runner would never record the version.
    assert undeclared_dialects(MIGRATIONS, postgres, single_dialect=declared) == [
        f"migration {NEXT} declares no sqlite statements"
    ]

    # The real declaration: migration 10 is Postgres only, with a reason,
    # and the default mapping is the one that accepts it.
    dialect, reason = SINGLE_DIALECT_MIGRATIONS[10]
    assert dialect == "postgres" and reason.strip()
    assert dict(MIGRATIONS)[10] == []
    assert dict(POSTGRES_MIGRATIONS)[10]
    assert undeclared_dialects(MIGRATIONS, POSTGRES_MIGRATIONS, single_dialect={}) == [
        "migration 10 declares no sqlite statements"
    ]


def test_a_sqlite_only_migration_leaves_postgres_empty_on_purpose() -> None:
    """The same rule the other way round: a declared SQLite-only version may
    leave its Postgres list empty, and nothing else may."""
    real: Declared = dict(SINGLE_DIALECT_MIGRATIONS)
    declared: Declared = {**real, NEXT: ("sqlite", "a synthetic reason")}
    sqlite = [*MIGRATIONS, (NEXT, ["CREATE TABLE later (id INTEGER PRIMARY KEY)"])]
    empty_postgres = [*POSTGRES_MIGRATIONS, (NEXT, list[str]())]
    with_postgres = [*POSTGRES_MIGRATIONS, (NEXT, ["CREATE TABLE later (id bigint)"])]

    assert undeclared_dialects(sqlite, empty_postgres, single_dialect=real) == [
        f"migration {NEXT} declares no postgres statements"
    ]
    assert undeclared_dialects(sqlite, empty_postgres, single_dialect=declared) == []
    assert undeclared_dialects(sqlite, with_postgres, single_dialect=declared) == [
        f"migration {NEXT} is sqlite only but declares postgres statements"
    ]
    empty_sqlite = [*MIGRATIONS, (NEXT, list[str]())]
    assert undeclared_dialects(empty_sqlite, empty_postgres, single_dialect=declared) == [
        f"migration {NEXT} declares no sqlite statements"
    ]
    no_reason: Declared = {**real, NEXT: ("sqlite", "")}
    assert undeclared_dialects(sqlite, empty_postgres, single_dialect=no_reason) == [
        f"migration {NEXT} is sqlite only without a reason"
    ]


# --- migrating ---


def test_migrate_applies_the_baseline_once() -> None:
    with fresh_database() as url:
        assert migrate_postgres(url) == (0, LATEST)
        tables_after_first = public_tables(url)
        assert migrate_postgres(url) == (LATEST, LATEST)
        assert recorded_versions(url) == ALL_VERSIONS
        assert public_tables(url) == tables_after_first


def waiting_on_the_migration_lock(url: str) -> int:
    found = rows(
        url,
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())",
    )
    return int(found[0][0])


def test_two_first_migrations_apply_once_on_postgres() -> None:
    """Both runners queue on the advisory lock before either has read a
    version under it, so each has to find the other's work rather than
    redo it. Without the lock the second CREATE FUNCTION would fail."""
    import psycopg

    with fresh_database() as url:
        outcomes: list[tuple[int, int]] = []
        failures: list[BaseException] = []

        def run() -> None:
            try:
                outcomes.append(migrate_postgres(url))
            except BaseException as error:
                failures.append(error)

        holder = psycopg.connect(url)
        try:
            holder.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            runners = [threading.Thread(target=run) for _ in range(2)]
            for runner in runners:
                runner.start()
            deadline = time.monotonic() + 30
            while waiting_on_the_migration_lock(url) < 2:
                assert time.monotonic() < deadline, "the runners never queued on the lock"
                time.sleep(0.05)
            holder.commit()
        finally:
            holder.close()
        for runner in runners:
            runner.join(timeout=60)

        assert failures == []
        assert [after for _, after in outcomes] == [LATEST, LATEST]
        assert recorded_versions(url) == ALL_VERSIONS
        tables = public_tables(url)
        assert len(tables) == len(set(tables))
        assert {"jobs", "tracks", "job_events", "schema_version"} <= set(tables)


# --- opening ---


def test_opening_an_unmigrated_store_is_refused() -> None:
    with fresh_database() as url:
        with pytest.raises(StoreVersionError) as refused:
            open_postgres_store(url)
        assert str(refused.value) == (
            f"the Postgres store is at version 0; this code needs {LATEST}. "
            "Run harrier store migrate."
        )
        # Refusing did not migrate it.
        assert "schema_version" not in public_tables(url)


def test_opening_a_migrated_store_succeeds() -> None:
    with fresh_database() as url:
        migrate_postgres(url)
        conn = open_postgres_store(url)
        try:
            assert not conn.closed
        finally:
            conn.close()


def test_a_store_ahead_of_the_code_is_refused() -> None:
    with fresh_database() as url:
        migrate_postgres(url)
        ahead = LATEST + 1
        rows(url, "INSERT INTO schema_version (version) VALUES (%s) RETURNING version", (ahead,))

        with pytest.raises(StoreVersionError) as refused:
            open_postgres_store(url)
        assert str(ahead) in str(refused.value)
        assert str(LATEST) in str(refused.value)

        with pytest.raises(StoreVersionError) as refused:
            migrate_postgres(url)
        assert str(ahead) in str(refused.value)
        assert str(LATEST) in str(refused.value)
        assert recorded_versions(url) == [*ALL_VERSIONS, ahead]
