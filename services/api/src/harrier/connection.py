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

import functools
import json
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, SupportsIndex, cast, overload
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


class StoreLocalOnlyError(StoreError):
    """Work that moves the old system onto the local tracker file was given
    a Postgres connection (spec 126). Raised before any statement runs."""


class StoreDatabaseError(StoreError):
    """A statement through a `Connection` failed (spec 112).

    The same class for the same failure on both stores, with the driver's
    error as `__cause__`. The message carries no row or bound value. On
    Postgres it is the server's primary message, never its `DETAIL`, which
    quotes the row, and a data exception's value is withheld from it; an
    error without one, such as a lost connection, is named by its class. On
    SQLite it is the library's text, a value it quotes withheld
    (`withhold_values`).
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
    """Another connection held a lock this one needed, and did not let it go
    within this connection's `busy_timeout_ms`. With `0`, the connection
    did not wait at all."""


class StoreConflictError(StoreDatabaseError):
    """Another connection's commit replaced what this transaction had read,
    so the store refused its write at once rather than wait (spec 112).

    SQLite's `SQLITE_BUSY_SNAPSHOT` and Postgres's serialization failure
    (`40001`). No lock was waited for, and waiting would not help: only
    running the whole transaction again can succeed.
    """


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

# The refusals of a transaction the seam cannot begin as asked (spec 112,
# post-merge review of PR #288). Neither touches the transaction it found.
WRITE_LOCK_NOT_HELD = (
    "the write lock was asked for inside a SQLite transaction this connection began without it"
)
POSTGRES_NOT_IDLE = "the Postgres connection is not idle, so no transaction can begin on it"
# A block whose transaction the store gave up on: a Postgres transaction a
# statement failed in, or a SQLite one the library rolled back whole (spec
# 112, amendment of 2026-10-13).
TRANSACTION_GIVEN_UP = "a statement failed inside this transaction, so the store kept none of it"
# The refusal of a statement that would begin, end or mark a transaction
# itself (spec 112, amendment of 2026-10-13: transaction control).
TRANSACTION_CONTROL = (
    "a statement may not control the transaction: transaction() begins, commits and rolls back"
)

# The refusals of a statement whose placeholders the seam cannot read the
# same way on both stores (spec 112, post-merge review of PR #288).
NAMED_PLACEHOLDER = "a statement names no placeholder: write ? and pass a sequence"
NUMBERED_PLACEHOLDER = "a statement numbers no placeholder: write ? alone"
JSONB_KEY_OPERATOR = (
    "a statement may not use ?| or ?& because ? is always a placeholder: "
    "write jsonb_exists_any or jsonb_exists_all"
)

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
_POSTGRES_SERIALIZATION_FAILURE = "40001"
# SQLSTATE class 22, data exception: the server quotes the value it could
# not take, a bound one included.
_POSTGRES_DATA_EXCEPTION = "22"
# SQLITE_BUSY_SNAPSHOT: (5 | (2 << 8)) in sqlite3.h.
_SQLITE_BUSY_SNAPSHOT: int = getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517)

# The message of an error whose every word was a value.
VALUE_WITHHELD = "the store refused a value"

# A value as each store quotes one: Postgres in double quotes, or as the
# hexadecimal bytes of a character it cannot read; SQLite with `%Q`, in
# single quotes.
_POSTGRES_QUOTED = re.compile(r'\s*"(?:[^"]|"")*"|\s*\b0x[0-9A-Fa-f]+\b')
_SQLITE_QUOTED = re.compile(r"\s*'(?:[^']|'')*'")


def withhold_values(message: str, quoted: re.Pattern[str]) -> str:
    """A store's message without the values it quotes (spec 112).

    What follows the first `: ` is dropped, since both stores end a
    message with the value there (`invalid input syntax for type integer:
    "..."`, `bad JSON path: '...'`), then every quoted span left
    (`value "..." is out of range for type integer`). The wording that
    names the type or the column stays.
    """
    head = message.split(": ", 1)[0]
    return quoted.sub("", head).strip().rstrip(":").strip() or VALUE_WITHHELD


def store_error_from_sqlite(error: sqlite3.Error) -> StoreDatabaseError:
    """The store error for a sqlite3 error.

    Its text is the library's own, but for a value it quotes: an integrity
    error's text names the schema, and is kept whole; any other quotes a
    value only through a function such as `json_extract`'s `bad JSON path`,
    and that value is withheld. An error the sqlite3 module raises itself
    carries no code from the library and quotes no value.
    """
    message = str(error)
    code: int | None = getattr(error, "sqlite_errorcode", None)
    if code is None:
        return StoreDatabaseError(message)
    if isinstance(error, sqlite3.IntegrityError) and code in _SQLITE_KINDS:
        return StoreIntegrityError(_SQLITE_KINDS[code], message)
    if isinstance(error, sqlite3.IntegrityError):
        return StoreDatabaseError(message)
    message = withhold_values(message, _SQLITE_QUOTED) if "'" in message else message
    if code == _SQLITE_BUSY_SNAPSHOT:
        return StoreConflictError(message)
    # The low byte is the primary code: SQLITE_BUSY and its other extended
    # forms, each a lock not taken within the busy timeout.
    if code & 0xFF == sqlite3.SQLITE_BUSY:
        return StoreBusyError(message)
    return StoreDatabaseError(message)


def store_error_from_postgres(error: psycopg.Error) -> StoreDatabaseError:
    """The store error for a psycopg error.

    Its text is the server's primary message: the `DETAIL` quotes the row's
    values, and `str()` of a psycopg error includes it. A data exception's
    primary message quotes the value it refused, a bound one included, so
    that value is withheld (`withhold_values`). Every other message quotes
    names only: a table, a column, a constraint, a type. An error the
    server did not send, such as a lost connection, is named by its class,
    since the client's text is not the server's and nothing promises what
    it quotes.
    """
    diag = error.diag
    message = diag.message_primary or type(error).__name__
    state = error.sqlstate or ""
    if state.startswith(_POSTGRES_DATA_EXCEPTION):
        message = withhold_values(message, _POSTGRES_QUOTED)
    if state == "23503" and diag.constraint_name == UNKNOWN_TRACK_CONSTRAINT:
        return StoreIntegrityError("refused", UNKNOWN_TRACK)
    if state in _POSTGRES_KINDS:
        return StoreIntegrityError(_POSTGRES_KINDS[state], message)
    if state == _POSTGRES_LOCK_NOT_AVAILABLE:
        return StoreBusyError(message)
    if state == _POSTGRES_SERIALIZATION_FAILURE:
        return StoreConflictError(message)
    return StoreDatabaseError(message)


def _psycopg_error_class() -> type[BaseException] | None:
    # From `sys.modules`, never imported here: a process that never loaded
    # psycopg holds none of its errors, and a log record must not import it.
    module = sys.modules.get("psycopg")
    found = getattr(module, "Error", None) if module is not None else None
    return found if isinstance(found, type) and issubclass(found, BaseException) else None


def is_driver_error(error: object) -> bool:
    """Whether `error` is the driver's own, psycopg's or sqlite3's, rather
    than the store error the seam raises from it (spec 135)."""
    if isinstance(error, sqlite3.Error):
        return True
    psycopg_error = _psycopg_error_class()
    return psycopg_error is not None and isinstance(error, psycopg_error)


def driver_error_text(error: BaseException) -> str:
    """A driver error's text as a log may print it (spec 135).

    The store error's text for it, which quotes no row value, after the
    SQLSTATE when the error has one: never `str(error)`, which on Postgres
    carries the server's `DETAIL` lines and with them the row. Empty when
    the error cannot be rendered, so it is named by its class alone; this
    is called on the logging path and never raises.
    """
    try:
        if isinstance(error, sqlite3.Error):
            return str(store_error_from_sqlite(error))
        psycopg_error = _psycopg_error_class()
        if psycopg_error is None or not isinstance(error, psycopg_error):
            return ""
        text = str(store_error_from_postgres(cast("psycopg.Error", error)))
        state = getattr(error, "sqlstate", None)
        return f"[{state}] {text}" if isinstance(state, str) and state else text
    except Exception:
        return ""


# A character that may continue a Postgres identifier, so a `$` or an `E`
# after one is part of a name, not the start of a quote.
_IDENTIFIER_CHARACTER = re.compile(r"[A-Za-z0-9_$\x80-\U0010ffff]")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*)?\$")


@dataclass(frozen=True)
class Statement:
    """A statement as the seam reads it: psycopg's text and its count of
    `?` placeholders."""

    psycopg: str
    placeholders: int


def _quoted_end(sql: str, start: int, quote: str, *, backslash: bool) -> int:
    """The index after the quote that closes the one at `start`, or the end
    of the text. A doubled quote is part of the quoted text, and with
    `backslash`, an E'' string's, so is any escaped character."""
    index = start + 1
    while index < len(sql):
        character = sql[index]
        if backslash and character == "\\":
            index += 2
            continue
        if character == quote:
            if sql.startswith(quote * 2, index):
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _block_comment_end(sql: str, start: int) -> int:
    """The index after the `*/` that closes the `/*` at `start`. Postgres
    nests block comments; SQLite does not, and a nested one is not
    portable SQL."""
    depth = 0
    index = start
    while index < len(sql):
        if sql.startswith("/*", index):
            depth += 1
            index += 2
        elif sql.startswith("*/", index):
            depth -= 1
            index += 2
            if depth == 0:
                return index
        else:
            index += 1
    return len(sql)


