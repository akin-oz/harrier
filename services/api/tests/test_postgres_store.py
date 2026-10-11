"""The Postgres store: choosing it, reaching it, migrating it (spec 103).

Tests that need a server take a throwaway database from `fresh_database`
(tests/pg_support.py). Without `HARRIER_TEST_POSTGRES_URL` they skip
locally and fail in CI, and two tests here hold that rule. The URL,
driver and connection-failure tests need no server and always run.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import traceback
import unicodedata
import uuid
from typing import Any, LiteralString
from urllib.parse import quote, unquote, unquote_plus, urlsplit, urlunsplit

import pytest
from pg_support import TEST_URL_VARIABLE, admin_url, fresh_database

from harrier.connection import (
    StoreConnectionError,
    StoreDriverMissing,
    StoreUrlError,
    StoreVersionError,
    store_target,
)
from harrier.pgstore import (
    MIGRATION_LOCK_KEY,
    migrate_postgres,
    open_postgres_store,
    postgres_connect,
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
DELIMITER_REFUSAL = (
    f"{URL_REFUSAL}; percent-encode any / ? # @ or control character in its user, password, "
    "database name and query values"
)


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


# --- the test server ---


def test_without_a_test_server_the_postgres_tests_fail_in_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gate that skips in CI has not run (spec 039). Both outcomes are
    caught and the kind asserted: a skip let through would turn this test
    into a skip as well, and it would pass for the wrong reason."""
    monkeypatch.delenv(TEST_URL_VARIABLE, raising=False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        admin_url()
    assert outcome.type is pytest.fail.Exception


def test_without_a_test_server_the_postgres_tests_skip_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TEST_URL_VARIABLE, raising=False)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as outcome:
        admin_url()
    assert outcome.type is pytest.skip.Exception


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
    # Without the declaration, every Postgres-only version is reported.
    assert undeclared_dialects(MIGRATIONS, POSTGRES_MIGRATIONS, single_dialect={}) == [
        f"migration {version} declares no sqlite statements"
        for version, (only, _) in sorted(SINGLE_DIALECT_MIGRATIONS.items())
        if only == "postgres"
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


def _end_the_session(conn: Any) -> None:
    """Have the server end a connection's session while the client is idle,
    and wait until it has gone. psycopg learns of it only on its next
    statement."""
    import psycopg

    pid = conn.info.backend_pid
    with psycopg.connect(admin_url(), autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        deadline = time.monotonic() + 30
        while admin.execute("SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone():
            assert time.monotonic() < deadline, "the session never ended"
            time.sleep(0.05)


def test_a_session_lost_after_the_last_commit_still_reports_the_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every migration committed, then the server ended the session before
    the unlock. The unlock raised, and `migrate_postgres` reported a
    driver error for a store it had fully migrated (privacy review of PR
    #281, case F). The lock went with the session, so nothing is held."""
    import harrier.pgstore as pgstore

    real = pgstore._apply_under_the_session_lock  # pyright: ignore[reportPrivateUsage]
    ended: list[bool] = []

    def then_lost(conn: Any) -> tuple[int, int]:
        found = real(conn)
        _end_the_session(conn)
        # The client has not seen the end yet, so a guard on `closed` would
        # not have skipped the unlock: the case is live.
        ended.append(conn.closed)
        return found

    monkeypatch.setattr(pgstore, "_apply_under_the_session_lock", then_lost)
    with fresh_database() as url:
        assert migrate_postgres(url) == (0, LATEST)
        assert ended == [False]
        assert recorded_versions(url) == ALL_VERSIONS
        assert waiting_on_the_migration_lock(url) == 0


def test_a_refusal_is_reported_when_the_session_is_lost_before_the_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration's precheck refused the store, then the server ended the
    session before the unlock. The unlock's error replaced the refusal,
    which `harrier store` then printed as a driver error (privacy review of
    PR #281, case G). The refusal is what the caller sees."""
    import harrier.pgstore as pgstore
    from harrier.connection import StoreMigrationRefused
    from harrier.tracker import schema

    real = pgstore._apply_under_the_session_lock  # pyright: ignore[reportPrivateUsage]

    def refused_then_lost(conn: Any) -> tuple[int, int]:
        try:
            return real(conn)
        finally:
            _end_the_session(conn)

    refusal = "DO $$ BEGIN RAISE EXCEPTION 'synthetic refusal'; END $$"
    monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [*POSTGRES_MIGRATIONS, (NEXT, [refusal])])
    monkeypatch.setattr(pgstore, "_apply_under_the_session_lock", refused_then_lost)
    with fresh_database() as url:
        with pytest.raises(StoreMigrationRefused) as refused:
            migrate_postgres(url)
        assert str(refused.value) == "synthetic refusal"
        assert recorded_versions(url) == ALL_VERSIONS


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


@pytest.mark.parametrize(
    "escapes",
    ["%zz", "%40at%zz"],
    ids=["an-invalid-escape", "a-valid-escape-then-an-invalid-one"],
)
def test_a_driver_error_that_quotes_the_password_never_carries_it(
    escapes: str, caplog: pytest.LogCaptureFixture
) -> None:
    """libpq quotes the URL's text as written, still percent-encoded, when it
    refuses it. Scrubbing only the decoded password let the encoded one
    through, and raising inside the except block kept the driver's error as
    `__context__`, where a debugger or error reporter still finds it
    (post-merge privacy review of PR #207). No server is needed: the driver
    refuses the URL before it connects."""
    import psycopg

    secret = f"synthetic{uuid.uuid4().hex}"
    written = f"{secret}{escapes}"
    url = f"postgresql://harrier:{written}@127.0.0.1:1/harrier"
    # The driver's own error quotes the password, so the scrub has work to do.
    with pytest.raises(psycopg.Error) as raw:
        psycopg.connect(url, connect_timeout=2)
    assert written in str(raw.value)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    error = refused.value
    assert error.__cause__ is None
    assert error.__context__ is None
    places = {
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
        "context": repr(error.__context__),
        "cause": repr(error.__cause__),
        "log": caplog.text,
    }
    for form in (written, unquote(written), secret):
        for place, text in places.items():
            assert form not in text, f"the password reached the {place}"


def test_a_store_target_never_shows_its_password() -> None:
    secret = f"synthetic{uuid.uuid4().hex}"
    target = store_target(
        {"HARRIER_DATABASE_URL": f"postgresql://harrier:{secret}@db.example.test/h"}
    )
    assert target.is_postgres
    assert secret not in repr(target)


# Malformed without a raw delimiter, so refused with the plain text. Every
# other template below holds a delimiter the two parsers can read two ways,
# and its refusal names the fix (post-merge review of PR #243).
PLAIN_MALFORMED = (
    "postgresql://harrier@127.0.0.1:notaport/harrier",
    "postgresql://harrier@[::1/harrier",
    "PostgreSQL://harrier@127.0.0.1/harrier",
)


@pytest.mark.parametrize(
    "template",
    [
        *PLAIN_MALFORMED,
        # A raw `/` in the password ends the authority there, so the rest of
        # the password is read as the port, and the parse error quotes it.
        # The host's own `@` is then in the path.
        "postgresql://harrier:{secret}/x@127.0.0.1/harrier",
        # A raw `@` in the password: libpq splits at the first `@`, so the
        # rest of the password becomes part of the host it fails to resolve
        # and quotes (review of PR #225).
        "postgresql://harrier:x@{secret}@127.0.0.1:1/harrier",
        # The same split from an `@` in the query when no path comes first.
        "postgresql://127.0.0.1:1?password=x@{secret}",
        # urlsplit reads a raw `#` as the start of a fragment and drops a raw
        # tab, CR or LF. libpq does neither, so the scrub missed what libpq
        # quotes (post-merge review of PR #231).
        "postgresql://harrier@127.0.0.1:1/harrier?password=x#{secret}%zz",
        "postgresql://harrier:x\t{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier:x\r{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier:x\n{secret}%zz@127.0.0.1:1/harrier",
    ],
    ids=[
        "a-bad-port",
        "a-bad-host",
        "a-scheme-in-another-case",
        "a-slash-in-the-password",
        "an-at-sign-in-the-password",
        "an-at-sign-in-a-query-without-a-path",
        "a-hash-in-the-query-password",
        "a-tab-in-the-password",
        "a-carriage-return-in-the-password",
        "a-line-feed-in-the-password",
    ],
)
def test_a_malformed_url_is_refused_like_any_other(template: str) -> None:
    """A bad port or host raised a bare ValueError traceback, and a scheme in
    another case passed here and failed in libpq with an unrelated message
    (post-merge data integrity review of PR #207). The refusal was raised
    inside the `except ValueError` block, so that error stayed attached as
    `__context__`, quoting the password (review of PR #218). A raw
    delimiter's refusal names the fix (post-merge review of PR #243)."""
    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)
    with pytest.raises(StoreUrlError) as refused:
        store_target({"HARRIER_DATABASE_URL": url})

    error = refused.value
    assert str(error) == (URL_REFUSAL if template in PLAIN_MALFORMED else DELIMITER_REFUSAL)
    assert error.__context__ is None
    assert error.__cause__ is None
    for text in (str(error), "".join(traceback.format_exception(error))):
        assert secret not in text


@pytest.mark.parametrize(
    "template",
    [
        "postgresql://harrier:x@{secret}@127.0.0.1:1/harrier",
        "postgresql://127.0.0.1:1?password=x@{secret}",
    ],
    ids=["an-at-sign-in-the-password", "an-at-sign-in-a-query-without-a-path"],
)
def test_an_at_sign_libpq_reads_as_the_host_never_reaches_the_cli(
    template: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The driver quotes the host it cannot resolve, and with a second `@` in
    the authority that host holds the tail of the password, which the scrub
    never knew as a password (review of PR #225). The CLI now refuses the
    URL before it connects. No server is needed: the host never resolves."""
    import psycopg

    from harrier_cli.main import main

    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)
    with pytest.raises(psycopg.Error) as raw:
        psycopg.connect(url, connect_timeout=2)
    assert secret in str(raw.value)

    monkeypatch.setenv("HARRIER_DATABASE_URL", url)
    for command in ("status", "migrate"):
        assert main(["store", command]) == 1
        captured = capsys.readouterr()
        assert captured.err == f"error: {DELIMITER_REFUSAL}\n", command
        assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "template",
    [
        "postgresql://harrier@127.0.0.1:1/harrier?sslmode=disable&password=x#{secret}%zz",
        "postgresql://harrier:x#{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier:x\t{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier:x\r{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier:x\n{secret}%zz@127.0.0.1:1/harrier",
        "postgresql://harrier@127.0.0.1:1/harrier?password=x\t{secret}%zz",
    ],
    ids=[
        "a-hash-in-the-query-password",
        "a-hash-in-the-password",
        "a-tab-in-the-password",
        "a-carriage-return-in-the-password",
        "a-line-feed-in-the-password",
        "a-tab-in-the-query-password",
    ],
)
def test_a_url_urlsplit_reads_differently_from_libpq_never_reaches_the_cli(
    template: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The scrub learns the password from urlsplit, which reads a raw `#` as
    the start of a fragment and drops a raw tab, CR or LF. libpq does
    neither, takes the whole text as the password, and quotes it when an
    escape in it is invalid. The scrub then missed it, or matched only a
    part (post-merge review of PR #231). The CLI now refuses the URL before
    it connects. No server is needed: libpq refuses the escape first."""
    import psycopg

    from harrier_cli.main import main

    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)
    # The driver's own error quotes the secret, so each case is live.
    with pytest.raises(psycopg.Error) as raw:
        psycopg.connect(url, connect_timeout=2)
    assert secret in str(raw.value)

    monkeypatch.setenv("HARRIER_DATABASE_URL", url)
    for command in ("status", "migrate"):
        assert main(["store", command]) == 1
        captured = capsys.readouterr()
        assert captured.err == f"error: {DELIMITER_REFUSAL}\n", command
        assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://harrier:{secret}@db.example.test?application_name=a@b",
        "postgresql://harrier@db.example.test?application_name=a@b",
        "postgresql://harrier:{secret}@db.example.test:5432?application_name=a@b&sslmode=disable",
    ],
    ids=["with-a-password", "without-a-password", "with-a-port-and-two-parameters"],
)
def test_an_at_sign_in_the_query_after_the_host_is_accepted(url: str) -> None:
    """libpq looks for the user's `@` up to the first `/`, so with no path it
    reads into the query. When the authority already holds its one `@`,
    libpq stops there, and it and urlsplit read the same user, password and
    host. Counting every `@` before the first `/` refused this valid URL
    (post-merge review of PR #231). The two-`@` refusals still hold
    (test_a_malformed_url_is_refused_like_any_other)."""
    from psycopg.conninfo import conninfo_to_dict

    secret = f"synthetic{uuid.uuid4().hex}"
    written = url.format(secret=secret)
    read = conninfo_to_dict(written)
    assert (read["user"], read["host"], read["application_name"]) == (
        "harrier",
        "db.example.test",
        "a@b",
    )

    target = store_target({"HARRIER_DATABASE_URL": written})

    assert target.is_postgres
    assert target.url == written


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("password", "{secret}+tail%zz"),
        ("pass%77ord", "{secret}%zz"),
        ("pass%77ord", "{secret}+tail%zz"),
    ],
    ids=["a-plain-name", "an-encoded-name", "an-encoded-name-and-a-plus"],
)
def test_a_password_in_the_query_string_never_reaches_the_error(
    name: str, value: str, caplog: pytest.LogCaptureFixture
) -> None:
    """libpq takes a password from `?password=` as well as from the authority,
    and quotes the parameter as written when it refuses it. Only the
    authority's password was scrubbed (review of PR #218). The `+` makes the
    written, the percent-decoded and the form-decoded spellings differ.
    libpq decodes the parameter's name too, so `pass%77ord` is the password
    (review of PR #225)."""
    import psycopg

    secret = f"synthetic{uuid.uuid4().hex}"
    written = value.format(secret=secret)
    url = f"postgresql://harrier@127.0.0.1:1/harrier?sslmode=disable&{name}={written}"
    # The driver's own error quotes the password, so the scrub has work to do.
    with pytest.raises(psycopg.Error) as raw:
        psycopg.connect(url, connect_timeout=2)
    assert written in str(raw.value)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    error = refused.value
    assert error.__context__ is None
    places = {
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
        "log": caplog.text,
    }
    for form in (written, unquote(written), unquote_plus(written), secret):
        for place, text in places.items():
            assert form not in text, f"the password reached the {place}"


