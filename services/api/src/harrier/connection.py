"""The connection the domain speaks through, and the store a process opens.

Every domain function takes a `Connection`, harrier's own type, never a
driver's (spec 112). It wraps a SQLite connection or a psycopg one, and
speaks both: `?` placeholders on both, one set of store errors for the
driver's, and transactions that nest as savepoints. A Postgres connection
opened for an owner acts as that owner in every transaction, under the row
policy (spec 105): the seam sets the role and the claims, and domain SQL
names no tenant (ADR-013 decision 2).

A `with` block over the connection itself is not offered. On a sqlite3
connection it commits or rolls back and leaves the connection open (spec
076); on a psycopg connection the same block closes it. `Connection` has no
`__enter__`, so pyright refuses one, and a transaction is written
`with conn.transaction():`. Nor does it offer `commit()` or `rollback()`:
a transaction ends where its block does.

`HARRIER_DATABASE_URL` chooses the store. Unset or empty, the local SQLite
store opens exactly as before (`harrier.db`). A `postgresql://` URL names
the hosted store (`harrier.pgstore`). Anything else is refused rather than
read as "no URL", because a typo that fell back to SQLite would have the
operator believe they were writing to Postgres (spec 103).

The store errors and the store choice live here, not in `harrier.pgstore`,
because they belong to no one driver (spec 112, post-merge review of PRs
#194 and #207). `harrier.pgstore` keeps only driver code.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, SupportsIndex, overload
from urllib.parse import parse_qsl, unquote, urlsplit

if TYPE_CHECKING:
    import psycopg

URL_VARIABLE = "HARRIER_DATABASE_URL"
POSTGRES_SCHEME = "postgresql"

# What every command but `harrier store`, and the API, answer while the
# domain still speaks only SQLite. One text, so the CLI and the API cannot
# drift apart (spec 103).
POSTGRES_NOT_YET = (
    "HARRIER_DATABASE_URL names a Postgres store; only harrier store runs on it until spec 112"
)

# Characters urlsplit and libpq read differently: urlsplit starts a
# fragment at `#` and drops a tab, CR or LF; libpq keeps each as text.
_READ_DIFFERENTLY = ("#", "\t", "\r", "\n")

# The refusal of a URL whose delimiters can be read more than one way. Fixed
# text, so it quotes nothing of the URL, and it names the fix: each such URL
# is one with a reserved character written raw inside a part (spec 103,
# post-merge review of PR #243).
DELIMITER_REFUSAL = (
    f"{URL_VARIABLE} must be empty or a postgresql:// URL; percent-encode any / ? # @ "
    "or control character in its user, password, database name and query values"
)

# A user, database name or query value harrier prints, or leaves unmasked in
# the driver's text: letters, digits, `_`, `.` and `-`, the shape of a role
# or database name created without quotes, at most Postgres's 63 bytes for
# an identifier. Anything else may be part of a password that a raw
# delimiter pushed out of the authority, so it is withheld (spec 103).
_PRINTABLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}")

# The parameters an error names on purpose: `describe()` prints the host and
# port, and the driver's text quotes them.
_PRINTED = frozenset({"host", "hostaddr", "port"})


class StoreError(Exception):
    """A store could not be chosen, reached or used. Safe to print."""


class StoreUrlError(StoreError):
    """`HARRIER_DATABASE_URL` is neither empty nor a postgresql:// URL."""


class StoreDriverMissing(StoreError):
    """A Postgres URL is set and psycopg is not installed."""


class StoreConnectionError(StoreError):
    """The Postgres store could not be reached or refused the connection."""


class StoreVersionError(StoreError):
    """The store's schema version is not the one this code needs."""


class StoreServerError(StoreError):
    """The Postgres server refused a statement after `harrier store` connected.

    The message is `pgstore.server_reason`'s one line: the server's primary
    message, scrubbed, or the driver error's class. The driver's error is
    not chained to it (spec 128).
    """


class StoreMigrationRefused(StoreError):
    """A migration's own precheck refused this store (spec 105).

    The message is the text the migration raises, which names no URL.
    """


class StoreLibraryTooOld(StoreError):
    """The SQLite library is older than harrier needs (spec 112)."""


class StoreDatabaseError(StoreError):
    """A statement through a `Connection` failed (spec 112).

    The same class for the same failure on both stores, with the driver's
    error as `__cause__`. On Postgres the message is the server's primary
    message, never its `DETAIL`, which quotes the row; an error without one,
    such as a lost connection, is named by its class.
    """


