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

import copy
import json
import pickle
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing
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
    JSONB_KEY_OPERATOR,
    NAMED_PLACEHOLDER,
    NOT_AN_OWNER_ID,
    NOT_SQLITE,
    NUMBERED_PLACEHOLDER,
    OWNER_NEEDS_POSTGRES,
    POSTGRES_NOT_IDLE,
    TRANSACTION_CONTROL,
    TRANSACTION_GIVEN_UP,
    UNKNOWN_TRACK,
    UNKNOWN_TRACK_CONSTRAINT,
    URL_VARIABLE,
    WRITE_LOCK_NOT_HELD,
    Connection,
    StoreBusyError,
    StoreConflictError,
    StoreDatabaseError,
    StoreError,
    StoreIntegrityError,
    StoreLibraryTooOld,
    begins_a_transaction,
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
    if store.dialect == "postgres":
        # A Postgres row copies and pickles with its names (post-merge
        # review of PR #288). sqlite3.Row does neither, so nothing on SQLite
        # relies on it.
        for copied in (copy.copy(row), copy.deepcopy(row), pickle.loads(pickle.dumps(row))):
            assert copied == row
            assert dict(copied) == dict(row)
            assert copied["NAME"] == "alpha"


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


def a_failure_that_gives_up(store: Store, conn: Connection, table: str) -> Callable[[], object]:
    """A statement whose failure leaves the store keeping none of the
    transaction it runs in (spec 112, amendment of 2026-10-13). On Postgres
    any failure does; here a second row 1 in `table`, so call it in a block
    that has written row 1. On SQLite a write past the file's page limit,
    which rolls the whole transaction back; a constraint error would not."""
    if store.dialect == "postgres":
        return lambda: conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
    filler = new_table(conn, "b BLOB")
    pages = int(conn.execute("PRAGMA page_count").fetchone()[0])
    conn.execute(f"PRAGMA max_page_count = {pages + 2}")
    return lambda: conn.execute(f"INSERT INTO {filler} (b) VALUES (randomblob(?))", (1_000_000,))


def test_a_block_whose_transaction_was_given_up_raises_at_its_exit(store: Store) -> None:
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    fail = a_failure_that_gives_up(store, conn, table)

    with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        # Caught without a savepoint: the store keeps none of the block.
        with pytest.raises(StoreDatabaseError):
            fail()
        with pytest.raises(StoreDatabaseError) as later:
            conn.execute(f"SELECT id FROM {table}")
        if store.dialect == "sqlite":
            assert str(later.value) == TRANSACTION_GIVEN_UP
        else:
            # The server refuses it in its own words; the seam adds nothing.
            assert type(later.value.__cause__).__name__ == "InFailedSqlTransaction"
            assert str(later.value) != TRANSACTION_GIVEN_UP
    # Not a busy lock or a conflict, which a caller might retry.
    assert type(at_exit.value) is StoreDatabaseError
    assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    assert at_exit.value.__cause__ is None
    assert not conn.in_transaction
    assert ids(store.open(), table) == []
    # The connection is idle, and its next block commits.
    with conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
    assert ids(store.open(), table) == [2]


def test_a_sqlite_write_after_the_store_gave_up_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "seam.db"
    store = Store("sqlite", lambda: connect(path))
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    fail = a_failure_that_gives_up(store, conn, table)

    with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        with pytest.raises(StoreDatabaseError):
            fail()
        # Without the refusal this write would begin a new transaction, and
        # the block's exit would commit it.
        with pytest.raises(StoreDatabaseError) as later:
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
        assert str(later.value) == TRANSACTION_GIVEN_UP
        with pytest.raises(StoreDatabaseError) as nested, conn.transaction(immediate=True):
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (3,))
        assert str(nested.value) == TRANSACTION_GIVEN_UP
        # Refused as it opens: it did not begin a transaction and take the
        # write lock.
        assert _write_lock_is_free(path)
        with pytest.raises(StoreDatabaseError) as read:
            conn.execute(f"SELECT id FROM {table}")
        assert str(read.value) == TRANSACTION_GIVEN_UP
        assert type(later.value) is StoreDatabaseError
    assert type(at_exit.value) is StoreDatabaseError
    assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    assert ids(store.open(), table) == []
    for opened in store.opened:
        opened.close()