@functools.lru_cache(maxsize=1024)
def read_statement(sql: str) -> Statement:
    """Domain SQL, written with `?`, as psycopg takes it (spec 112).

    A `?` outside a literal, a quoted identifier, a dollar-quoted string
    and a comment becomes `%s`; inside one it is left alone. Literals are
    `'...'` with `''` inside, and `E'...'`, whose backslash escapes a
    quote too. Every `%` is doubled, wherever it stands: psycopg reads `%`
    anywhere once parameters are passed, and undoubles `%%`, so the server
    receives each literal and comment as written.

    Raises StoreDatabaseError, before anything runs on either store, for a
    shape the two stores would read differently: a named placeholder
    (`:name`), a numbered one (`?1`), and the jsonb operators `?|` and
    `?&`, which SQLite would read as a placeholder. The jsonb `?` operator
    alone cannot be told from a placeholder, and is always read as one.
    """
    out: list[str] = []
    count = 0
    index = 0
    while index < len(sql):
        character = sql[index]
        before = sql[index - 1] if index else ""
        end = index + 1
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            end = len(sql) if newline < 0 else newline + 1
        elif sql.startswith("/*", index):
            end = _block_comment_end(sql, index)
        elif character == "'":
            escaped = before in ("E", "e") and not _identifier_before(sql, index - 1)
            end = _quoted_end(sql, index, "'", backslash=escaped)
        elif character == '"':
            end = _quoted_end(sql, index, '"', backslash=False)
        elif character == "$" and not _identifier_before(sql, index):
            tag = _DOLLAR_TAG.match(sql, index)
            if tag is not None:
                close = sql.find(tag.group(0), tag.end())
                end = len(sql) if close < 0 else close + len(tag.group(0))
        elif character == ":":
            following = sql[index + 1 : index + 2]
            if following == ":":
                end = index + 2
            elif following.isalpha() or following == "_":
                raise StoreDatabaseError(NAMED_PLACEHOLDER)
        elif character == "?":
            following = sql[index + 1 : index + 2]
            if following in ("|", "&"):
                raise StoreDatabaseError(JSONB_KEY_OPERATOR)
            if following.isdigit():
                raise StoreDatabaseError(NUMBERED_PLACEHOLDER)
            out.append("%s")
            count += 1
            index = end
            continue
        out.append(sql[index:end].replace("%", "%%"))
        index = end
    return Statement("".join(out), count)


