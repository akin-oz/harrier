"""The store a process opens, and the Postgres store (spec 103, ADR-013).

`HARRIER_DATABASE_URL` chooses. Unset or empty, the local SQLite store opens
exactly as before (`harrier.db`). A `postgresql://` URL names the hosted
store. Anything else is refused rather than read as "no URL", because a typo
that fell back to SQLite would have the operator believe they were writing
to Postgres.

Opening a Postgres store never migrates it. Requests will run as a role
that cannot run DDL (ADR-013 decision 3), and a deploy migrates once rather
than every machine on its first request. `harrier store migrate` is the one
path that applies migrations.

A URL carries a password, in its authority or as a `password` query
parameter. A connection error is built from the host, port and database
name, never from the URL, and the driver's own message is scrubbed of the
password, in its written and decoded forms, before it is quoted. The scrub
learns the password from urlsplit, so `store_target` refuses a URL that
urlsplit and libpq would read differently. The driver's error is not
chained to the one raised, so it cannot be found as `__context__` either.
Errors from a migration's statements are the driver's own; `harrier store
migrate` prints only the server's primary message, which quotes SQL, not
the URL. Spec 035 is the record of a credential leaking through an
exception string.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

if TYPE_CHECKING:
    import psycopg

URL_VARIABLE = "HARRIER_DATABASE_URL"
POSTGRES_SCHEME = "postgresql"

# The key of the transaction-scoped advisory lock the migration runner takes
# before it re-reads the version: the Postgres counterpart of SQLite's
# BEGIN IMMEDIATE (spec 090). Any fixed bigint works; this one is the ASCII
# of "harrier" read as a number, so it is recognisable in pg_locks.
MIGRATION_LOCK_KEY = 0x68_61_72_72_69_65_72

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
class _Where:
    """The parts of a URL that may be printed."""

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
        _where(raw)
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
    on its own (`require_driver`). Every failure is swallowed here, so no
    text of libpq's, which can quote the URL whole, leaves this function.
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


def _where(url: str) -> _Where:
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
    return _Where(
        host=parts.hostname or "localhost",
        port=str(parts.port or 5432),
        database=parts.path.lstrip("/") or "postgres",
        passwords=tuple(
            form for form in (*written, *map(unquote, written), *decoded, *read) if form
        ),
    )


def _scrub(message: str, where: _Where) -> str:
    # Every occurrence of every form is found in the text as written, and
    # the union of those spans is replaced. Replacing the longest form first
    # broke a shorter one that overlapped it, and left its other part behind
    # (privacy review of PR #243).
    spans: list[tuple[int, int]] = []
    for form in set(where.passwords):
        start = message.find(form)
        while start >= 0:
            spans.append((start, start + len(form)))
            start = message.find(form, start + 1)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    kept: list[str] = []
    at = 0
    for start, end in merged:
        kept.append(message[at:start])
        kept.append("***")
        at = end
    kept.append(message[at:])
    return " ".join("".join(kept).split())


def require_driver() -> None:
    """Raise StoreDriverMissing unless psycopg can be imported."""
    try:
        import psycopg  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError:
        raise StoreDriverMissing(
            "the Postgres store needs psycopg. Install it with: uv sync --group postgres"
        ) from None


def postgres_connect(url: str) -> psycopg.Connection[tuple[object, ...]]:
    """Connect in autocommit mode; transactions are opened explicitly."""
    require_driver()
    import psycopg

    try:
        where = _where(url)
    except ValueError:
        where = None
    if where is None:
        # urlsplit could not read the port, and its error quotes the text it
        # read as one, which can be the password. store_target refuses such
        # a URL; this is for a caller that came another way.
        message = f"cannot connect to the Postgres store: {URL_VARIABLE} is malformed"
    else:
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=10)
        except psycopg.Error as error:
            message = (
                f"cannot connect to the Postgres store at {where.describe()}: "
                f"{_scrub(str(error), where)}"
            )
        except Exception as error:
            # A decode or encode error for a value that is not UTF-8, or
            # anything else the driver raises outside its own classes. Its
            # text gives positions in the URL, so only its class is named,
            # and the CLI, which catches StoreError, prints one line
            # (privacy review of PR #243).
            message = (
                f"cannot connect to the Postgres store at {where.describe()}: "
                f"{type(error).__name__}"
            )
    # Raised after the except block, not inside it. `from None` only hides
    # the driver's error from a printed traceback; it stays attached as
    # `__context__`, and its text may quote the password (post-merge review
    # of PR #207).
    raise StoreConnectionError(message)


def target_version() -> int:
    """The version this code builds a Postgres store to."""
    from harrier.tracker.schema import POSTGRES_MIGRATIONS

    return max(version for version, _ in POSTGRES_MIGRATIONS)


def postgres_version(conn: psycopg.Connection[tuple[object, ...]]) -> int:
    """The store's recorded version; 0 when it has no schema_version table."""
    row = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL").fetchone()
    if row is None or not row[0]:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(str(row[0])) if row is not None and row[0] is not None else 0