def test_a_caught_failure_inside_a_nested_block_fails_the_outermost(store: Store) -> None:
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    fail = a_failure_that_gives_up(store, conn, table)

    with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        # The inner block swallows its failure and exits normally, which
        # its exit refuses; the outer block swallows that refusal too.
        with (
            pytest.raises(StoreDatabaseError) as inner,
            conn.transaction(),
            pytest.raises(StoreDatabaseError),
        ):
            fail()
        if store.dialect == "sqlite":
            # On Postgres the server refuses the release, in its own words.
            assert str(inner.value) == TRANSACTION_GIVEN_UP
    assert type(at_exit.value) is StoreDatabaseError
    assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    assert not conn.in_transaction
    assert ids(store.open(), table) == []


def test_an_adopted_sqlite_transaction_can_be_given_up(tmp_path: Path) -> None:
    """A write outside any block opens the sqlite3 module's transaction,
    which the next block adopts. Losing it is the store giving up too, or
    the block's next write would begin a new transaction and commit it
    without the first row (review of PR #304)."""
    path = tmp_path / "seam.db"
    store = Store("sqlite", lambda: connect(path))
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    fail = a_failure_that_gives_up(store, conn, table)
    conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
    assert conn.in_transaction

    with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
        with pytest.raises(StoreDatabaseError):
            fail()
        with pytest.raises(StoreDatabaseError) as later:
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
        assert str(later.value) == TRANSACTION_GIVEN_UP
    assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    assert ids(store.open(), table) == []
    for opened in store.opened:
        opened.close()


# SQLite's heap limit holds for the whole process, and PRAGMA can only lower
# it, so the out of memory case runs in a child that takes the limit with it.
OUT_OF_MEMORY_CHILD = """
import sys
from contextlib import closing
from pathlib import Path

from harrier.connection import StoreDatabaseError
from harrier.db import connect

path = Path(sys.argv[1])
with closing(connect(path)) as conn, closing(connect(path)) as other:
    with conn.transaction():
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE f (b BLOB)")
    try:
        with conn.transaction():
            conn.execute("INSERT INTO t (id) VALUES (?)", (1,))
            conn.execute("PRAGMA hard_heap_limit = 8000000")
            try:
                conn.execute(
                    "INSERT INTO f (b) VALUES (zeroblob(?) || randomblob(?))", (20000000, 10)
                )
            except MemoryError:
                print("memory error")
    except StoreDatabaseError as error:
        print(error)
    else:
        print("committed")
    print([int(row[0]) for row in other.execute("SELECT id FROM t")])
"""