# --- the privacy review of PR #243 ---


def pieces(secret: str, width: int = 8) -> set[str]:
    """The secret and every run of `width` characters in it: a scrub that
    leaves part of a secret behind has leaked that part."""
    return {secret} | {secret[i : i + width] for i in range(len(secret) - width + 1)}


def leaked(secret: str, text: str) -> list[str]:
    return sorted(piece for piece in pieces(secret) if piece in text)


@pytest.mark.parametrize(
    "template",
    [
        # libpq quotes a query parameter without its `=` whole.
        "postgresql://harrier@127.0.0.1:1/harrier?password:{secret}",
        "postgresql://harrier@127.0.0.1:1/harrier?password%3D{secret}",
        "postgresql://harrier@127.0.0.1:1/harrier?sslmode=disable&password {secret}",
        # Secret parameters other than password, with an invalid escape.
        "postgresql://harrier@127.0.0.1:1/harrier?sslpassword={secret}%zz",
        "postgresql://harrier@127.0.0.1:1/harrier?oauth_client_secret={secret}%zz",
        # urlsplit accepts these; libpq quotes the whole URL.
        "postgresql://harrier:{secret}]@[v1.x/harrier",
        "postgresql://harrier]@[v1.x/harrier?sslpassword={secret}",
        # Bytes that are not UTF-8 once decoded, and one raw in the value.
        "postgresql://harrier:{secret}%FF@127.0.0.1:1/harrier",
        "postgresql://harrier@127.0.0.1:1/harrier?password={secret}%C3",
        "postgresql://harrier:{secret}\udcff@127.0.0.1:1/harrier",
    ],
    ids=[
        "a-colon-for-the-equals-sign",
        "an-encoded-equals-sign",
        "a-space-for-the-equals-sign",
        "sslpassword-with-an-invalid-escape",
        "oauth-client-secret-with-an-invalid-escape",
        "a-bracket-in-the-password",
        "a-bracket-in-the-user-and-sslpassword",
        "an-encoded-byte-that-is-not-utf-8",
        "an-encoded-truncated-utf-8-sequence",
        "a-raw-byte-that-is-not-utf-8",
    ],
)
def test_a_url_libpq_cannot_parse_never_reaches_the_cli(
    template: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """libpq quotes the text of a URL it cannot parse, sometimes the whole
    URL, and other secret parameters were never scrubbed. A value that is
    not UTF-8 crashed with a traceback giving the password's length
    (privacy review of PR #243). `store_target` now parses the URL with
    libpq's own parser and refuses it, with harrier's message alone, when
    that parse fails in any way."""
    from harrier_cli.main import main

    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)

    # A raw space or a raw surrogate is refused before libpq reads the URL,
    # with the text that names the fix (privacy review of PR #281).
    raw_space_or_control = any(
        character.isspace() or unicodedata.category(character).startswith("C") for character in url
    )
    expected = DELIMITER_REFUSAL if raw_space_or_control else URL_REFUSAL

    monkeypatch.setenv("HARRIER_DATABASE_URL", url)
    for command in ("status", "migrate"):
        assert main(["store", command]) == 1
        captured = capsys.readouterr()
        assert captured.err == f"error: {expected}\n", command
        assert leaked(secret, captured.out + captured.err) == [], command


