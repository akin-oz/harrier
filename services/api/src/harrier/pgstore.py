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

A URL carries a password. A connection error is built from the host, port
and database name, never from the URL, and the driver's own message is
scrubbed of the password, in both its encoded and decoded forms, before it
is quoted. Errors from a migration's statements are the driver's own; they
quote SQL, not the URL. Spec 035 is the record of a
credential leaking through an exception string.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

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
    # Both forms: libpq quotes the URL as written, still percent-encoded,
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
    # The scheme exactly as libpq accepts it, and a host and port that parse:
    # anything else failed later as a traceback or an unrelated driver
    # message (post-merge data integrity review of PR #207).
    refused = StoreUrlError(f"{URL_VARIABLE} must be empty or a postgresql:// URL")
    if not raw.startswith(f"{POSTGRES_SCHEME}://"):
        raise refused
    try:
        _where(raw)
    except ValueError:
        raise refused from None
    return StoreTarget("postgres", raw)


def _where(url: str) -> _Where:
    parts = urlsplit(url)
    return _Where(
        host=parts.hostname or "localhost",
        port=str(parts.port or 5432),
        database=parts.path.lstrip("/") or "postgres",
        passwords=tuple(
            form for form in (parts.password or "", unquote(parts.password or "")) if form
        ),
    )


def _scrub(message: str, where: _Where) -> str:
    # Longest first, so a decoded form inside an encoded one cannot leave a
    # fragment of the other behind.
    for password in sorted(set(where.passwords), key=len, reverse=True):
        message = message.replace(password, "***")
    return " ".join(message.split())


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

    where = _where(url)
    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=10)
    except psycopg.Error as error:
        raise StoreConnectionError(
            f"cannot connect to the Postgres store at {where.describe()}: "
            f"{_scrub(str(error), where)}"
        ) from None


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