def test_an_out_of_memory_failure_gives_up_the_sqlite_transaction(tmp_path: Path) -> None:
    """SQLite's out of memory rolls the transaction back and reaches Python
    as MemoryError, not a sqlite3.Error (review of PR #304)."""
    child = subprocess.run(
        [sys.executable, "-c", OUT_OF_MEMORY_CHILD, str(tmp_path / "seam.db")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.splitlines() == ["memory error", TRANSACTION_GIVEN_UP, "[]"]


def test_a_closed_sqlite_connection_still_raises_a_store_error(tmp_path: Path) -> None:
    """After a block that held a transaction ends, nothing reads the closed
    driver on its account (review of PR #304)."""
    conn = connect(tmp_path / "seam.db")
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    with conn.transaction():
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
    conn.close()
    with pytest.raises(StoreDatabaseError):
        conn.execute(f"SELECT id FROM {table}")


def test_a_commit_inside_a_block_is_not_the_store_giving_up(tmp_path: Path) -> None:
    """Only a failed statement gives a transaction up. A COMMIT the caller
    ran also ends it, and its work was kept (review of PR #304). The seam
    refuses a COMMIT through `execute()`, so on SQLite it runs through
    `sqlite_driver`, which the seam does not see; Postgres has no such way
    in (spec 112, amendment of 2026-10-13: transaction control)."""
    path = tmp_path / "seam.db"
    with closing(connect(path)) as conn, closing(connect(path)) as other:
        table = new_table(conn, "id INTEGER PRIMARY KEY")

        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            conn.sqlite_driver.execute("COMMIT")
            conn.execute(f"SELECT id FROM {table}").fetchall()
        assert ids(other, table) == [1]


def test_a_sqlite_block_that_only_read_has_nothing_to_give_up(tmp_path: Path) -> None:
    """A failed read in a block that began no transaction gives nothing up,
    though the block before it began one and committed."""
    path = tmp_path / "seam.db"
    with closing(connect(path)) as conn:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        with conn.transaction():
            with pytest.raises(StoreDatabaseError):
                conn.execute("SELECT id FROM no_such_table")
            conn.execute(f"SELECT id FROM {table}").fetchall()
        assert ids(conn, table) == [1]


def test_a_caught_sqlite_constraint_error_keeps_the_transaction(tmp_path: Path) -> None:
    path = tmp_path / "seam.db"
    with closing(connect(path)) as conn, closing(connect(path)) as other:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        with conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            # SQLite undoes the refused statement only.
            with pytest.raises(StoreIntegrityError):
                conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
        assert ids(other, table) == [1, 2]


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


# --- the post-merge review of PR #288 ---


def _write_lock_is_free(path: Path) -> bool:
    """Whether another connection to the SQLite file can take the write lock
    at once. It takes it and gives it back, and waits for nothing."""
    with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as other:
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            assert "locked" in str(error)
            return False
        other.execute("ROLLBACK")
        return True


def test_a_block_that_asks_for_the_write_lock_holds_it_at_any_depth(tmp_path: Path) -> None:
    """A deferred block that has only read, then a block inside it that
    asks for `immediate`: the write lock is taken there and held until the
    outermost block ends, as the write lock before the seam was. A caller
    that wraps a locked writer in its own block keeps spec 079's
    guarantee."""
    path = tmp_path / "lock.db"
    with closing(connect(path)) as conn:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        with conn.transaction():
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            assert _write_lock_is_free(path)
            with conn.transaction(immediate=True):
                assert not _write_lock_is_free(path)
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            # Held past the inner block, to the end of the outermost.
            assert not _write_lock_is_free(path)
        assert _write_lock_is_free(path)

        # Two levels down, under deferred blocks that have not begun either.
        with conn.transaction(), conn.transaction():
            with conn.transaction(immediate=True):
                assert not _write_lock_is_free(path)
                conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            assert not _write_lock_is_free(path)
        assert _write_lock_is_free(path)
        assert ids(conn, table) == [1]

        # A write outside any block took the lock, so a block that asks for
        # it then is satisfied, and commits the write, as before the seam.
        conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (2,))
        with conn.transaction(immediate=True):
            assert not _write_lock_is_free(path)
        assert _write_lock_is_free(path)
        with closing(connect(path)) as reader:
            assert ids(reader, table) == [1, 2]


def test_the_write_lock_is_refused_inside_a_transaction_begun_without_it(
    tmp_path: Path,
) -> None:
    """A transaction begun with a plain BEGIN may hold a read snapshot that
    another commit has made stale, and the lock cannot be taken inside it
    safely. Asking for it is refused, at any depth, and the transaction is
    left as it was found."""
    path = tmp_path / "foreign.db"
    with closing(connect(path)) as conn:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        conn.sqlite_driver.execute("BEGIN")
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        with (
            pytest.raises(StoreDatabaseError, match=f"^{WRITE_LOCK_NOT_HELD}$"),
            conn.transaction(immediate=True),
        ):
            pytest.fail("the block ran")
        assert conn.sqlite_driver.in_transaction
        with conn.transaction():
            with (
                pytest.raises(StoreDatabaseError, match=f"^{WRITE_LOCK_NOT_HELD}$"),
                conn.transaction(immediate=True),
            ):
                pytest.fail("the block ran")
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        assert not conn.in_transaction
        assert ids(conn, table) == [1]


def test_a_nested_block_waits_for_the_lock_as_the_outermost_does(tmp_path: Path) -> None:
    """A block reads, another connection commits, the block writes. At any
    depth the block began no transaction before its write, so it read no
    snapshot that the commit made stale, and the write succeeds."""
    path = tmp_path / "snapshot.db"
    with closing(connect(path)) as conn, closing(connect(path)) as other:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        next_id = iter(range(1, 100))
        for depth in (1, 2, 3):
            with ExitStack() as blocks:
                for _ in range(depth):
                    blocks.enter_context(conn.transaction())
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                with other.transaction():
                    other.execute(f"INSERT INTO {table} (id) VALUES (?)", (next(next_id),))
                conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (next(next_id),))
        assert ids(other, table) == [1, 2, 3, 4, 5, 6]