IntegrityKind = Literal["unique", "check", "foreign_key", "not_null", "refused"]


class StoreIntegrityError(StoreDatabaseError):
    """The store refused a row: a key, a check, a reference, a missing value,
    or a trigger's refusal (`kind="refused"`, whose message is the
    trigger's text on both stores)."""

    def __init__(self, kind: IntegrityKind, message: str) -> None:
        super().__init__(message)
        self.kind: IntegrityKind = kind
        self.message = message


class StoreBusyError(StoreDatabaseError):
    """A writer waited past the connection's `busy_timeout_ms` for a lock
    another connection held."""


class Row(Protocol):
    """A result row (spec 112).

    Read by position (`row[0]`) or by column name (`row["id"]`), iterated
    over its values (`tuple(row)`), and read as a mapping through `keys()`,
    so `dict(row)` works. `sqlite3.Row` satisfies it.
    """

    def __getitem__(self, key: int | str, /) -> Any: ...

    def __iter__(self) -> Iterator[Any]: ...

    def __len__(self) -> int: ...

    def keys(self) -> list[str]: ...


class Cursor(Protocol):
    """What `Connection.execute` returns: the reads the domain makes of it."""

    @property
    def rowcount(self) -> int: ...

    @property
    def lastrowid(self) -> int | None: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


Dialect = Literal["sqlite", "postgres"]

# The refusal both stores give a job naming a track that does not exist.
# SQLite raises it from a trigger (`tracker/schema.py`, migration 4);
# Postgres from the composite foreign key migration 10 adds, which Postgres
# names after its table and columns. The seam maps that key to the
# trigger's text (spec 112, Store errors).
UNKNOWN_TRACK = "unknown track"
UNKNOWN_TRACK_CONSTRAINT = "jobs_owner_id_track_id_fkey"

# Spec 106's text, "The run's database session".
IDENTITY_DID_NOT_HOLD = "the run's database identity did not hold"
OWNER_NEEDS_POSTGRES = "an owner applies only to a Postgres store"
NOT_AN_OWNER_ID = "the owner is not an owner id"
NOT_SQLITE = "this connection is not SQLite"
OLDEST_SQLITE = (3, 35, 0)

_SQLITE_KINDS: dict[int, IntegrityKind] = {
    sqlite3.SQLITE_CONSTRAINT_UNIQUE: "unique",
    sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY: "unique",
    sqlite3.SQLITE_CONSTRAINT_CHECK: "check",
    sqlite3.SQLITE_CONSTRAINT_FOREIGNKEY: "foreign_key",
    sqlite3.SQLITE_CONSTRAINT_NOTNULL: "not_null",
    # RAISE(ABORT, ...) in a trigger.
    sqlite3.SQLITE_CONSTRAINT_TRIGGER: "refused",
}

_POSTGRES_KINDS: dict[str, IntegrityKind] = {
    "23505": "unique",
    "23514": "check",
    "23503": "foreign_key",
    "23502": "not_null",
    # RAISE EXCEPTION, which harrier_refuse() uses for every refusal trigger.
    "P0001": "refused",
}
_POSTGRES_LOCK_NOT_AVAILABLE = "55P03"


def store_error_from_sqlite(error: sqlite3.Error) -> StoreDatabaseError:
    """The store error for a sqlite3 error; its text is the driver's own."""
    message = str(error)
    # An error the sqlite3 module raises itself, such as a statement on a
    # closed connection, carries no code from the library.
    code: int | None = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return StoreDatabaseError(message)
    if isinstance(error, sqlite3.IntegrityError) and code in _SQLITE_KINDS:
        return StoreIntegrityError(_SQLITE_KINDS[code], message)
    # The low byte is the primary code: SQLITE_BUSY and its extended forms.
    if code & 0xFF == sqlite3.SQLITE_BUSY:
        return StoreBusyError(message)
    return StoreDatabaseError(message)


def store_error_from_postgres(error: psycopg.Error) -> StoreDatabaseError:
    """The store error for a psycopg error.

    Its text is the server's primary message: the `DETAIL` quotes the row's
    values, and `str()` of a psycopg error includes it. An error the server
    did not send, such as a lost connection, is named by its class, since
    the client's text is not the server's and nothing promises what it
    quotes.
    """
    diag = error.diag
    message = diag.message_primary or type(error).__name__
    state = error.sqlstate or ""
    if state == "23503" and diag.constraint_name == UNKNOWN_TRACK_CONSTRAINT:
        return StoreIntegrityError("refused", UNKNOWN_TRACK)
    if state in _POSTGRES_KINDS:
        return StoreIntegrityError(_POSTGRES_KINDS[state], message)
    if state == _POSTGRES_LOCK_NOT_AVAILABLE:
        return StoreBusyError(message)
    return StoreDatabaseError(message)