@pytest.mark.parametrize("name", ["sslpassword", "oauth_client_secret", "ssl%70assword"])
def test_every_secret_parameter_is_scrubbed(name: str, caplog: pytest.LogCaptureFixture) -> None:
    """Only `password` was scrubbed; libpq takes other secrets from the
    query and quotes them as written when it refuses them (privacy review
    of PR #243). Called directly, past `store_target`, so the scrub is what
    is tested."""
    secret = f"synthetic{uuid.uuid4().hex}"
    url = f"postgresql://harrier@127.0.0.1:1/harrier?sslmode=disable&{name}={secret}%zz"
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    error = refused.value
    assert error.__context__ is None
    for text in (str(error), "".join(traceback.format_exception(error)), caplog.text):
        assert leaked(secret, text) == []


def test_two_overlapping_secrets_are_both_scrubbed() -> None:
    """The scrub replaced the longest form first. When a shorter form
    overlapped it, the replacement broke the shorter one's text and left
    its other part behind: here the first ten characters of the password
    (privacy review of PR #243). Every occurrence of every form is now
    found in the original text, and the union of them is replaced."""
    secret = f"synthetic{uuid.uuid4().hex}"
    tail = f"{secret[10:]}]@[v1.x/harrier?"
    url = f"postgresql://harrier:{secret}]@[v1.x/harrier?password={quote(tail, safe='')}"

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    assert leaked(secret, str(refused.value)) == []