def _stale_snapshot_write(store: Store) -> Callable[[], None]:
    """A write from a transaction whose snapshot another connection's
    commit has replaced. SQLite: a BEGIN the seam did not run, then a read.
    Postgres: REPEATABLE READ, which the seam never asks for, then a read."""
    conn = store.open()
    other = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY, body TEXT")
    with conn.transaction():
        conn.execute(f"INSERT INTO {table} (id, body) VALUES (?, ?)", (1, "first"))

    def update(on: Connection, body: str) -> None:
        on.execute(f"UPDATE {table} SET body = ? WHERE id = ?", (body, 1))

    def write() -> None:
        if store.dialect == "sqlite":
            conn.sqlite_driver.execute("BEGIN")
            conn.execute(f"SELECT body FROM {table}").fetchone()
            with other.transaction():
                update(other, "other")
            update(conn, "mine")
            return
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute(f"SELECT body FROM {table}").fetchone()
            with other.transaction():
                update(other, "other")
            update(conn, "mine")

    return write


def test_a_stale_snapshot_is_a_conflict_not_a_busy_lock(store: Store) -> None:
    """No lock was waited for: the store refused the write at once, and
    waiting longer would not help. StoreBusyError says a lock was not taken
    within the timeout, so this is StoreConflictError, on both stores."""
    write = _stale_snapshot_write(store)
    started = time.monotonic()
    with pytest.raises(StoreConflictError) as refused:
        write()
    assert not isinstance(refused.value, StoreBusyError)
    # The connection would wait up to BUSY_TIMEOUT_MS for a lock; it did not.
    assert time.monotonic() - started < BUSY_TIMEOUT_MS / 2000


def test_a_connection_lost_while_giving_up_still_says_so(migrated_postgres: str) -> None:
    """The rollback at the exit fails when the connection is gone. The error
    the caller sees is still the store giving up (spec 112, amendment of
    2026-10-13, Failure modes)."""
    with closing(open_store({URL_VARIABLE: migrated_postgres})) as conn:
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        pid = int(conn.execute("SELECT pg_backend_pid()").fetchone()[0])
        with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            with pytest.raises(StoreIntegrityError):
                conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
            with closing(raw(migrated_postgres)) as admin:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
                deadline = time.monotonic() + 5
                while admin.execute(
                    "SELECT 1 FROM pg_stat_activity WHERE pid = %s", (pid,)
                ).fetchone():
                    assert time.monotonic() < deadline, "the backend did not end"
                    time.sleep(0.05)
        assert type(at_exit.value) is StoreDatabaseError
        assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    with closing(open_store({URL_VARIABLE: migrated_postgres})) as other:
        assert ids(other, table) == []


def test_a_postgres_driver_in_a_transaction_is_never_adopted(migrated_postgres: str) -> None:
    """A driver already in a transaction, from a foreign BEGIN, a pooler or
    a failed ROLLBACK, would run the seam's statements as the login role,
    with no claims and no lock timeout, and the block would commit them.
    The seam refuses it, with an owner or without, and leaves it as it was
    found."""
    from psycopg.pq import TransactionStatus

    url = migrated_postgres
    with closing(raw(url)) as admin:
        owner = new_owner(admin)
        probe = f"seam_{uuid.uuid4().hex[:12]}"
        admin.execute(f"CREATE TABLE {probe} (body text)".encode())
        admin.execute(f"GRANT INSERT, SELECT ON {probe} TO {TENANT_ROLE}".encode())

    for claims in (owner_claims(owner), None):
        with closing(raw(url)) as driver:
            driver.execute(b"BEGIN")
            conn = Connection.postgres(driver, claims=claims)
            with pytest.raises(StoreDatabaseError, match=f"^{POSTGRES_NOT_IDLE}$"):
                conn.execute(f"INSERT INTO {probe} (body) VALUES (?)", ("outside",))
            with (
                pytest.raises(StoreDatabaseError, match=f"^{POSTGRES_NOT_IDLE}$"),
                conn.transaction(),
            ):
                conn.execute(f"INSERT INTO {probe} (body) VALUES (?)", ("inside",))
            # Neither rolled back nor committed: still the caller's.
            assert driver.info.transaction_status == TransactionStatus.INTRANS
            driver.execute(b"COMMIT")
            # Idle again, the seam begins as it always does.
            conn.execute(f"INSERT INTO {probe} (body) VALUES (?)", ("idle",))
    with closing(raw(url)) as admin:
        rows = admin.execute(f"SELECT body FROM {probe}".encode()).fetchall()
    assert rows == [("idle",), ("idle",)]