def _identifier_before(sql: str, index: int) -> bool:
    """Whether the character before `index` continues an identifier."""
    return index > 0 and _IDENTIFIER_CHARACTER.match(sql, index - 1) is not None


def to_psycopg(sql: str) -> str:
    """Domain SQL, written with `?`, as psycopg takes it (`read_statement`)."""
    return read_statement(sql).psycopg


def _parameters_match(sql: str, params: object) -> Statement:
    """The statement, once its parameters are known to fit it. Raises
    StoreDatabaseError for a mapping, which names its placeholders, and for
    a count of parameters other than the count of `?`. On Postgres the
    driver's own refusal of either is a client error, which the seam names
    only by its class (spec 112, post-merge review of PR #288)."""
    if isinstance(params, Mapping):
        raise StoreDatabaseError(NAMED_PLACEHOLDER)
    statement = read_statement(sql)
    supplied = len(cast(Sequence[object], params))
    if supplied != statement.placeholders:
        raise StoreDatabaseError(
            f"the statement has {statement.placeholders} placeholders "
            f"and {supplied} parameters were passed"
        )
    return statement


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

    def __getnewargs__(self) -> tuple[tuple[Any, ...], tuple[str, ...]]:
        # `copy`, `deepcopy` and `pickle` build the copy through `__new__`,
        # which needs the names as well as the values.
        return (tuple(self), self._names)


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

    def run(
        self, sql: str, statement: Statement, params: Sequence[object], *, in_block: bool
    ) -> Any: ...

    def control(self, sql: str) -> None: ...

    def driver_in_transaction(self) -> bool: ...

    def refuse_to_begin(self, *, immediate: bool) -> None: ...

    def begin(self, *, immediate: bool) -> None: ...

    def open_savepoint(self, name: str, *, immediate: bool) -> None: ...

    def forget_savepoint(self, name: str) -> bool: ...

    def given_up(self) -> bool: ...

    def refuse_given_up(self) -> None: ...

    def end(self) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def translate(self, error: Exception) -> StoreDatabaseError | None: ...

    def get_busy_timeout_ms(self) -> int: ...

    def set_busy_timeout_ms(self, value: int) -> None: ...

    def close(self) -> None: ...