@pytest.mark.parametrize(
    "template",
    [
        "postgresql://harrier:{secret}%FF@127.0.0.1:1/harrier",
        "postgresql://harrier@127.0.0.1:1/harrier?password={secret}%C3",
        "postgresql://harrier:{secret}\udcff@127.0.0.1:1/harrier",
        # urlsplit reads the password before a raw `/` as the port.
        "postgresql://harrier:{secret}/x@127.0.0.1:1/harrier",
    ],
    ids=[
        "an-encoded-byte-that-is-not-utf-8",
        "an-encoded-truncated-utf-8-sequence",
        "a-raw-byte-that-is-not-utf-8",
        "a-port-urlsplit-cannot-read",
    ],
)
def test_any_failure_to_connect_is_a_store_error(template: str) -> None:
    """A decode or encode error, or urlsplit's error on the port, escaped
    `postgres_connect` as a traceback, which the CLI does not catch. The
    traceback gave the password's length, or quoted it (privacy review of
    PR #243). Now every failure is a StoreConnectionError naming only the
    error's class, with nothing chained."""
    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    error = refused.value
    assert error.__context__ is None
    assert error.__cause__ is None
    printed = "".join(traceback.format_exception(error))
    assert leaked(secret, printed) == []
    assert "position" not in printed