# Synthetic values a store could quote back. None is a real person's.
SECRET = "synthetic.person@example.test"
OUT_OF_RANGE = "98765432109"
NO_SUCH_DATE = "2031-13-45"


def _postgres_value_refusals(conn: Connection, table: str) -> list[tuple[str, StoreDatabaseError]]:
    """Each SQLSTATE class 22 refusal the test provokes with a bound value,
    with the wording its message keeps."""
    cases: list[tuple[str, str, object]] = [
        ("invalid input syntax for type integer", f"INSERT INTO {table} (n) VALUES (?)", SECRET),
        ("invalid input syntax for type date", f"INSERT INTO {table} (d) VALUES (?)", SECRET),
        ("invalid input syntax for type uuid", f"INSERT INTO {table} (u) VALUES (?)", SECRET),
        ("invalid input syntax for type boolean", f"INSERT INTO {table} (b) VALUES (?)", SECRET),
        ("invalid input syntax for type numeric", "SELECT CAST(? AS numeric)", SECRET),
        (
            "invalid input syntax for type timestamp with time zone",
            f"INSERT INTO {table} (t) VALUES (?)",
            SECRET,
        ),
        ("value is out of range for type integer", "SELECT CAST(? AS integer)", OUT_OF_RANGE),
        ("date/time field value out of range", "SELECT CAST(? AS date)", NO_SUCH_DATE),
        (
            "value too long for type character varying(5)",
            f"INSERT INTO {table} (s) VALUES (?)",
            SECRET,
        ),
        ("invalid input syntax for type json", "SELECT CAST(? AS jsonb)", SECRET),
        ("time zone not recognized", "SELECT now() AT TIME ZONE ?", SECRET),
        ("malformed array literal", "SELECT CAST(? AS text[])", SECRET),
        (
            "invalid byte sequence for encoding",
            "SELECT convert_from(CAST(? AS bytea), 'UTF8')",
            b"\xff\xfe",
        ),
    ]
    refused: list[tuple[str, StoreDatabaseError]] = []
    for wording, statement, value in cases:
        with pytest.raises(StoreDatabaseError) as error:
            conn.execute(statement, (value,))
        refused.append((wording, error.value))
    return refused


def _sqlite_value_refusals(conn: Connection, table: str) -> list[tuple[str, StoreDatabaseError]]:
    """The SQLite refusals of a bound value, with the wording each keeps."""
    cases = [
        ("bad JSON path", "SELECT json_extract('{}', ?)"),
        ("malformed JSON", "SELECT json_extract(?, '$.a')"),
        ("cannot store TEXT value in INTEGER column", f"INSERT INTO {table} (n) VALUES (?)"),
    ]
    refused: list[tuple[str, StoreDatabaseError]] = []
    for wording, statement in cases:
        with pytest.raises(StoreDatabaseError) as error:
            conn.execute(statement, (SECRET,))
        refused.append((wording, error.value))
    return refused


def test_no_store_error_quotes_a_bound_value(store: Store) -> None:
    """A store error's message keeps the wording that names the type and
    drops the value the store quoted, on both stores. The driver's own
    error, chained as the cause, did quote it, so the test could see a
    leak."""
    conn = store.open()
    if store.dialect == "postgres":
        columns = "n integer, d date, u uuid, b boolean, t timestamptz, s varchar(5)"
        refused = _postgres_value_refusals(conn, new_table(conn, columns))
    else:
        table = f"seam_{uuid.uuid4().hex[:12]}"
        with conn.transaction():
            conn.execute(f"CREATE TABLE {table} (n INTEGER) STRICT")
        refused = _sqlite_value_refusals(conn, table)
    values = (SECRET, OUT_OF_RANGE, NO_SUCH_DATE, "0xff", "xff")
    quoted_by_the_driver = 0
    for wording, error in refused:
        assert str(error).startswith(wording), (wording, str(error))
        assert not any(value in str(error) for value in values), str(error)
        quoted_by_the_driver += any(value in str(error.__cause__) for value in values)
    # SQLite quotes one of its three; Postgres most of its own.
    assert quoted_by_the_driver >= 1