def begins_a_transaction(sql: str) -> bool:
    """Whether the sqlite3 module begins a transaction before `sql`.

    Its own rule, in Python 3.12's `Modules/_sqlite/statement.c`: after
    whitespace and comments, the statement starts with INSERT, UPDATE,
    DELETE or REPLACE, in any case. Nothing else begins one, a write that
    starts with WITH or a CREATE included, so those run in autocommit as
    they did inside `with conn:` (spec 076). Pinned against the module by
    `services/api/tests/test_seam_on_both_stores.py::test_the_seam_begins_where_the_sqlite3_module_would`.
    """
    index = 0
    while index < len(sql):
        if sql[index] in " \t\f\n\r":
            index += 1
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline < 0:
                return False
            index = newline + 1
        elif sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            if close < 0:
                return False
            index = close + 2
        else:
            break
    head = sql[index : index + 7].lower()
    return head.startswith(("insert", "update", "delete", "replace"))


# The first words of a statement that controls the transaction, and the one
# pair (spec 112, amendment of 2026-10-13: transaction control).
_CONTROL_WORDS = frozenset(
    {"begin", "start", "commit", "end", "rollback", "abort", "savepoint", "release"}
)
_CONTROL_PAIR = ("prepare", "transaction")
# What may stand before a word: whitespace, the vertical tab included, and
# the `;` of an empty statement.
_BEFORE_A_WORD = " \t\n\r\f\v;"
_WORD = re.compile(r"[A-Za-z0-9_$]*")
_POSTGRES_LINE_END = re.compile(r"[\r\n]")
_READINGS: tuple[Dialect, ...] = ("sqlite", "postgres")


def _next_word_at(sql: str, index: int, reading: Dialect) -> int:
    """The index of the next word from `index`, as `reading` skips what
    stands before one, or the end of the text when a comment never closes.

    A `--` comment ends at a line feed; on the Postgres reading at a
    carriage return too, as Postgres's own scanner ends one. A block
    comment ends at the first `*/` on the SQLite reading, and nests on the
    Postgres one (`_block_comment_end`).
    """
    while index < len(sql):
        if sql[index] in _BEFORE_A_WORD:
            index += 1
        elif sql.startswith("--", index):
            if reading == "postgres":
                line_end = _POSTGRES_LINE_END.search(sql, index)
                index = len(sql) if line_end is None else line_end.end()
            else:
                newline = sql.find("\n", index)
                index = len(sql) if newline < 0 else newline + 1
        elif sql.startswith("/*", index):
            if reading == "postgres":
                index = _block_comment_end(sql, index)
            else:
                close = sql.find("*/", index + 2)
                index = len(sql) if close < 0 else close + 2
        else:
            return index
    return len(sql)


def _first_words(sql: str, reading: Dialect) -> tuple[str, ...]:
    """The first two words of `sql` as `reading` finds them, in lower case.
    A word is a run of ASCII letters, digits, `_` and `$`."""
    words: list[str] = []
    index = 0
    while len(words) < 2:
        found = _WORD.match(sql, _next_word_at(sql, index, reading))
        if found is None or not found.group(0):
            break
        words.append(found.group(0).lower())
        index = found.end()
    return tuple(words)


def controls_the_transaction(sql: str) -> bool:
    """Whether `sql` begins, ends or marks a transaction itself (spec 112,
    amendment of 2026-10-13: transaction control).

    True when its first word is BEGIN, START, COMMIT, END, ROLLBACK, ABORT,
    SAVEPOINT or RELEASE, or its first two are PREPARE TRANSACTION, on
    either reading of what stands before them: SQLite's or Postgres's. Only
    the first word is read, so a control word inside a literal, a quoted
    name, a comment or a body after it is not. `begins_a_transaction`
    reads the same way for another question, and is left as the sqlite3
    module's rule. Pinned on both stores by
    `services/api/tests/test_seam_on_both_stores.py::test_the_seam_reads_a_first_word_as_either_store_would`.
    """
    for reading in _READINGS:
        words = _first_words(sql, reading)
        if words[:1] and words[0] in _CONTROL_WORDS:
            return True
        if words == _CONTROL_PAIR:
            return True
    return False