def to_psycopg(sql: str) -> str:
    """Domain SQL, written with `?`, as psycopg takes it.

    A `?` outside a single-quoted literal becomes `%s`. Every `%` is
    doubled, inside a literal too: psycopg reads `%` wherever it stands
    once parameters are passed, and undoubles `%%`, so the server receives
    each literal as written. A `?` inside a literal is left alone. Only
    single quotes are known (spec 112, Honest limitations).
    """
    out: list[str] = []
    quoted = False
    for character in sql:
        if character == "'":
            quoted = not quoted
            out.append(character)
        elif character == "%":
            out.append("%%")
        elif character == "?" and not quoted:
            out.append("%s")
        else:
            out.append(character)
    return "".join(out)


class PostgresRow(tuple[Any, ...]):
    """A Postgres row that reads as `sqlite3.Row` does (spec 112, Row).

    A tuple of the values, so it reads by position, iterates over its
    values and compares equal to the tuple of them; and by column name,
    with `keys()`, so `dict(row)` works. psycopg's `dict_row` was not
    chosen: `tuple(row)` would yield the column names (Options weighed).
    """

    _names: tuple[str, ...]

    def __new__(cls, values: Sequence[Any], names: tuple[str, ...]) -> PostgresRow:
        row = super().__new__(cls, values)
        row._names = names
        return row

    @overload
    def __getitem__(self, key: SupportsIndex, /) -> Any: ...

    @overload
    def __getitem__(self, key: slice, /) -> tuple[Any, ...]: ...

    @overload
    def __getitem__(self, key: str, /) -> Any: ...

    def __getitem__(self, key: SupportsIndex | slice | str, /) -> Any:
        if not isinstance(key, str):
            return super().__getitem__(key)
        # Exact first, then without case, as sqlite3.Row reads a name.
        if key in self._names:
            return super().__getitem__(self._names.index(key))
        folded = key.casefold()
        for index, name in enumerate(self._names):
            if name.casefold() == folded:
                return super().__getitem__(index)
        raise IndexError("No item with that key")

    def keys(self) -> list[str]:
        return list(self._names)


def _postgres_row_factory(cursor: Any) -> Callable[[Sequence[Any]], PostgresRow]:
    names = tuple(str(column.name) for column in cursor.description or ())

    def make(values: Sequence[Any]) -> PostgresRow:
        return PostgresRow(values, names)

    return make


Translate = Callable[[Exception], StoreDatabaseError | None]


def _raise_translated(error: Exception, translate: Translate) -> None:
    """Raise the store error for a driver error, chained to it; return when
    the error is not the driver's, so the caller re-raises it as it was."""
    mapped = translate(error)
    if mapped is not None:
        raise mapped from error


class _StoreCursor:
    """A driver cursor whose reads raise store errors (spec 112)."""

    def __init__(self, cursor: Any, translate: Translate) -> None:
        self._cursor = cursor
        self._translate = translate

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    @property
    def lastrowid(self) -> int | None:
        # psycopg has none; spec 126 moves the two inserts that read it to
        # RETURNING id.
        value = getattr(self._cursor, "lastrowid", None)
        return int(value) if value is not None else None

    def fetchone(self) -> Any:
        try:
            return self._cursor.fetchone()
        except Exception as error:
            _raise_translated(error, self._translate)
            raise

    def fetchall(self) -> list[Any]:
        try:
            return list(self._cursor.fetchall())
        except Exception as error:
            _raise_translated(error, self._translate)
            raise

    def __iter__(self) -> Iterator[Any]:
        rows = iter(self._cursor)
        while True:
            try:
                row = next(rows)
            except StopIteration:
                return
            except Exception as error:
                _raise_translated(error, self._translate)
                raise
            yield row


