"""The seam speaks both dialects (spec 112, Amendment (2026-10-11): split,
owner and nesting).

A `Connection` over SQLite or Postgres takes the same `?` statements, reads
rows the same way, nests transactions as savepoints, waits for a lock for
as long as its `busy_timeout_ms` says, and raises the same store error for
the same refusal. A Postgres connection opened for an owner acts as that
owner in every transaction, under the row policy (spec 105).

The domain's own SQL still speaks SQLite in places until spec 126, so the
Postgres side runs statements written for these tests, never domain
functions, except for the identity loaders, whose two reads are portable.

Each test that takes the `store` fixture runs on both stores. The Postgres
side shares one migrated database per module (`tests/pg_support.py`) and
skips locally without a test server; a test that changes privileges, and
each parity probe, takes a database of its own.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from pg_support import fresh_database, new_owner
from test_dialect_parity import PROBES, Probe

import harrier.container
import harrier.db
import harrier.hostlease
import harrier.pgstore
import harrier_cli.main as cli
from harrier.connection import (
    IDENTITY_DID_NOT_HOLD,
    NOT_AN_OWNER_ID,
    NOT_SQLITE,
    OWNER_NEEDS_POSTGRES,
    UNKNOWN_TRACK,
    UNKNOWN_TRACK_CONSTRAINT,
    URL_VARIABLE,
    Connection,
    StoreBusyError,
    StoreDatabaseError,
    StoreError,
    StoreIntegrityError,
    StoreLibraryTooOld,
    open_store,
    owner_claims,
)
from harrier.db import BUSY_TIMEOUT_MS, connect
from harrier.logredact import identity_values
from harrier.pgstore import migrate_postgres, postgres_connect
from harrier.tracker.schema import TENANT_ROLE
from harrier.tracks import DuplicateTrackError, add_track

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[Any]


class Abandoned(Exception):
    """Raised to leave a transaction() block, so it rolls back."""


# --- stores ---


@pytest.fixture(scope="module")
def migrated_postgres() -> Iterator[str]:
    """One migrated Postgres database for the module. Tests that share it
    create their own tables and owners, so none sees another's rows."""
    with fresh_database() as url:
        migrate_postgres(url)
        yield url


@dataclass
class Store:
    """Connections to one store, closed after the test."""

    dialect: str
    opener: Callable[[], Connection]
    opened: list[Connection] = field(default_factory=lambda: [])

    def open(self) -> Connection:
        conn = self.opener()
        self.opened.append(conn)
        return conn


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Store]:
    if request.param == "sqlite":
        path = tmp_path / "seam.db"
        opened = Store("sqlite", lambda: connect(path))
    else:
        url = cast(str, request.getfixturevalue("migrated_postgres"))
        opened = Store("postgres", lambda: open_store({URL_VARIABLE: url}))
    yield opened
    for conn in opened.opened:
        conn.close()


def new_table(conn: Connection, columns: str) -> str:
    """A table of the test's own, created and committed."""
    name = f"seam_{uuid.uuid4().hex[:12]}"
    with conn.transaction():
        conn.execute(f"CREATE TABLE {name} ({columns})")
    return name


def ids(conn: Connection, table: str) -> list[int]:
    return [int(row[0]) for row in conn.execute(f"SELECT id FROM {table} ORDER BY id")]


def raw(url: str) -> PgConnection:
    """The migrating role's own psycopg connection, outside the seam."""
    return postgres_connect(url)


# --- rows and placeholders ---


def test_a_row_reads_by_position_by_name_and_as_a_dict(store: Store) -> None:
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY, name TEXT, n INTEGER")
    with conn.transaction():
        conn.execute(f"INSERT INTO {table} (id, name, n) VALUES (?, ?, ?)", (7, "alpha", None))
    row = conn.execute(f"SELECT id, name, n FROM {table}").fetchone()
    assert (row[0], row[1], row[2]) == (7, "alpha", None)
    assert (row["id"], row["name"], row["n"]) == (7, "alpha", None)
    # By name in another case, as sqlite3.Row reads it.
    assert row["NAME"] == "alpha"
    assert tuple(row) == (7, "alpha", None)
    assert list(row.keys()) == ["id", "name", "n"]
    assert dict(row) == {"id": 7, "name": "alpha", "n": None}
    assert len(row) == 3
    assert type(row["id"]) is int and type(row["name"]) is str
    with pytest.raises(IndexError):
        row["missing"]
    rows = conn.execute(f"SELECT name FROM {table}").fetchall()
    assert [r["name"] for r in rows] == ["alpha"]
    assert [tuple(r) for r in conn.execute(f"SELECT name FROM {table}")] == [("alpha",)]


