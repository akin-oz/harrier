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
import re
import sqlite3
import unicodedata
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