class _SqliteBackend:
    """SQLite under the seam's transactions (spec 112, post-merge review of
    PR #288).

    A block begins no transaction until it must. Its reads run in
    autocommit, as they did inside `with conn:` (spec 076), so no block
    holds a read snapshot that a later write would find stale. The
    transaction begins with `BEGIN IMMEDIATE`, write lock and all, at the
    first of two moments: a statement the sqlite3 module would begin a
    transaction for (`begins_a_transaction`), or a block, at any depth,
    that asks for `immediate`. The savepoints of the blocks open by then
    are opened in that same moment, in order, so a savepoint never begins
    the transaction and its release never commits.

    So every transaction a block begins holds the write lock from its first
    statement, and a block that asks for it inside one already has it.
    """

    dialect: Dialect = "sqlite"

    def __init__(self, driver: sqlite3.Connection) -> None:
        self.driver = driver
        # The savepoints of open blocks that have not begun yet, outermost
        # first.
        self.pending: list[str] = []
        # Whether the open transaction holds the write lock: true from a
        # BEGIN IMMEDIATE, and after a write outside any block that the
        # module began a transaction for and that took the lock.
        self.write_locked = False
        # Whether the outermost block holds a transaction: one it began, or
        # one it adopted. False outside any block, so nothing after the block
        # ends reads the driver on its account.
        self.held = False
        # Whether a statement failed and took that transaction with it.
        # Only a failure counts: a COMMIT the caller ran also leaves the
        # driver outside a transaction, and its work was kept.
        self.lost = False

    def run(
        self, sql: str, statement: Statement, params: Sequence[object], *, in_block: bool
    ) -> Any:
        if self.driver.in_transaction or not begins_a_transaction(sql):
            return self.driver.execute(sql, params)
        if in_block:
            self._begin_writing()
            return self.driver.execute(sql, params)
        # Outside any block the module begins the transaction, as it did
        # before the seam. A write that ran, or that a constraint refused,
        # took the write lock; one refused for want of the lock did not.
        try:
            cursor = self.driver.execute(sql, params)
        except sqlite3.IntegrityError:
            self.write_locked = self.driver.in_transaction
            raise
        except sqlite3.Error:
            self.write_locked = False
            raise
        self.write_locked = self.driver.in_transaction
        return cursor

    def _begin_writing(self) -> None:
        self.driver.execute("BEGIN IMMEDIATE")
        self.write_locked = True
        self.held = True
        for name in self.pending:
            self.driver.execute(f"SAVEPOINT {name}")
        self.pending.clear()

    def control(self, sql: str) -> None:
        self.driver.execute(sql)

    def driver_in_transaction(self) -> bool:
        return self.driver.in_transaction

    def _refuse_without_the_lock(self, *, immediate: bool) -> None:
        # A transaction this connection began without the write lock: a
        # BEGIN its caller ran, or a write outside any block that was
        # refused the lock. It may hold a snapshot another commit has made
        # stale, and nothing can take the lock inside it safely.
        if immediate and self.driver.in_transaction and not self.write_locked:
            raise StoreDatabaseError(WRITE_LOCK_NOT_HELD)

    def refuse_to_begin(self, *, immediate: bool) -> None:
        self._refuse_without_the_lock(immediate=immediate)

    def begin(self, *, immediate: bool) -> None:
        self.pending = []
        self.held = self.driver.in_transaction
        self.lost = False
        # A transaction the module began for a write outside any block is
        # taken as it is: the block commits it, as `with conn:` did.
        if self.driver.in_transaction:
            return
        self.write_locked = False
        if immediate:
            self._begin_writing()

    def open_savepoint(self, name: str, *, immediate: bool) -> None:
        self._refuse_without_the_lock(immediate=immediate)
        if self.driver.in_transaction:
            self.driver.execute(f"SAVEPOINT {name}")
            return
        self.pending.append(name)
        if immediate:
            self._begin_writing()

    def forget_savepoint(self, name: str) -> bool:
        # A block that never began has nothing to release or undo: its reads
        # ran in autocommit.
        if name in self.pending:
            self.pending.remove(name)
            return True
        return False

    def given_up(self) -> bool:
        # A full disk, an I/O error, out of memory and some busy cases roll
        # the whole transaction back, not only the statement. The next write
        # would begin a new one, which the block's exit would commit.
        return self.lost

    def refuse_given_up(self) -> None:
        if self.given_up():
            raise StoreDatabaseError(TRANSACTION_GIVEN_UP)

    def end(self) -> None:
        self.pending = []
        self.held = False
        if not self.driver.in_transaction:
            self.write_locked = False

    def commit(self) -> None:
        self.driver.commit()

    def rollback(self) -> None:
        self.driver.rollback()

    def translate(self, error: Exception) -> StoreDatabaseError | None:
        # Every failed statement and cursor read passes here, whatever it
        # raised: SQLite's out of memory reaches Python as MemoryError, not
        # as a sqlite3.Error, and rolls the transaction back all the same.
        if self.held and not self.driver.in_transaction:
            self.lost = True
        if not isinstance(error, sqlite3.Error):
            return None
        return store_error_from_sqlite(error)

    def get_busy_timeout_ms(self) -> int:
        return int(self.driver.execute("PRAGMA busy_timeout").fetchone()[0])

    def set_busy_timeout_ms(self, value: int) -> None:
        self.driver.execute(f"PRAGMA busy_timeout={value}")

    def close(self) -> None:
        self.driver.close()