def test_a_failed_fetch_is_a_store_error(tmp_path: Path) -> None:
    """SQLite steps a query as its rows are read, so an error can arrive
    after `execute()` returns. Postgres sends every row with the result,
    so its errors arrive at `execute()`, which the other tests cover."""
    with closing(connect(tmp_path / "fetch.db")) as conn:
        table = new_table(conn, "id INTEGER PRIMARY KEY, body TEXT")
        with conn.transaction():
            for index, body in enumerate(('{"a": 1}', "not json"), start=1):
                conn.execute(f"INSERT INTO {table} (id, body) VALUES (?, ?)", (index, body))
        query = f"SELECT json_extract(body, '$.a') FROM {table} ORDER BY id"
        with pytest.raises(StoreDatabaseError, match="malformed JSON"):
            conn.execute(query).fetchall()
        with pytest.raises(StoreDatabaseError, match="malformed JSON"):
            list(conn.execute(query))
        # The module reads one row ahead, so the first fetch meets the second
        # row's error.
        cursor = conn.execute(query)
        with pytest.raises(StoreDatabaseError, match="malformed JSON"):
            cursor.fetchone()


def test_question_marks_bind_on_both_stores(store: Store) -> None:
    conn = store.open()
    # A `%` outside a literal, a literal holding `%` and `?`, and two
    # parameters around them.
    row = conn.execute("SELECT ? % 4, 'a%b?c', ?, '100%'", (10, "z")).fetchone()
    assert tuple(row) == (2, "a%b?c", "z", "100%")
    # No parameters at all, and a doubled quote inside a literal.
    assert tuple(conn.execute("SELECT '50%', 'it''s ?'").fetchone()) == ("50%", "it's ?")
    table = new_table(conn, "id INTEGER PRIMARY KEY, name TEXT")
    with conn.transaction():
        for index, name in enumerate(("alpha", "graphite", "beta"), start=1):
            conn.execute(f"INSERT INTO {table} (id, name) VALUES (?, ?)", (index, name))
    found = conn.execute(
        f"SELECT name FROM {table} WHERE name LIKE '%ph%' AND id >= ? ORDER BY id", (1,)
    ).fetchall()
    assert [r[0] for r in found] == ["alpha", "graphite"]
    # A `?` with no parameter for it is the store's error on both.
    with pytest.raises(StoreDatabaseError):
        conn.execute("SELECT ?, ?", (1,))


# --- transactions ---


def test_a_nested_transaction_is_a_savepoint_on_both_stores(store: Store) -> None:
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")

    with conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        with pytest.raises(StoreIntegrityError), conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        # After the caught error: on Postgres the transaction is usable again.
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (3,))
        # A savepoint that exits normally keeps its work.
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (4,))
    # Seen from another connection: the outer block committed, the inner
    # block that failed did not.
    assert ids(store.open(), table) == [1, 3, 4]

    # An error leaving the outer block rolls back both.
    with pytest.raises(Abandoned), conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (5,))
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (6,))
        raise Abandoned
    # A savepoint opened before the outer block wrote anything: on SQLite
    # it must not be the statement that begins the transaction, or its
    # release would commit.
    with pytest.raises(Abandoned), conn.transaction():
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (8,))
        raise Abandoned
    # And so does an inner error that nothing catches.
    with pytest.raises(StoreIntegrityError), conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (7,))
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
    assert ids(store.open(), table) == [1, 3, 4]
    assert not conn.in_transaction


def test_a_connection_offers_no_commit_or_rollback(store: Store) -> None:
    conn = store.open()
    assert not hasattr(conn, "commit")
    assert not hasattr(conn, "rollback")
    assert not conn.in_transaction
    with conn.transaction():
        # True before any statement, though SQLite has not begun yet.
        assert conn.in_transaction
        with conn.transaction():
            assert conn.in_transaction
        assert conn.in_transaction
    assert not conn.in_transaction
    conn.execute("SELECT 1").fetchone()
    assert not conn.in_transaction