def apply_postgres_migrations(conn: psycopg.Connection[tuple[object, ...]]) -> tuple[int, int]:
    """Apply every pending Postgres migration, each whole or not at all.

    Returns the version before and after. Each migration runs in its own
    transaction, which takes the advisory lock and re-reads the version
    under it, so two runners started together apply each migration once
    and the second finds the first's work (spec 090's rule, spec 103).
    Postgres DDL is transactional, so a statement that fails rolls back the
    whole migration and leaves no version row.
    """
    import psycopg

    from harrier.tracker.schema import POSTGRES_MIGRATIONS, POSTGRES_VERSION_TABLE

    before = postgres_version(conn)
    known = target_version()
    if before > known:
        raise StoreVersionError(_ahead_message(before, known))
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        conn.execute(POSTGRES_VERSION_TABLE.encode())
    for version, statements in POSTGRES_MIGRATIONS:
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
            if version <= postgres_version(conn):
                continue
            for statement in statements:
                # As bytes: psycopg types a query as a literal string, and
                # these are built from the schema's constants. Sent without
                # parameters, so nothing in them is read as a placeholder.
                try:
                    conn.execute(statement.encode())
                except psycopg.errors.RaiseException as error:
                    # Only a migration's precheck raises during DDL, and its
                    # text is the refusal (spec 105). Raised inside the
                    # transaction, so the migration rolls back whole.
                    raise StoreMigrationRefused(
                        error.diag.message_primary or f"migration {version} was refused"
                    ) from None
            conn.execute("INSERT INTO schema_version (version) VALUES (%s)", (version,))
    return before, postgres_version(conn)


def migrate_postgres(url: str) -> tuple[int, int]:
    """`harrier store migrate` on Postgres: connect, migrate, close."""
    conn = postgres_connect(url)
    try:
        return apply_postgres_migrations(conn)
    finally:
        conn.close()


def _ahead_message(found: int, known: int) -> str:
    return (
        f"the store is at version {found}, newer than this code knows ({known}). "
        "Run a newer harrier."
    )


def check_postgres_version(found: int) -> None:
    """Refuse a store that is not at the version this code needs."""
    known = target_version()
    if found > known:
        raise StoreVersionError(_ahead_message(found, known))
    if found < known:
        raise StoreVersionError(
            f"the Postgres store is at version {found}; this code needs {known}. "
            "Run harrier store migrate."
        )


def open_postgres_store(url: str) -> psycopg.Connection[tuple[object, ...]]:
    """Open a Postgres store for use. Never migrates; refuses a wrong version."""
    conn = postgres_connect(url)
    try:
        check_postgres_version(postgres_version(conn))
    except BaseException:
        conn.close()
        raise
    return conn