class _Backend(Protocol):
    """What `Connection` asks of a driver. Every call may raise the
    driver's errors; `Connection` translates them."""

    dialect: Dialect

    def run(self, sql: str, params: Sequence[object] | Mapping[str, object]) -> Any: ...

    def control(self, sql: str) -> None: ...

    def driver_in_transaction(self) -> bool: ...

    def begin(self, *, immediate: bool) -> None: ...

    def ensure_begun(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def translate(self, error: Exception) -> StoreDatabaseError | None: ...

    def get_busy_timeout_ms(self) -> int: ...

    def set_busy_timeout_ms(self, value: int) -> None: ...

    def close(self) -> None: ...


class _SqliteBackend:
    dialect: Dialect = "sqlite"

    def __init__(self, driver: sqlite3.Connection) -> None:
        self.driver = driver

    def run(self, sql: str, params: Sequence[object] | Mapping[str, object]) -> Any:
        return self.driver.execute(sql, params)

    def control(self, sql: str) -> None:
        self.driver.execute(sql)

    def driver_in_transaction(self) -> bool:
        return self.driver.in_transaction

    def begin(self, *, immediate: bool) -> None:
        # A deferred transaction begins where the sqlite3 module begins one,
        # before the first write, exactly as `with conn:` did (spec 076). An
        # explicit BEGIN here would hold a read snapshot from the block's
        # first read, and a write after another connection's commit would
        # then fail at once with SQLITE_BUSY_SNAPSHOT instead of waiting.
        if immediate:
            self.driver.execute("BEGIN IMMEDIATE")

    def ensure_begun(self) -> None:
        # A savepoint opened with no transaction begins one, and releasing it
        # would commit: the outer block would lose its atomicity.
        if not self.driver.in_transaction:
            self.driver.execute("BEGIN")

    def commit(self) -> None:
        self.driver.commit()

    def rollback(self) -> None:
        self.driver.rollback()

    def translate(self, error: Exception) -> StoreDatabaseError | None:
        return store_error_from_sqlite(error) if isinstance(error, sqlite3.Error) else None

    def get_busy_timeout_ms(self) -> int:
        return int(self.driver.execute("PRAGMA busy_timeout").fetchone()[0])

    def set_busy_timeout_ms(self, value: int) -> None:
        self.driver.execute(f"PRAGMA busy_timeout={value}")

    def close(self) -> None:
        self.driver.close()


class _PostgresBackend:
    dialect: Dialect = "postgres"

    def __init__(
        self,
        driver: psycopg.Connection[Any],
        claims: Mapping[str, object] | None,
        busy_timeout_ms: int,
    ) -> None:
        self.driver = driver
        self.claims = claims
        self.busy_timeout_ms = busy_timeout_ms

    def run(self, sql: str, params: Sequence[object] | Mapping[str, object]) -> Any:
        cursor = self.driver.cursor(row_factory=_postgres_row_factory)
        # Parameters are always passed, an empty sequence included, so
        # psycopg always reads the doubled `%` of to_psycopg as one.
        cursor.execute(to_psycopg(sql).encode(), params)
        return cursor

    def control(self, sql: str) -> None:
        self.driver.execute(sql.encode())

    def driver_in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self.driver.info.transaction_status != TransactionStatus.IDLE

    def begin(self, *, immediate: bool) -> None:
        # `immediate` locks nothing here until spec 126 adds the per-job row
        # lock. SET LOCAL ends with the transaction, so nothing carries over
        # to another client behind a transaction-mode pooler. 0 on Postgres
        # means no limit, so "do not wait" is sent as 1 ms.
        self.driver.execute(b"BEGIN")
        self.driver.execute(f"SET LOCAL lock_timeout = '{max(self.busy_timeout_ms, 1)}ms'".encode())
        if self.claims is not None:
            assume_identity(self.driver, self.claims)

    def ensure_begun(self) -> None:
        # The outermost transaction always begins with BEGIN.
        return

    def commit(self) -> None:
        self.driver.execute(b"COMMIT")

    def rollback(self) -> None:
        self.driver.execute(b"ROLLBACK")

    def translate(self, error: Exception) -> StoreDatabaseError | None:
        import psycopg

        return store_error_from_postgres(error) if isinstance(error, psycopg.Error) else None

    def get_busy_timeout_ms(self) -> int:
        return self.busy_timeout_ms

    def set_busy_timeout_ms(self, value: int) -> None:
        self.busy_timeout_ms = value

    def close(self) -> None:
        self.driver.close()


def owner_claims(owner: str) -> dict[str, str]:
    """The claims a connection opened for `owner` sets (spec 106, "The run's
    database session"). Raises StoreError unless `owner` is a UUID."""
    try:
        sub = str(uuid.UUID(owner))
    except (ValueError, TypeError, AttributeError):
        raise StoreError(NOT_AN_OWNER_ID) from None
    return {"sub": sub, "role": "authenticated", "aud": "authenticated"}


def assume_identity(driver: psycopg.Connection[Any], claims: Mapping[str, object]) -> None:
    """Act as the owner `claims` names, for the rest of the open transaction.

    The three steps spec 112 gives every outermost transaction of an owner's
    connection (Amendment (2026-10-11): split, owner and nesting, decision
    2): `SET LOCAL ROLE harrier_tenant`, the claims set local with
    `set_config(..., true)`, and a read-back of both. Spec 104's request
    transaction passes the request's verified claims to this same function.

    Raises StoreDatabaseError with spec 106's text when the read-back
    differs from what was set, as it does when the transaction was split or
    never opened and `SET LOCAL` did nothing, or when a step is refused. The
    caller rolls back, and runs no other statement.
    """
    import psycopg

    from harrier.tracker.schema import TENANT_ROLE

    sub = str(claims.get("sub", ""))
    try:
        driver.execute(f"SET LOCAL ROLE {TENANT_ROLE}".encode())
        driver.execute(
            b"SELECT set_config('request.jwt.claims', %s, true)", (json.dumps(dict(claims)),)
        )
        row = driver.execute(
            b"SELECT current_user::text, "
            b"current_setting('request.jwt.claims', true)::jsonb ->> 'sub'"
        ).fetchone()
    except psycopg.Error as error:
        raise StoreDatabaseError(IDENTITY_DID_NOT_HOLD) from error
    if row is None or tuple(row) != (TENANT_ROLE, sub):
        raise StoreDatabaseError(IDENTITY_DID_NOT_HOLD)


class Connection:
    """A store connection the domain owns (spec 112).

    Over SQLite each statement is the sqlite3 connection's own, and an error
    keeps its text; over Postgres the `?` placeholders are rewritten
    (`to_psycopg`) and each row reads as `sqlite3.Row` does. On both, a
    driver error reaches the caller as `StoreDatabaseError` or one of its
    subclasses, with the driver's error as `__cause__`.
    """

    _backend: _Backend
    _depth: int

    def __init__(self, driver: sqlite3.Connection) -> None:
        self._backend = _SqliteBackend(driver)
        self._depth = 0

    @classmethod
    def postgres(
        cls, driver: psycopg.Connection[Any], *, claims: Mapping[str, object] | None = None
    ) -> Connection:
        """A `Connection` over a psycopg connection in autocommit mode.

        With `claims`, every outermost transaction acts as their owner
        (`assume_identity`). Without, statements run as the login role,
        with no claims: `harrier_owner_id()` is null, and an insert into an
        owned table is refused.
        """
        from harrier.db import BUSY_TIMEOUT_MS

        conn = cls.__new__(cls)
        conn._backend = _PostgresBackend(driver, claims, BUSY_TIMEOUT_MS)
        conn._depth = 0
        return conn

    @property
    def dialect(self) -> Dialect:
        return self._backend.dialect

    @property
    def busy_timeout_ms(self) -> int:
        """How long a writer waits for another connection's lock. `0` means
        do not wait, on both stores. On SQLite it is `PRAGMA busy_timeout`;
        on Postgres the `lock_timeout` of every later transaction."""
        return self._guard(self._backend.get_busy_timeout_ms)

    @busy_timeout_ms.setter
    def busy_timeout_ms(self, value: int) -> None:
        if isinstance(value, bool) or value < 0:
            raise ValueError(f"busy_timeout_ms must be a whole number of 0 or more, not {value!r}")
        self._guard(lambda: self._backend.set_busy_timeout_ms(value))

    def _guard[T](self, call: Callable[[], T]) -> T:
        try:
            return call()
        except Exception as error:
            _raise_translated(error, self._backend.translate)
            raise

    def execute(self, sql: str, params: Sequence[object] | Mapping[str, object] = ()) -> Cursor:
        """Run one statement written with `?` placeholders.

        On Postgres a statement outside `transaction()` runs in a
        transaction of its own, which begins as every transaction does, so
        none runs without the lock timeout and the owner's identity.
        """
        if self._backend.dialect == "postgres" and self._depth == 0:
            with self.transaction():
                return self._run(sql, params)
        return self._run(sql, params)

    def _run(self, sql: str, params: Sequence[object] | Mapping[str, object]) -> Cursor:
        cursor = self._guard(lambda: self._backend.run(sql, params))
        return _StoreCursor(cursor, self._backend.translate)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[None, None, None]:
        """Commit on success and roll back on any exception.

        The outermost block is the transaction. On SQLite it is what `with
        conn:` was on the sqlite3 connection (spec 076), and `immediate`
        begins it with `BEGIN IMMEDIATE`, the write lock taken before
        anything is read (spec 079). On Postgres it begins with `SET LOCAL
        lock_timeout`, then, on an owner's connection, the owner's identity.

        A block opened inside another is a SAVEPOINT, on both stores: its
        work is kept when it exits normally and undone when it raises, and
        the exception goes on. An outer block that catches it goes on
        without the inner work, and on Postgres stays usable. Nothing
        commits before the outermost block exits.
        """
        if self._depth == 0:
            with self._outermost(immediate=immediate):
                yield
            return
        name = f"harrier_savepoint_{self._depth}"
        self._guard(self._backend.ensure_begun)
        self._guard(lambda: self._backend.control(f"SAVEPOINT {name}"))
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._backend.driver_in_transaction():
                self._quietly(f"ROLLBACK TO SAVEPOINT {name}")
                self._quietly(f"RELEASE SAVEPOINT {name}")
            raise
        self._depth -= 1
        self._guard(lambda: self._backend.control(f"RELEASE SAVEPOINT {name}"))

    @contextmanager
    def _outermost(self, *, immediate: bool) -> Generator[None, None, None]:
        # A SQLite connection the sqlite3 module already put in a transaction,
        # with a write made outside any block, is taken as it is: the block
        # commits it, as `with conn:` did.
        if not self._backend.driver_in_transaction():
            try:
                self._guard(lambda: self._backend.begin(immediate=immediate))
            except BaseException:
                self._quietly_roll_back()
                raise
        self._depth = 1
        try:
            yield
        except BaseException:
            self._depth = 0
            self._quietly_roll_back()
            raise
        self._depth = 0
        try:
            self._guard(self._backend.commit)
        except BaseException:
            self._quietly_roll_back()
            raise

    def _quietly(self, sql: str) -> None:
        """A statement that ends a failed block. It runs while another error
        is on its way out, which is the one the caller must see; a
        connection too broken to run it is lost with its transaction."""
        try:
            self._backend.control(sql)
        except Exception as error:
            if self._backend.translate(error) is None:
                raise

    def _quietly_roll_back(self) -> None:
        try:
            if self._backend.driver_in_transaction():
                self._backend.rollback()
        except Exception as error:
            if self._backend.translate(error) is None:
                raise

    @property
    def in_transaction(self) -> bool:
        """True inside `transaction()`, on both stores; on SQLite also after
        a write outside one, which the sqlite3 module began a transaction
        for."""
        return self._depth > 0 or self._backend.driver_in_transaction()

    def close(self) -> None:
        self._guard(self._backend.close)

    @property
    def sqlite_driver(self) -> sqlite3.Connection:
        """The sqlite3 connection underneath, for code whose subject is SQLite
        itself: pragmas, autocommit, statement tracing (spec 112, Scope 3).
        Domain code never needs it. Raises StoreError on Postgres."""
        if isinstance(self._backend, _SqliteBackend):
            return self._backend.driver
        raise StoreError(NOT_SQLITE)


def open_store(environ: Mapping[str, str] | None = None, *, owner: str | None = None) -> Connection:
    """Open the store `HARRIER_DATABASE_URL` names (spec 112).

    SQLite: `harrier.db.connect()`, unchanged; an owner is refused there.
    Postgres: `harrier.pgstore.open_postgres_store`, with spec 103's version
    check, which takes no host lease, asks the Docker engine nothing and
    writes nothing under `data/` (ADR-011 concerns the SQLite file). With
    `owner`, every transaction acts as that owner. Both refusals of an owner
    come before anything opens.
    """
    target = store_target(environ)
    if not target.is_postgres:
        if owner is not None:
            raise StoreError(OWNER_NEEDS_POSTGRES)
        from harrier.db import connect

        return connect()
    claims = owner_claims(owner) if owner is not None else None
    from harrier.pgstore import open_postgres_store

    return Connection.postgres(open_postgres_store(target.url), claims=claims)


@dataclass(frozen=True)
class StoreTarget:
    """Which store this process opens."""

    dialect: Literal["sqlite", "postgres"]
    # Out of the repr: the URL carries the password, and a repr is what an
    # assertion or a log line prints (post-merge privacy review of PR #207).
    url: str = field(default="", repr=False)

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgres"


@dataclass(frozen=True)
class UrlParts:
    """The parts of a URL that may be printed, and the passwords to scrub."""

    host: str
    port: str
    database: str
    # Every form: libpq quotes the URL as written, still percent-encoded,
    # when it refuses it, and the decoded form when it quotes a parameter.
    passwords: tuple[str, ...]
    # The user, database name and query values that are not printable, in
    # every form. The server quotes a database or user it does not know, so
    # these are masked in the driver's text as the passwords are.
    withheld: tuple[str, ...] = ()

    def describe(self) -> str:
        # A database name outside the printable shape is not printed: with a
        # raw `/` in the password, the rest of it and the real host were read
        # as the database name (post-merge review of PR #243).
        database = self.database if printable(self.database) else "(database name withheld)"
        return f"{self.host}:{self.port}/{database}"


def printable(value: str) -> bool:
    """Whether a user, database name or query value may be printed as is."""
    return _PRINTABLE.fullmatch(value) is not None


def store_target(environ: Mapping[str, str] | None = None) -> StoreTarget:
    """The store `HARRIER_DATABASE_URL` names. Raises StoreUrlError.

    Read from the process environment and, when it is not set there, from
    `.env` in the working directory, the CLI's rule (spec 011). The CLI loads
    `.env` and the API does not, so without this a URL set only in `.env`
    made the CLI refuse while the API served SQLite (review of PR #207).
    An exported value, even an empty one, wins over the file.
    """
    if environ is None:
        if URL_VARIABLE in os.environ:
            raw = os.environ[URL_VARIABLE]
        else:
            from harrier.envfile import read_env_file

            raw = read_env_file(Path(".env")).get(URL_VARIABLE, "")
    else:
        raw = environ.get(URL_VARIABLE, "")
    if not raw.strip():
        return StoreTarget("sqlite")
    # Only `postgresql://`, in lower case, and a host and port that parse:
    # anything else failed later as a traceback or an unrelated driver
    # message (post-merge data integrity review of PR #207). libpq refuses
    # the scheme in another case. It also accepts `postgres://`, which is
    # refused here on purpose: spec 103 names one scheme.
    refused = StoreUrlError(f"{URL_VARIABLE} must be empty or a postgresql:// URL")
    # Leading or trailing whitespace is not stripped: it would change the
    # configured value silently, a password's last character included. It
    # reaches the raw-character refusal below (CodeRabbit review of PR #285).
    if not raw.lstrip().startswith(f"{POSTGRES_SCHEME}://"):
        raise refused
    # Before the parse below, so a raw delimiter is refused with the text
    # that names the fix even where the parse would fail too.
    if _has_raw_space_or_control(raw) or not _delimiters_read_one_way(raw):
        raise StoreUrlError(DELIMITER_REFUSAL)
    try:
        url_parts(raw)
        parses = True
    except ValueError:
        parses = False
    # Raised after the except block, not inside it, so the ValueError is not
    # chained as `__context__`. Its text can quote the password: a raw `/` in
    # the password ends the authority, so the password before it is read as
    # the port (review of PR #218).
    if not parses or not _libpq_parses(raw):
        raise refused
    return StoreTarget("postgres", raw)


def _has_raw_space_or_control(url: str) -> bool:
    """Whether the URL holds a raw whitespace or Unicode category C character.

    `scrub_secrets` ends a URL's password at whitespace, so a password
    holding a raw space of any kind, such as a no-break space, a vertical
    tab or an ideographic space, was left whole in text it scrubbed, while
    this function accepted the URL. Category C (control, format, surrogate,
    private use and unassigned) is refused with it: none of it belongs raw
    in a URL. Every URL accepted here is then one whose password
    `scrub_secrets` masks whole (privacy review of PR #281, spec 128).
    """
    return any(
        character.isspace() or unicodedata.category(character).startswith("C") for character in url
    )


def _delimiters_read_one_way(url: str) -> bool:
    """Whether no raw delimiter in the URL can be read two ways.

    Three shapes are refused. urlsplit, which tells the scrub what the
    password is, reads a raw `#` as the start of a fragment and drops a raw
    tab, CR or LF; libpq does neither, so it took a password the scrub never
    saw whole, and quoted it (post-merge review of PR #231). libpq and
    urlsplit split the user from the host at different `@`s (review of PR
    #225). And a raw `@` after the authority: a raw `/` or `?` in the
    password ends the authority early, so the host's own `@` lands in the
    path or the query. Both parsers then read the start of the URL as the
    host, the rest of the password as the database name, and the scrub
    learns no password at all (post-merge review of PR #243).

    An `@` in the path is always refused. An `@` in the query is refused
    only when the authority holds none: after a user's `@`,
    `?application_name=a@b` is a valid URL both parsers read alike (post-merge
    review of PR #231), and a password holding an `@` and then a `?` reads
    exactly as that URL does (spec 103, Honest limitations).

    A written URL carries all of these percent-encoded. True when urlsplit
    cannot read the URL at all: the parse in `store_target` then refuses it
    with the plain malformed-URL text.
    """
    if any(character in url for character in _READ_DIFFERENTLY):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    if "@" in parts.path or ("@" in parts.query and "@" not in parts.netloc):
        return False
    return _one_reading_of_the_authority(url)


def _libpq_parse(url: str) -> dict[str, object] | None:
    """The URL as libpq reads it, or None when libpq cannot read it.

    None also when psycopg is missing: the command that needs it refuses
    on its own (`harrier.pgstore.require_driver`). Every failure is
    swallowed here, so no text of libpq's, which can quote the URL whole,
    leaves this function. psycopg is imported here, not at module import,
    because the local store never needs it.
    """
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        return None
    try:
        return dict(conninfo_to_dict(url))
    except Exception:
        # A psycopg error quoting the URL, or a decode or encode error for
        # a value that is not UTF-8.
        return None


def _libpq_parses(url: str) -> bool:
    """Whether libpq's own parser reads the URL, or psycopg is missing.

    libpq quotes the text of a URL it cannot parse: a query parameter
    without its `=` whole, an invalid escape in any parameter, and for an
    unclosed `[` host the whole URL. A value that is not UTF-8 once decoded
    raised a decode error with a traceback. Each reached the CLI (privacy
    review of PR #243). Refusing what libpq cannot parse closes all of
    them where they start.
    """
    try:
        import psycopg  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        return True
    return _libpq_parse(url) is not None


def _one_reading_of_the_authority(url: str) -> bool:
    """Whether libpq and urlsplit split the user from the host at one `@`.

    urlsplit splits at the last `@` before the first `/`, `?` or `#`; libpq
    at the first `@` before the first `/`. A raw `@` in the password makes
    them disagree: urlsplit reads the whole of `p@rest` as the password and
    scrubs that, while libpq takes `rest@host` as the host and quotes it
    when it cannot resolve it, carrying the tail of the password. An `@` in
    a query with no path before it, and no `@` before the host, does the
    same (review of PR #225).

    The two agree when they split at the same `@`, or when neither finds
    one. Counting every `@` before the first `/` refused a valid URL whose
    query, with no path before it, holds an `@` after the host's own
    (post-merge review of PR #231). Called after the `#`, tab, CR and LF
    refusal, so urlsplit's authority is the URL's own text.
    """
    rest = url.split("://", 1)[1]
    libpq_at = rest.split("/", 1)[0].find("@")
    return libpq_at == urlsplit(url).netloc.rfind("@")


def _is_secret(name: str) -> bool:
    """Whether a connection parameter carries a secret: `password`,
    `sslpassword`, `oauth_client_secret` and any later one named alike."""
    return "pass" in name or "secret" in name


def url_parts(url: str) -> UrlParts:
    parts = urlsplit(url)
    written = [parts.password or ""]
    # libpq also takes secrets from the query (`?password=`, `?sslpassword=`
    # and others), and quotes a parameter as written when it refuses it
    # (review of PR #218; privacy review of PR #243).
    for pair in parts.query.split("&"):
        name, _, value = pair.partition("=")
        if _is_secret(unquote(name)):
            written.append(value)
    decoded = [
        value for name, value in parse_qsl(parts.query, keep_blank_values=True) if _is_secret(name)
    ]
    # And the values as libpq itself reads them, when it can.
    parsed = _libpq_parse(url) or {}
    read = [str(value) for name, value in parsed.items() if _is_secret(name) and value]
    # The user, the database name and every query value, as written, decoded
    # and read by libpq, except the host and port, which are printed. One
    # that is not printable is withheld: the server quotes a database or
    # user it does not know, and either can hold a password's tail
    # (post-merge review of PR #243). A printable one is left alone, so a
    # value like `1` does not mask every digit of the driver's text.
    others = [parts.username or "", parts.path.lstrip("/")]
    others += [pair.partition("=")[2] for pair in parts.query.split("&")]
    others += [value for _, value in parse_qsl(parts.query, keep_blank_values=True)]
    others += [str(value) for name, value in parsed.items() if name not in _PRINTED]
    withheld = (*others, *map(unquote, others))
    return UrlParts(
        host=parts.hostname or "localhost",
        port=str(parts.port or 5432),
        database=parts.path.lstrip("/") or "postgres",
        passwords=tuple(
            form for form in (*written, *map(unquote, written), *decoded, *read) if form
        ),
        withheld=tuple(dict.fromkeys(form for form in withheld if form and not printable(form))),
    )