def test_a_connection_names_its_dialect_and_its_wait(store: Store) -> None:
    conn = store.open()
    assert conn.dialect == store.dialect

    def wait_in_force() -> str:
        if store.dialect == "sqlite":
            return str(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        with conn.transaction():
            return str(conn.execute("SELECT current_setting('lock_timeout')").fetchone()[0])

    assert conn.busy_timeout_ms == BUSY_TIMEOUT_MS
    assert wait_in_force() == ("5000" if store.dialect == "sqlite" else "5s")
    conn.busy_timeout_ms = 250
    assert conn.busy_timeout_ms == 250
    assert wait_in_force() == ("250" if store.dialect == "sqlite" else "250ms")
    # 0 is "do not wait" on both. On Postgres 0 would mean no limit.
    conn.busy_timeout_ms = 0
    assert conn.busy_timeout_ms == 0
    assert wait_in_force() == ("0" if store.dialect == "sqlite" else "1ms")
    with pytest.raises(ValueError):
        conn.busy_timeout_ms = -1
    if store.dialect == "postgres":
        with pytest.raises(StoreError, match=NOT_SQLITE):
            conn.sqlite_driver  # noqa: B018


def test_a_stuck_writer_reports_busy_on_both_stores(store: Store) -> None:
    holder = store.open()
    blocked = store.open()
    table = new_table(holder, "id INTEGER PRIMARY KEY, body TEXT")
    with holder.transaction():
        holder.execute(f"INSERT INTO {table} (id, body) VALUES (?, ?)", (1, "first"))
    if store.dialect == "postgres":
        # Were the seam to send lock_timeout 0, which on Postgres means no
        # limit, the test would fail here after five seconds, not hang.
        blocked.execute("SET statement_timeout = '5s'")

    def blocked_write() -> float:
        started = time.monotonic()
        with pytest.raises(StoreBusyError), blocked.transaction(immediate=True):
            blocked.execute(f"UPDATE {table} SET body = ? WHERE id = ?", ("blocked", 1))
        return time.monotonic() - started

    with pytest.raises(Abandoned), holder.transaction(immediate=True):
        holder.execute(f"UPDATE {table} SET body = ? WHERE id = ?", ("held", 1))
        blocked.busy_timeout_ms = 300
        waited = blocked_write()
        assert 0.25 <= waited < 3, f"waited {waited:.3f}s for a 300 ms timeout"
        blocked.busy_timeout_ms = 0
        waited = blocked_write()
        assert waited < 1, f"waited {waited:.3f}s with busy_timeout_ms 0"
        raise Abandoned
    assert not blocked.in_transaction
    assert conn_body(blocked, table) == "first"


def conn_body(conn: Connection, table: str) -> str:
    return str(conn.execute(f"SELECT body FROM {table} WHERE id = ?", (1,)).fetchone()[0])


# --- the owner's identity (decision 2) ---


IDENTITY = "SELECT current_user::text, current_setting('request.jwt.claims', true)::jsonb ->> 'sub'"


def test_an_owner_connection_sets_its_identity_in_every_transaction(
    migrated_postgres: str,
) -> None:
    url = migrated_postgres
    with closing(raw(url)) as admin:
        first, second = new_owner(admin), new_owner(admin)
    with (
        closing(open_store({URL_VARIABLE: url}, owner=first)) as conn,
        closing(open_store({URL_VARIABLE: url}, owner=second)) as other,
    ):
        # Outside transaction(): the statement's own transaction.
        assert tuple(conn.execute(IDENTITY).fetchone()) == (TENANT_ROLE, first)
        with conn.transaction():
            assert tuple(conn.execute(IDENTITY).fetchone()) == (TENANT_ROLE, first)
            # A savepoint keeps what the outermost transaction set.
            with conn.transaction():
                assert tuple(conn.execute(IDENTITY).fetchone()) == (TENANT_ROLE, first)
        claims = conn.execute(
            "SELECT current_setting('request.jwt.claims', true)::jsonb"
        ).fetchone()[0]
        assert claims == {"sub": first, "role": "authenticated", "aud": "authenticated"}

        # An insert that names no owner_id takes the owner's.
        conn.execute("INSERT INTO user_config (kind, value) VALUES (?, ?)", ("feeds", "[]"))
        other.execute("INSERT INTO user_config (kind, value) VALUES (?, ?)", ("feeds", "[1]"))
        mine = conn.execute("SELECT owner_id::text, value FROM user_config").fetchall()
        assert [tuple(row) for row in mine] == [(first, "[]")]
        theirs = other.execute("SELECT owner_id::text, value FROM user_config").fetchall()
        assert [tuple(row) for row in theirs] == [(second, "[1]")]


class _SplitTransactions:
    """A psycopg connection whose BEGIN never reaches the server, as behind
    a pooler that splits a transaction: SET LOCAL then changes nothing."""

    def __init__(self, driver: PgConnection) -> None:
        self._driver = driver

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if bytes(query).strip().upper() == b"BEGIN":
            return None
        return self._driver.execute(query, params, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)


class _OtherClaims(_SplitTransactions):
    """A psycopg connection that sets another owner's claims than asked."""

    def __init__(self, driver: PgConnection, sub: str) -> None:
        super().__init__(driver)
        self._sub = sub

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        if b"set_config('request.jwt.claims'" in bytes(query):
            params = (json.dumps({"sub": self._sub, "role": "authenticated"}),)
        return self._driver.execute(query, params, **kwargs)


def test_a_lost_owner_identity_runs_no_statement(migrated_postgres: str) -> None:
    url = migrated_postgres
    with closing(raw(url)) as admin:
        owner, stranger = new_owner(admin), new_owner(admin)
        probe = f"seam_{uuid.uuid4().hex[:12]}"
        admin.execute(f"CREATE TABLE {probe} (body text)".encode())
        # The tenant may write it, so only the identity check stops a write.
        admin.execute(f"GRANT INSERT ON {probe} TO {TENANT_ROLE}".encode())

    def split(driver: PgConnection) -> object:
        return _SplitTransactions(driver)

    def other_claims(driver: PgConnection) -> object:
        return _OtherClaims(driver, stranger)

    for wrap in (split, other_claims):
        driver = postgres_connect(url)
        conn = Connection.postgres(cast(Any, wrap(driver)), claims=owner_claims(owner))
        try:
            with (
                pytest.raises(StoreDatabaseError, match=re.escape(IDENTITY_DID_NOT_HOLD)),
                conn.transaction(),
            ):
                conn.execute(f"INSERT INTO {probe} (body) VALUES (?)", ("inside",))
            with pytest.raises(StoreDatabaseError, match=re.escape(IDENTITY_DID_NOT_HOLD)):
                conn.execute(f"INSERT INTO {probe} (body) VALUES (?)", ("outside",))
        finally:
            driver.close()

    with closing(raw(url)) as admin:
        assert admin.execute(f"SELECT count(*) FROM {probe}".encode()).fetchone() == (0,)


def test_an_owner_needs_a_postgres_store(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def no_sqlite(*_: object, **__: object) -> Connection:
        opened.append("sqlite")
        raise AssertionError("the SQLite store was opened")

    def no_postgres(_url: str) -> Any:
        opened.append("postgres")
        raise AssertionError("the Postgres store was opened")

    monkeypatch.setattr(harrier.db, "connect", no_sqlite)
    monkeypatch.setattr(harrier.pgstore, "open_postgres_store", no_postgres)
    with pytest.raises(StoreError, match=f"^{OWNER_NEEDS_POSTGRES}$"):
        open_store({URL_VARIABLE: ""}, owner=str(uuid.uuid4()))
    hosted = {URL_VARIABLE: "postgresql://harrier:s3cret@db.example.test/tracker"}
    for not_an_owner in ("not-a-uuid", "", "1234"):
        with pytest.raises(StoreError, match=f"^{NOT_AN_OWNER_ID}$"):
            open_store(hosted, owner=not_an_owner)
    assert opened == []
    # A UUID in another spelling is the owner it names.
    spelled = str(uuid.uuid4())
    assert owner_claims(spelled.upper())["sub"] == spelled


# --- opening ---


def test_opening_a_postgres_store_takes_no_lease(
    migrated_postgres: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    asked: list[str] = []

    def record(what: str) -> Callable[..., Any]:
        def recorded(*_: object, **__: object) -> Any:
            asked.append(what)
            raise AssertionError(f"opening a Postgres store reached {what}")

        return recorded

    monkeypatch.setattr(harrier.db, "check_database_ownership", record("the ownership check"))
    monkeypatch.setattr(harrier.db, "probe_database_ownership", record("the ownership probe"))
    monkeypatch.setattr(harrier.db, "acquire_host_lease", record("the host lease"))
    monkeypatch.setattr(harrier.hostlease, "write_lease", record("a lease file"))
    monkeypatch.setattr(harrier.container, "detect", record("the Docker engine"))
    data = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(data))

    with closing(raw(migrated_postgres)) as admin:
        owner = new_owner(admin)
    for conn in (
        open_store({URL_VARIABLE: migrated_postgres}),
        open_store({URL_VARIABLE: migrated_postgres}, owner=owner),
    ):
        try:
            assert conn.dialect == "postgres"
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()
    assert asked == []
    assert not data.exists()
    assert not harrier.db.holds_host_lease()


def test_an_old_sqlite_library_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "the SQLite library is 3.34.1; harrier needs 3.35 or newer"
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 34, 1))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.34.1")
    path = tmp_path / "old.db"
    with pytest.raises(StoreLibraryTooOld) as refused:
        connect(path)
    assert str(refused.value) == text
    assert not path.exists()
    assert not harrier.db.holds_host_lease()
    # The CLI prints it and exits 1.
    capsys.readouterr()
    assert cli.main(["tracks", "list"]) == 1
    assert capsys.readouterr().err.strip() == f"error: {text}"

    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 35, 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.35.0")
    connect(path).close()
    assert path.exists()


