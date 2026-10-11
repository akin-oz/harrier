"""The connection the domain speaks through, and the store a process opens.

Every domain function takes a `Connection`, harrier's own type, never a
driver's (spec 112). It wraps a SQLite connection today; the Postgres store
comes behind the same type in spec 112's second pull request.

A `with` block over the connection itself is not offered. On a sqlite3
connection it commits or rolls back and leaves the connection open (spec
076); on a psycopg connection the same block closes it. `Connection` has no
`__enter__`, so pyright refuses one, and a transaction is written
`with conn.transaction():`.

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

import os
import sqlite3
from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, unquote, urlsplit

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


class StoreMigrationRefused(StoreError):
    """A migration's own precheck refused this store (spec 105).

    The message is the text the migration raises, which names no URL.
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

    @property
    def lastrowid(self) -> int | None: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def __iter__(self) -> Iterator[Any]: ...


class Connection:
    """A store connection the domain owns (spec 112).

    Over SQLite each method is the sqlite3 connection's own, so a statement,
    a commit and an error behave exactly as they did when the domain held
    the sqlite3 connection itself.
    """

    def __init__(self, driver: sqlite3.Connection) -> None:
        self._driver = driver

    def execute(self, sql: str, params: Sequence[object] | Mapping[str, object] = ()) -> Cursor:
        return self._driver.execute(sql, params)

    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        """Commit on success and roll back on any exception.

        Over SQLite this is the `with` block over the sqlite3 connection that
        the domain wrote before (spec 076): the connection stays open either
        way.
        """
        with self._driver:
            yield

    @property
    def in_transaction(self) -> bool:
        return self._driver.in_transaction

    def commit(self) -> None:
        self._driver.commit()

    def rollback(self) -> None:
        self._driver.rollback()

    def close(self) -> None:
        self._driver.close()

    @property
    def sqlite_driver(self) -> sqlite3.Connection:
        """The sqlite3 connection underneath, for code whose subject is SQLite
        itself: pragmas, autocommit, statement tracing (spec 112, Scope 3).
        Domain code never needs it."""
        return self._driver


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

    def describe(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"


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
    raw = raw.strip()
    if not raw:
        return StoreTarget("sqlite")
    # Only `postgresql://`, in lower case, and a host and port that parse:
    # anything else failed later as a traceback or an unrelated driver
    # message (post-merge data integrity review of PR #207). libpq refuses
    # the scheme in another case. It also accepts `postgres://`, which is
    # refused here on purpose: spec 103 names one scheme.
    refused = StoreUrlError(f"{URL_VARIABLE} must be empty or a postgresql:// URL")
    if not raw.startswith(f"{POSTGRES_SCHEME}://"):
        raise refused
    # urlsplit, which tells the scrub what the password is, reads a raw `#`
    # as the start of a fragment and drops a raw tab, CR or LF. libpq does
    # neither, so it took a password the scrub never saw whole, and quoted
    # it (post-merge review of PR #231). A written URL carries these
    # percent-encoded.
    if any(character in raw for character in _READ_DIFFERENTLY):
        raise refused
    try:
        url_parts(raw)
        parses = True
    except ValueError:
        parses = False
    # Raised after the except block, not inside it, so the ValueError is not
    # chained as `__context__`. Its text can quote the password: a raw `/` in
    # the password ends the authority, so the password before it is read as
    # the port (review of PR #218).
    if not parses or not _one_reading_of_the_authority(raw) or not _libpq_parses(raw):
        raise refused
    return StoreTarget("postgres", raw)


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
    return UrlParts(
        host=parts.hostname or "localhost",
        port=str(parts.port or 5432),
        database=parts.path.lstrip("/") or "postgres",
        passwords=tuple(
            form for form in (*written, *map(unquote, written), *decoded, *read) if form
        ),
    )