def test_a_statement_reads_alike_on_both_stores(store: Store) -> None:
    """Comments, quoted identifiers and doubled quotes hold a `?` or an
    apostrophe that is no placeholder, on both stores, and so do
    Postgres's own E'' strings and dollar quotes. A shape the two stores
    would read differently is refused at the seam, on both, before
    anything runs."""
    conn = store.open()
    portable: list[tuple[str, tuple[object, ...], tuple[object, ...]]] = [
        ("SELECT ? -- it's a why?\n, ?", ("a", "b"), ("a", "b")),
        ("SELECT ? /* why? it's */, 'it''s ?'", ("a",), ("a", "it's ?")),
        ('SELECT 1 AS "a?b", 2 AS "it\'s", ?', ("a",), (1, 2, "a")),
        ("SELECT ? % 3, '50%'", (7,), (1, "50%")),
    ]
    if store.dialect == "postgres":
        portable += [
            ("SELECT E'it\\'s ?', ?::text", ("a",), ("it's ?", "a")),
            ("SELECT $$what? it's$$, $tag$?$tag$, ?::text", ("a",), ("what? it's", "?", "a")),
        ]
    for statement, params, expected in portable:
        assert tuple(conn.execute(statement, params).fetchone()) == expected, statement

    table = new_table(conn, "id INTEGER PRIMARY KEY")
    refusals: list[tuple[str, Any, str]] = [
        ("SELECT ?| ARRAY['a']", (), JSONB_KEY_OPERATOR),
        ("SELECT ?& ARRAY['a']", (), JSONB_KEY_OPERATOR),
        ("SELECT ?1", (1,), NUMBERED_PLACEHOLDER),
        ("SELECT :name", (1,), NAMED_PLACEHOLDER),
        ("SELECT ?", {"name": 1}, NAMED_PLACEHOLDER),
        ("SELECT ?, ?", (1,), "the statement has 2 placeholders and 1 parameters were passed"),
        ("SELECT ?", (1, 2), "the statement has 1 placeholders and 2 parameters were passed"),
        (f"INSERT INTO {table} (id) VALUES (?), (:two)", (1,), NAMED_PLACEHOLDER),
    ]
    for statement, params, text in refusals:
        with pytest.raises(StoreDatabaseError, match=f"^{re.escape(text)}$") as refused:
            conn.execute(statement, params)
        # Refused by the seam, not by a driver, and nothing was left open.
        assert refused.value.__cause__ is None, statement
        assert not conn.in_transaction, statement
    assert ids(conn, table) == []


def test_the_seam_begins_where_the_sqlite3_module_would() -> None:
    """The seam begins a SQLite block's transaction before each statement
    the sqlite3 module would begin one for, so the module never begins one
    under a block whose savepoints are not open. Held to the module's own
    behavior, so a Python whose rule changes fails here."""
    corpus = [
        "INSERT INTO t VALUES (1)",
        "  \n\tinsert into t values (1)",
        "\fUPDATE t SET v = 2",
        "-- a comment\nINSERT INTO t VALUES (1)",
        "/* a */ -- b\n /* c */DELETE FROM t WHERE v = 9",
        "REPLACE INTO t VALUES (1)",
        "Update t SET v = 3",
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "SELECT 1",
        "CREATE TABLE u (a)",
        "PRAGMA user_version",
    ]
    for sql in corpus:
        with closing(sqlite3.connect(":memory:")) as driver:
            driver.execute("CREATE TABLE t (v)")
            driver.execute(sql)
            assert begins_a_transaction(sql) == driver.in_transaction, sql


# --- transaction control (amendment of 2026-10-13: transaction control) ---


CONTROL_STATEMENTS = [
    "BEGIN",
    "START TRANSACTION",
    "COMMIT",
    "END",
    "ROLLBACK",
    "ROLLBACK TO SAVEPOINT harrier_savepoint_1",
    "ABORT",
    "SAVEPOINT s",
    "RELEASE s",
    "PREPARE TRANSACTION 'x'",
    "COMMIT PREPARED 'x'",
]