# --- store errors ---


def _refusal(conn: Connection, probe: Probe) -> tuple[str, str | None, str] | None:
    """The store error a refused probe raises: its class, kind and message.
    Everything the probe wrote is rolled back."""
    outcome: tuple[str, str | None, str] | None = None
    with pytest.raises(Abandoned), conn.transaction():
        for statement in probe.setup:
            conn.execute(statement)
        try:
            with conn.transaction():
                conn.execute(probe.statement)
        except StoreDatabaseError as error:
            kind = error.kind if isinstance(error, StoreIntegrityError) else None
            outcome = (type(error).__name__, kind, str(error))
        raise Abandoned
    return outcome


# A probe whose statement the tenant role holds no privilege for: Postgres
# checks the privilege before any trigger fires, so as an owner it is
# refused there, and no trigger's text can be compared. Each is run again
# as the migrating role, which holds the privilege (spec 112, amendment of
# this pull request).
TENANT_MAY_NOT = {
    "job-events-update": "job_events",
    "job-events-delete": "job_events",
    "job-events-update-to-status": "job_events",
    "track-delete": "tracks",
    "eligibility-update-verdict": "job_eligibility",
    "eligibility-delete": "job_eligibility",
}


def _postgres_refusal(probe: Probe, *, as_owner: bool) -> tuple[str, str | None, str] | None:
    """A refused probe on a store of its own: the probes name job 1, and ids
    are numbered across owners and never given back by a rollback. As the
    owner, through the owner's connection; otherwise as the migrating role,
    with the owner's claims set so the setup's rows have an owner."""
    with fresh_database() as url:
        migrate_postgres(url)
        with closing(raw(url)) as admin:
            owner = new_owner(admin)
        if as_owner:
            with closing(open_store({URL_VARIABLE: url}, owner=owner)) as pg:
                return _refusal(pg, probe)
        with closing(open_store({URL_VARIABLE: url})) as pg:
            pg.execute(
                "SELECT set_config('request.jwt.claims', ?, false)",
                (json.dumps({"sub": owner}),),
            )
            return _refusal(pg, probe)