@functools.cache
def _extended_cursor() -> type[psycopg.Cursor[Any]]:
    """A psycopg cursor that sends every statement by the extended query
    protocol (spec 112, amendment of 2026-10-13: transaction control,
    decision 4).

    psycopg sends a statement whose parameters are an empty sequence by the
    simple protocol, which runs every statement in the text: `SELECT 1;
    COMMIT` committed through the seam (psycopg 3.3.6, `_execute_send` in
    `_cursor_base.py`). By the extended protocol the server refuses a
    second statement before it runs any. psycopg offers no public switch
    for it. Pipeline mode sends the extended protocol too, but after it the
    server's idle in transaction timeout no longer fired, which spec 134's
    request transaction relies on. So this overrides the method that
    chooses, and asks for the extended protocol always. A psycopg that
    renames it fails
    `services/api/tests/test_seam_on_both_stores.py::test_a_second_statement_in_one_call_runs_nothing`.

    Built on first use: the local store never imports psycopg.
    """
    import psycopg

    class ExtendedCursor(psycopg.Cursor[Any]):
        def _execute_send(
            self, query: Any, *, force_extended: bool = False, binary: bool | None = None
        ) -> None:
            super()._execute_send(query, force_extended=True, binary=binary)

    return ExtendedCursor


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

    def run(
        self, sql: str, statement: Statement, params: Sequence[object], *, in_block: bool
    ) -> Any:
        cursor = _extended_cursor()(self.driver, row_factory=_postgres_row_factory)
        # Parameters are always passed, an empty sequence included, so
        # psycopg always reads the doubled `%` of read_statement as one.
        cursor.execute(statement.psycopg.encode(), params)
        return cursor

    def control(self, sql: str) -> None:
        self.driver.execute(sql.encode())

    def driver_in_transaction(self) -> bool:
        from psycopg.pq import TransactionStatus

        return self.driver.info.transaction_status != TransactionStatus.IDLE

    def refuse_to_begin(self, *, immediate: bool) -> None:
        # A driver already in a transaction, one its caller or a pooler
        # began, or one a failed ROLLBACK left open, is never adopted: its
        # statements would run as the login role, with no claims and no
        # lock timeout, and the block would commit them. Nor is it rolled
        # back, since its work is not the seam's (post-merge review of PR
        # #288).
        if self.driver_in_transaction():
            raise StoreDatabaseError(POSTGRES_NOT_IDLE)

    def begin(self, *, immediate: bool) -> None:
        # `immediate` locks nothing here: the domain's write lock locks the
        # job rows a write depends on instead (`harrier.tracker.store`,
        # spec 126). SET LOCAL ends with the transaction, so nothing carries over
        # to another client behind a transaction-mode pooler. 0 on Postgres
        # means no limit, so "do not wait" is sent as 1 ms.
        self.driver.execute(b"BEGIN")
        self.driver.execute(f"SET LOCAL lock_timeout = '{max(self.busy_timeout_ms, 1)}ms'".encode())
        if self.claims is not None:
            assume_identity(self.driver, self.claims)

    def open_savepoint(self, name: str, *, immediate: bool) -> None:
        self.driver.execute(f"SAVEPOINT {name}".encode())

    def forget_savepoint(self, name: str) -> bool:
        # The outermost transaction always begins with BEGIN, so every
        # savepoint is opened with its block.
        return False

    def given_up(self) -> bool:
        # A statement failed and no rollback to a savepoint has ended the
        # failed state. COMMIT here answers ROLLBACK with no error.
        from psycopg.pq import TransactionStatus

        return self.driver.info.transaction_status == TransactionStatus.INERROR

    def refuse_given_up(self) -> None:
        # The server refuses every later statement itself, with its own text.
        return

    def end(self) -> None:
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


IdentityPart = Literal["role", "claims", "owner", "read-back"]


class IdentityDidNotHold(StoreDatabaseError):
    """An owner transaction's identity did not hold (spec 134).

    `differed` names what did not hold: `role`, `claims` or `owner` for a
    value the read-back found different, or the step that was refused
    (`role`, `claims`, or `read-back`). The message is spec 106's text and
    those names, and carries no value the read-back found or set.
    """

    def __init__(self, differed: tuple[IdentityPart, ...]) -> None:
        super().__init__(f"{IDENTITY_DID_NOT_HOLD} ({', '.join(differed)})")
        self.differed = differed


def _owner_form(sub: str) -> str:
    """`sub` as `harrier_owner_id()::text` prints it, when it is a UUID."""
    try:
        return str(uuid.UUID(sub))
    except ValueError:
        return sub