def _refused(conn: Connection, sql: str, params: Any = ()) -> None:
    """`sql` is refused as transaction control: the class itself, the fixed
    text, and no driver error under it, so no driver ran it."""
    with pytest.raises(StoreDatabaseError) as refused:
        conn.execute(sql, params)
    assert type(refused.value) is StoreDatabaseError, sql
    assert str(refused.value) == TRANSACTION_CONTROL, sql
    assert refused.value.__cause__ is None, sql


def _the_blocks_transaction(store: Store, conn: Connection) -> object:
    """What shows the open transaction is still the one the block began. On
    SQLite the driver is in it. On Postgres the lock timeout the seam set:
    a statement after a COMMIT or a ROLLBACK runs in autocommit, and reads
    the login default."""
    if store.dialect == "sqlite":
        return conn.sqlite_driver.in_transaction
    return conn.execute("SELECT current_setting('lock_timeout')").fetchone()[0]


def test_transaction_control_through_execute_is_refused(store: Store) -> None:
    conn = store.open()
    other = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    insert = f"INSERT INTO {table} (id) VALUES (?)"

    for sql in CONTROL_STATEMENTS:
        with pytest.raises(Abandoned), conn.transaction():
            conn.execute(insert, (1,))
            before = _the_blocks_transaction(store, conn)
            _refused(conn, sql)
            # Nothing ran: no ROLLBACK took row 1, and the transaction is
            # the block's.
            assert ids(conn, table) == [1], sql
            assert _the_blocks_transaction(store, conn) == before, sql
            raise Abandoned
        # No COMMIT kept row 1: the block's rollback took it.
        assert ids(other, table) == [], sql
        assert not conn.in_transaction, sql

    # Read before the placeholders, whose own refusals would name them.
    with pytest.raises(Abandoned), conn.transaction():
        for params in ({"name": 1}, (1,)):
            _refused(conn, "COMMIT", params)
        _refused(conn, "SAVEPOINT :name", (1,))
        raise Abandoned


def test_a_refused_control_statement_gives_nothing_up(store: Store) -> None:
    conn = store.open()
    other = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    insert = f"INSERT INTO {table} (id) VALUES (?)"

    with conn.transaction():
        conn.execute(insert, (1,))
        _refused(conn, "COMMIT")
        conn.execute(insert, (2,))
    assert ids(other, table) == [1, 2]

    with conn.transaction():
        conn.execute(insert, (3,))
        with conn.transaction():
            conn.execute(insert, (4,))
            _refused(conn, "ROLLBACK")
        conn.execute(insert, (5,))
    assert ids(other, table) == [1, 2, 3, 4, 5]

    # In a block the store already gave up on, the refusal is checked
    # first, and the block's exit still says the store gave up.
    fail = a_failure_that_gives_up(store, conn, table)
    with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
        conn.execute(insert, (6,))
        with pytest.raises(StoreDatabaseError):
            fail()
        _refused(conn, "COMMIT")
    assert str(at_exit.value) == TRANSACTION_GIVEN_UP
    assert ids(other, table) == [1, 2, 3, 4, 5]


def test_transaction_control_outside_a_block_is_refused(
    store: Store, request: pytest.FixtureRequest
) -> None:
    if store.dialect == "sqlite":
        conn = store.open()
        table = new_table(conn, "id INTEGER PRIMARY KEY")
        _refused(conn, "BEGIN")
        # No transaction was left open without the write lock.
        assert not conn.in_transaction
        with conn.transaction(immediate=True):
            conn.execute(f"INSERT INTO {table} (id) VALUES (?)", (1,))
        assert ids(store.open(), table) == [1]
        return

    url = cast(str, request.getfixturevalue("migrated_postgres"))
    driver = postgres_connect(url)
    try:
        conn = Connection.postgres(
            cast(Any, _SplitTransactions(driver)), claims=owner_claims(str(uuid.uuid4()))
        )
        # Any statement the seam begins a transaction for fails here.
        with pytest.raises(StoreDatabaseError, match=re.escape(IDENTITY_DID_NOT_HOLD)):
            conn.execute("SELECT 1")
        # Refused before the seam began a transaction for it.
        _refused(conn, "COMMIT")
        assert not conn.in_transaction
    finally:
        driver.close()