# --- the post-merge review of PR #243 ---


@pytest.mark.parametrize(
    "template",
    [
        "postgresql://harrier:/{secret}@127.0.0.1:1/harrier",
        "postgresql://harrier:4821/{secret}@127.0.0.1:1/harrier",
        "postgresql://:/{secret}@127.0.0.1/harrier",
        "postgresql://harrier:/{secret}?x@127.0.0.1:1/harrier",
        "postgresql://harrier:a/{secret}@127.0.0.1:1/harrier",
        "postgresql://harrier:x@q/{secret}@127.0.0.1:1/harrier",
        "postgresql://harrier:/{secret}?application_name=x@127.0.0.1:1/harrier",
    ],
    ids=[
        "a-slash-first-in-the-password",
        "digits-then-a-slash-in-the-password",
        "no-user-and-a-slash-first-in-the-password",
        "a-slash-and-a-question-mark-in-the-password",
        "a-letter-then-a-slash-in-the-password",
        "an-at-sign-then-a-slash-in-the-password",
        "a-slash-then-a-parameter-in-the-password",
    ],
)
def test_a_raw_delimiter_after_the_authority_never_reaches_the_cli(
    template: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw `/` or `?` in the password ends the authority early, so the
    host's own `@` lands in the path or the query. urlsplit and libpq both
    read the start of the URL as the host and the rest of the password as
    the database name, the scrub learns no password, and `harrier store`
    printed the database name (post-merge review of PR #243). The CLI now
    refuses the URL, with fixed text that names the fix, before it
    connects. No server is needed."""
    from harrier_cli.main import main

    secret = f"synthetic{uuid.uuid4().hex}"
    url = template.format(secret=secret)
    # The secret is read outside the authority, where the scrub never looks
    # for a password, so each case is live.
    parts = urlsplit(url)
    assert secret in parts.path + parts.query
    assert secret not in parts.netloc

    monkeypatch.setenv("HARRIER_DATABASE_URL", url)
    for command in ("status", "migrate"):
        assert main(["store", command]) == 1
        captured = capsys.readouterr()
        assert captured.err == f"error: {DELIMITER_REFUSAL}\n", command
        assert leaked(secret, captured.out + captured.err) == [], command


# Every character a password could hold raw that the check below has to
# read: ASCII, Latin, the C1 controls, the general punctuation block and
# U+3000, then every whitespace character anywhere, and a sample of the rest
# of category C and of letters outside the basic plane.
_WHITESPACE = [code for code in range(0x110000) if chr(code).isspace()]
_SAMPLED = [
    *(0x200B, 0x2060, 0xFEFF, 0xD800, 0xDFFF, 0xE000, 0xF8FF, 0xFFFE, 0x0378),
    *(0xF0000, 0x10FFFF, 0x1F600, 0x20000),
]
_CANDIDATES = sorted({*range(0x3100), *_WHITESPACE, *_SAMPLED})


def test_every_url_store_target_accepts_has_its_password_scrubbed() -> None:
    """`scrub_secrets` ends a URL's password at whitespace, and store_target
    refused only a raw tab, CR and LF of it. A password holding a raw
    no-break space, vertical tab, form feed, U+001C, U+0085, U+2028 or
    U+3000 was accepted, and every text the scrub guards carried it whole
    (privacy review of PR #281). Each character is put raw inside a
    password between two generated halves: store_target refuses it with
    the text that names the fix, or the scrub masks both halves."""
    from harrier.sources import scrub_secrets

    first, second = f"synthetic{uuid.uuid4().hex}", f"synthetic{uuid.uuid4().hex}"
    refused: set[int] = set()
    leaks: list[str] = []
    for code in _CANDIDATES:
        url = f"postgresql://harrier:{first}{chr(code)}{second}@db.example.test:5432/tracker"
        try:
            store_target({"HARRIER_DATABASE_URL": url})
        except StoreUrlError as error:
            refused.add(code)
            assert str(error) in (URL_REFUSAL, DELIMITER_REFUSAL), hex(code)
            continue
        scrubbed = scrub_secrets(f'connection to "{url}" failed')
        if first in scrubbed or second in scrubbed:
            leaks.append(f"U+{code:04X}")

    assert leaks == []
    named = [0x20, 0x0B, 0x0C, 0x1C, 0x85, 0xA0, 0x2028, 0x3000, 0x200B, 0xFEFF]
    assert sorted(code for code in named if code not in refused) == []
    # Letters stay accepted, raw: a password may hold any of them.
    assert {ord("é"), ord("€"), ord("ü"), 0x1F600, 0x20000}.isdisjoint(refused)


def test_a_database_name_that_is_not_printable_is_withheld(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The connection error names the host, port and database, and the
    database name was printed whatever it held. With a raw `/` in the
    password it held the password's tail (post-merge review of PR #243).
    `store_target` refuses that URL; called directly, past it, the error
    still prints no database name outside the printable shape."""
    secret = f"synthetic{uuid.uuid4().hex}"
    url = f"postgresql://harrier@127.0.0.1:1/{secret}@x"
    caplog.set_level(logging.DEBUG)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    message = str(refused.value)
    assert message.startswith(
        "cannot connect to the Postgres store at 127.0.0.1:1/(database name withheld): "
    )
    for text in (message, caplog.text):
        assert leaked(secret, text) == []


def test_a_printable_database_name_is_still_printed() -> None:
    """Withholding stops at the printable shape: an ordinary name is named."""
    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect("postgresql://harrier@127.0.0.1:1/harrier_db-1.x")
    assert "127.0.0.1:1/harrier_db-1.x: " in str(refused.value)


def _on_test_server(netloc: str, path: str, query: str = "") -> str:
    """A URL on the test server's host and port; `{host}` in `netloc`
    becomes them."""
    parts = urlsplit(admin_url())
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(("postgresql", netloc.format(host=f"{host}{port}"), path, query, ""))


@pytest.mark.parametrize("place", ["database", "user", "user-in-the-query"])
def test_the_servers_text_never_quotes_a_withheld_value(place: str) -> None:
    """The server quotes a database or role it does not know. Either can
    hold a password's tail when a raw delimiter moved it out of the
    authority, so a user, database name or query value outside the
    printable shape is masked in the driver's text as a password is
    (post-merge review of PR #243)."""
    import psycopg

    admin = urlsplit(admin_url())
    credentials = f"{admin.username}:{admin.password}"
    secret = f"synthetic{uuid.uuid4().hex}"
    url = {
        "database": _on_test_server(f"{credentials}@{{host}}", f"/{secret}%21"),
        "user": _on_test_server(f"{secret}%21:wrong@{{host}}", "/postgres"),
        "user-in-the-query": _on_test_server(
            "{host}", "/postgres", f"user={secret}%21&password=wrong"
        ),
    }[place]
    # The server's own text quotes the secret, so each case is live.
    with pytest.raises(psycopg.Error) as raw:
        psycopg.connect(url, connect_timeout=10)
    assert secret in str(raw.value)

    with pytest.raises(StoreConnectionError) as refused:
        postgres_connect(url)

    message = str(refused.value)
    assert '"***"' in message
    assert leaked(secret, message) == []