def assume_identity(driver: psycopg.Connection[Any], claims: Mapping[str, object]) -> None:
    """Act as the owner `claims` names, for the rest of the open transaction.

    The three steps spec 112 gives every outermost transaction of an owner's
    connection (Amendment (2026-10-11): split, owner and nesting, decision
    2): `SET LOCAL ROLE harrier_tenant`, the claims set local with
    `set_config(..., true)`, and a read-back. Spec 134's request
    transaction passes the request's verified claims to this same function,
    so requests and runs (spec 106) are checked alike.

    The read-back is one statement: `current_user`, the `sub` of the claims
    setting, and `public.harrier_owner_id()`, the owner the row policy and
    the `owner_id` defaults read (spec 105, migration 16). It goes on only
    when the role is `harrier_tenant` and both equal the `sub` it set (spec
    134, "The read-back, in every owner transaction").

    Raises IdentityDidNotHold, a StoreDatabaseError with spec 106's text,
    naming what differed: as when the transaction was split or never opened
    and `SET LOCAL` did nothing, when the function returns another owner or
    none, or when a step is refused, such as the read-back on a store where
    the tenant lost `EXECUTE` on the function. The caller rolls back, and
    runs no other statement.
    """
    import psycopg

    from harrier.tracker.schema import TENANT_ROLE

    sub = str(claims.get("sub", ""))
    step: IdentityPart = "role"
    try:
        driver.execute(f"SET LOCAL ROLE {TENANT_ROLE}".encode())
        step = "claims"
        driver.execute(
            b"SELECT set_config('request.jwt.claims', %s, true)", (json.dumps(dict(claims)),)
        )
        step = "read-back"
        row = driver.execute(
            b"SELECT current_user::text, "
            b"nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub', "
            b"public.harrier_owner_id()::text"
        ).fetchone()
    except psycopg.Error as error:
        raise IdentityDidNotHold((step,)) from error
    if row is None:
        raise IdentityDidNotHold(("read-back",))
    role, claimed, owner = tuple(row)
    differed: list[IdentityPart] = []
    if role != TENANT_ROLE:
        differed.append("role")
    if claimed != sub:
        differed.append("claims")
    if owner is None or owner != _owner_form(sub):
        differed.append("owner")
    if differed:
        raise IdentityDidNotHold(tuple(differed))


class Connection:
    """A store connection the domain owns (spec 112).

    Over SQLite each statement is the sqlite3 connection's own, and an error
    keeps its text, a value it quotes withheld; over Postgres the `?`
    placeholders are rewritten (`read_statement`) and each row reads as
    `sqlite3.Row` does. On both, a
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

    def execute(self, sql: str, params: Sequence[object] = ()) -> Cursor:
        """Run one statement written with `?` placeholders.

        Raises StoreDatabaseError, on both stores and before anything runs,
        for a statement whose placeholders the two stores would read
        differently (`read_statement`), for parameters passed as a mapping,
        and for a count of parameters other than the count of `?`.

        Raises StoreDatabaseError (`TRANSACTION_CONTROL`) for a statement
        that controls the transaction (`controls_the_transaction`), on both
        stores, inside a block at any depth and outside one. It is checked
        first: before the placeholders, before the refusal in a block the
        store gave up on, and on Postgres before the statement's own transaction begins. No
        driver sees it, so the store has given nothing up, and a block that
        catches it goes on.

        On Postgres a statement outside `transaction()` runs in a
        transaction of its own, which begins as every transaction does, so
        none runs without the lock timeout and the owner's identity. A text
        holding a second statement is refused by the server before it runs
        any; on SQLite by the sqlite3 module.
        """
        if controls_the_transaction(sql):
            raise StoreDatabaseError(TRANSACTION_CONTROL)
        statement = _parameters_match(sql, params)
        if self._backend.dialect == "postgres" and self._depth == 0:
            with self.transaction():
                return self._run(sql, statement, params)
        return self._run(sql, statement, params)

    def _run(self, sql: str, statement: Statement, params: Sequence[object]) -> Cursor:
        in_block = self._depth > 0
        if in_block:
            self._backend.refuse_given_up()
        cursor = self._guard(lambda: self._backend.run(sql, statement, params, in_block=in_block))
        return _StoreCursor(cursor, self._backend.translate)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Generator[None, None, None]:
        """Commit on success and roll back on any exception.

        The outermost block is the transaction. On SQLite it is what `with
        conn:` was on the sqlite3 connection (spec 076): its reads run in
        autocommit, and the transaction begins with `BEGIN IMMEDIATE` at
        its first write. A block that asks for `immediate`, at any depth,
        begins it then instead, so the write lock is held before anything
        it reads (spec 079), until the outermost block ends. On Postgres it
        begins with `SET LOCAL lock_timeout`, then, on an owner's
        connection, the owner's identity.

        A block opened inside another is a SAVEPOINT, on both stores: its
        work is kept when it exits normally and undone when it raises, and
        the exception goes on. An outer block that catches it goes on
        without the inner work, and on Postgres stays usable. Nothing
        commits before the outermost block exits.

        When the store has given up on the transaction (a statement failed
        in it on Postgres, or SQLite rolled it back whole) and the block
        exits normally, nothing commits: the outermost block rolls back and
        raises StoreDatabaseError (`TRANSACTION_GIVEN_UP`). On SQLite every
        later statement in the block is refused with that text; on
        Postgres the server refuses them itself.

        Raises StoreDatabaseError, and touches nothing, when the transaction
        cannot begin as asked: on Postgres when the driver is already in a
        transaction the seam did not begin (`POSTGRES_NOT_IDLE`); on SQLite
        when `immediate` is asked inside a transaction begun without the
        write lock (`WRITE_LOCK_NOT_HELD`).
        """
        if self._depth == 0:
            with self._outermost(immediate=immediate):
                yield
            return
        name = f"harrier_savepoint_{self._depth}"
        self._backend.refuse_given_up()
        self._guard(lambda: self._backend.open_savepoint(name, immediate=immediate))
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if not self._backend.forget_savepoint(name) and self._backend.driver_in_transaction():
                self._quietly(f"ROLLBACK TO SAVEPOINT {name}")
                self._quietly(f"RELEASE SAVEPOINT {name}")
            raise
        self._depth -= 1
        self._backend.refuse_given_up()
        if not self._backend.forget_savepoint(name):
            self._guard(lambda: self._backend.control(f"RELEASE SAVEPOINT {name}"))

    @contextmanager
    def _outermost(self, *, immediate: bool) -> Generator[None, None, None]:
        # Before the try: a transaction the seam may not begin on is left as
        # it was found, never rolled back.
        self._guard(lambda: self._backend.refuse_to_begin(immediate=immediate))
        try:
            self._guard(lambda: self._backend.begin(immediate=immediate))
        except BaseException:
            self._quietly_roll_back()
            raise
        self._depth = 1
        try:
            yield
            given_up = self._guard(self._backend.given_up)
        except BaseException:
            self._depth = 0
            self._quietly_roll_back()
            raise
        finally:
            self._backend.end()
        self._depth = 0
        if given_up:
            # COMMIT would report success: Postgres answers it with ROLLBACK,
            # and SQLite has no transaction left to commit.
            self._quietly_roll_back()
            raise StoreDatabaseError(TRANSACTION_GIVEN_UP)
        try:
            self._guard(self._backend.commit)
        except BaseException:
            self._quietly_roll_back()
            raise
        finally:
            self._backend.end()

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


# --- portable domain SQL (spec 126) ---
#
# The fragments one statement text needs to mean the same on both stores.
# Domain SQL writes each statement once and asks these for the part the
# dialects spell differently, so no domain module reads the dialect, but
# for the write lock in `harrier.tracker.store`.

_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"


def store_now() -> str:
    """The current UTC time as `YYYY-MM-DD HH:MM:SS`, the text SQLite's own
    clock wrote before spec 126. Every timestamp the domain writes is this,
    bound as a parameter; a column's default stays the database's clock in
    the same format (spec 103)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def ascii_folded(conn: Connection, expression: str) -> str:
    """`expression` compared with `A` to `Z` folded to `a` to `z`, and no
    other character folded, on both stores (spec 126). SQLite's `NOCASE`
    collation folds exactly those; Postgres has no such collation, and its
    `lower()` follows the database's locale, so it folds `É` too."""
    if conn.dialect == "postgres":
        return f"translate({expression}, '{_ASCII_UPPER}', '{_ASCII_LOWER}')"
    return f"{expression} COLLATE NOCASE"