# The probes that name a track no store holds: a trigger refuses them on
# SQLite, the composite foreign key on Postgres, and both say so alike.
UNKNOWN_TRACK_PROBES = frozenset({"job-unknown-track", "job-track-update-unknown"})


def test_every_parity_probe_raises_one_error_on_both_stores(tmp_path: Path) -> None:
    """Each refused probe of spec 103's parity table, on a fresh store of
    each kind, run on Postgres through an owner's connection."""
    refused = [probe for probe in PROBES if probe.refused]
    assert {probe.name for probe in refused} >= set(TENANT_MAY_NOT) | UNKNOWN_TRACK_PROBES
    differences: list[str] = []
    for index, probe in enumerate(refused):
        with closing(connect(tmp_path / f"probe-{index}.db")) as lite:
            on_sqlite = _refusal(lite, probe)
        on_postgres = _postgres_refusal(probe, as_owner=True)
        if probe.name in TENANT_MAY_NOT:
            denied = f"permission denied for table {TENANT_MAY_NOT[probe.name]}"
            if on_postgres != ("StoreDatabaseError", None, denied):
                differences.append(f"{probe.name}: as the owner {on_postgres}, not {denied!r}")
            on_postgres = _postgres_refusal(probe, as_owner=False)
        assert on_sqlite is not None, f"{probe.name}: SQLite accepted a refused probe"
        assert on_sqlite[0] == "StoreIntegrityError", f"{probe.name}: {on_sqlite}"
        if on_postgres is None:
            differences.append(f"{probe.name}: Postgres accepted it")
            continue
        if on_postgres[:2] != on_sqlite[:2]:
            differences.append(f"{probe.name}: SQLite {on_sqlite}, Postgres {on_postgres}")
        expected = UNKNOWN_TRACK if probe.name in UNKNOWN_TRACK_PROBES else probe.message
        has_message = on_sqlite[1] == "refused" or expected is not None
        if has_message and not (on_sqlite[2] == on_postgres[2] == expected):
            differences.append(
                f"{probe.name}: messages {on_sqlite[2]!r} and {on_postgres[2]!r}, "
                f"expected {expected!r}"
            )
    assert differences == []