def test_the_seam_reads_a_first_word_as_either_store_would(store: Store) -> None:
    conn = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    refused = [
        "\vCOMMIT",
        ";COMMIT",
        "-- a comment\nCOMMIT",
        # Postgres ends a line comment at a carriage return; SQLite does not.
        "-- a comment\rCOMMIT",
        "/* x */ rollback",
        # Postgres nests block comments, so COMMIT is outside this one.
        "/* /* */ */ COMMIT",
        # SQLite ends one at the first */, so COMMIT is outside this one.
        "/* /* */ COMMIT",
        # A SELECT on Postgres, a COMMIT on SQLite's reading: refused.
        "/* /* */ COMMIT */ SELECT 1",
        "Begin Immediate",
    ]
    for sql in refused:
        _refused(conn, sql)
        assert not conn.in_transaction, sql

    runs = [
        "SELECT 'COMMIT'",
        "WITH c AS (SELECT 'COMMIT' AS x) SELECT x FROM c",
        "-- COMMIT\nSELECT 1",
        'SELECT 1 AS "commit"',
    ]
    if store.dialect == "postgres":
        runs += [
            f"PREPARE seam_{uuid.uuid4().hex[:12]} AS SELECT 1",
            "DO $$ BEGIN NULL; END $$",
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        ]
    else:
        runs.append(f"CREATE TEMP TRIGGER seam_trigger AFTER INSERT ON {table} BEGIN SELECT 1; END")
    for sql in runs:
        conn.execute(sql)
        assert not conn.in_transaction, sql

    # A word is read whole: COMMITTED is not COMMIT, so the store reads it.
    with pytest.raises(StoreDatabaseError) as unknown:
        conn.execute("COMMITTED")
    assert str(unknown.value) != TRANSACTION_CONTROL
    assert unknown.value.__cause__ is not None


SECOND_STATEMENT = {
    "postgres": "cannot insert multiple commands into a prepared statement",
    "sqlite": "You can only execute one statement at a time.",
}


def test_a_second_statement_in_one_call_runs_nothing(store: Store) -> None:
    conn = store.open()
    other = store.open()
    table = new_table(conn, "id INTEGER PRIMARY KEY")
    insert = f"INSERT INTO {table} (id) VALUES (?)"
    two = f"INSERT INTO {table} (id) VALUES (1); COMMIT"

    with pytest.raises(StoreDatabaseError) as outside:
        conn.execute(two)
    # The store's refusal, not the seam's.
    assert str(outside.value) == SECOND_STATEMENT[store.dialect]
    assert outside.value.__cause__ is not None
    assert not conn.in_transaction
    assert ids(other, table) == []

    if store.dialect == "postgres":
        # The server's refusal fails the transaction, as any failure does.
        with pytest.raises(StoreDatabaseError) as at_exit, conn.transaction():
            conn.execute(insert, (2,))
            with pytest.raises(StoreDatabaseError) as inside:
                conn.execute(two)
            assert str(inside.value) == SECOND_STATEMENT["postgres"]
        assert str(at_exit.value) == TRANSACTION_GIVEN_UP
        assert ids(other, table) == []
        # One statement still runs with a trailing `;` or comment, and a
        # BEGIN ATOMIC body is one statement.
        assert tuple(conn.execute("SELECT 1;").fetchone()) == (1,)
        assert tuple(conn.execute("SELECT 1; -- done").fetchone()) == (1,)
        conn.execute(
            "CREATE FUNCTION pg_temp.seam_atomic() RETURNS integer LANGUAGE sql "
            "BEGIN ATOMIC SELECT 1; SELECT 2; END"
        )
        assert tuple(conn.execute("SELECT pg_temp.seam_atomic()").fetchone()) == (2,)
        return

    # On SQLite the module's refusal leaves the transaction usable.
    with conn.transaction():
        conn.execute(insert, (2,))
        with pytest.raises(StoreDatabaseError) as inside:
            conn.execute(two)
        assert str(inside.value) == SECOND_STATEMENT["sqlite"]
        conn.execute(insert, (3,))
    assert ids(other, table) == [2, 3]