def by_code_point(conn: Connection, expression: str) -> str:
    """`expression` ordered by code point on both stores (spec 126). SQLite
    orders text by its bytes, which for UTF-8 is code point order; Postgres
    by the database's collation unless told `C`."""
    if conn.dialect == "postgres":
        return f'{expression} COLLATE "C"'
    return expression


def refuse_unless_local(conn: Connection, refusal: str) -> None:
    """Raise StoreLocalOnlyError with `refusal` unless `conn` is the local
    SQLite store. Called first, so no statement has run (spec 126)."""
    if conn.dialect != "sqlite":
        raise StoreLocalOnlyError(refusal)


def update_or_insert(
    conn: Connection,
    update: str,
    update_params: Sequence[object],
    insert: str,
    insert_params: Sequence[object],
) -> None:
    """Write one keyed row the same way on both stores (spec 126, Upserts).

    In one `transaction()`: run `update`; when it changed no row, run
    `insert` in a nested `transaction()`, a savepoint; when that insert is
    refused by a unique key (another writer inserted the key first), the
    savepoint rolls back and `update` runs once more. Raises the integrity
    error when the second update changes no row either.

    `update`'s `WHERE` names the key and nothing else, and neither text
    names a tenant: on Postgres the update sees only the owner's rows and
    the insert meets the owner's own unique index (ADR-013 decision 2).
    The savepoint is what lets the retry run on Postgres, where an error
    aborts the rest of a transaction unless it rolled back to one.
    """
    with conn.transaction():
        if conn.execute(update, update_params).rowcount:
            return
        try:
            with conn.transaction():
                conn.execute(insert, insert_params)
        except StoreIntegrityError as error:
            if error.kind != "unique" or not conn.execute(update, update_params).rowcount:
                raise


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