def test_the_unknown_track_key_is_the_one_migration_10_adds(migrated_postgres: str) -> None:
    """The mapping matches the key by name, so the name is read from the
    migrated store's catalog rather than trusted."""
    with closing(raw(migrated_postgres)) as admin:
        names = admin.execute(
            b"SELECT conname FROM pg_constraint WHERE contype = 'f' "
            b"AND conrelid = 'jobs'::regclass AND confrelid = 'tracks'::regclass"
        ).fetchall()
    assert names == [(UNKNOWN_TRACK_CONSTRAINT,)]


def test_a_postgres_refusal_never_quotes_the_row(migrated_postgres: str) -> None:
    """Run as the migrating role on a table of its own: under the row policy
    Postgres withholds the DETAIL, so as an owner there would be nothing
    for the check to catch."""
    secret = f"https://example.test/jobs/{uuid.uuid4().hex}"
    with closing(open_store({URL_VARIABLE: migrated_postgres})) as conn:
        table = new_table(conn, "url TEXT UNIQUE, status TEXT CHECK (status <> 'bogus')")
        conn.execute(f"INSERT INTO {table} (url, status) VALUES (?, 'ok')", (secret,))
        for statement, kind in (
            (f"INSERT INTO {table} (url, status) VALUES (?, 'ok')", "unique"),
            (f"INSERT INTO {table} (url, status) VALUES (? || '/2', 'bogus')", "check"),
        ):
            with pytest.raises(StoreIntegrityError) as refused:
                conn.execute(statement, (secret,))
            assert refused.value.kind == kind
            assert secret not in str(refused.value)
            assert secret not in refused.value.message
            # The server's DETAIL did quote the row, so the checks above
            # could have seen a leak.
            cause = cast("psycopg.Error", refused.value.__cause__)
            assert secret in (cause.diag.message_detail or "")


def test_a_duplicate_slug_is_a_duplicate_track_error(tmp_path: Path) -> None:
    with closing(connect(tmp_path / "tracks.db")) as conn:
        add_track(conn, "second", "academic", "Second search")
        with pytest.raises(DuplicateTrackError) as refused:
            add_track(conn, "second", "academic", "Again")
        assert isinstance(refused.value.__cause__, StoreIntegrityError)
        assert refused.value.__cause__.kind == "unique"


def test_identity_reads_survive_a_store_error_on_both_stores(tmp_path: Path) -> None:
    """The profile documents cannot be read; the contacts still are."""
    contact = ("Synthetic Contact Person", "synthetic.person@example.test")
    insert = "INSERT INTO contacts (person_name, person_email) VALUES (?, ?)"

    with closing(connect(tmp_path / "identity.db")) as lite:
        with lite.transaction():
            lite.execute(insert, contact)
            lite.execute("ALTER TABLE profile_documents RENAME TO profile_documents_gone")
        assert identity_values(lite) == set(contact)

    with fresh_database() as url:
        migrate_postgres(url)
        with closing(raw(url)) as admin:
            owner = new_owner(admin)
            admin.execute(f"REVOKE SELECT ON profile_documents FROM {TENANT_ROLE}".encode())
        with closing(open_store({URL_VARIABLE: url}, owner=owner)) as pg:
            pg.execute(insert, contact)
            assert identity_values(pg) == set(contact)
