"""The Postgres store's driver code (spec 103, ADR-013).

Connecting, reading the schema version and running the migrations. Which
store a process opens, and the store errors, are `harrier.connection`'s
(spec 112): they belong to no one driver.

Opening a Postgres store never migrates it. Requests will run as a role
that cannot run DDL (ADR-013 decision 3), and a deploy migrates once rather
than every machine on its first request. `harrier store migrate` is the one
path that applies migrations.

A URL carries a password, in its authority or as a `password` query
parameter. A connection error is built from the host, port and database
name, never from the URL, and the driver's own message is scrubbed of the
password, in its written and decoded forms, before it is quoted. The
scrub learns the password from urlsplit, so `harrier.connection`'s
`store_target` refuses a URL that urlsplit and libpq would read
differently, or that libpq cannot parse. The driver's error is not
chained to the one raised, so it cannot be found as `__context__` either.
A statement that fails after connecting raises the driver's own error from
`apply_postgres_migrations` and `postgres_version`. `migrate_postgres` and
`read_postgres_version`, which `harrier store` calls, turn it into
`StoreServerError` carrying only the server's primary message, which quotes
SQL, not the URL, through the same scrub (`server_reason`), so the CLI
needs no driver import (spec 128). Spec 035 is the record of a credential
leaking through an exception string.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harrier.connection import (
    URL_VARIABLE,
    StoreConnectionError,
    StoreDriverMissing,
    StoreMigrationRefused,
    StoreServerError,
    StoreVersionError,
    UrlParts,
    url_parts,
)

if TYPE_CHECKING:
    import psycopg

# The key of the advisory locks the migration runner takes: a session-level
# one from before it reads the version until its last migration commits
# (spec 128), and a transaction-level one in each migration's transaction
# before it re-reads the version, the Postgres counterpart of SQLite's
# BEGIN IMMEDIATE (spec 090). Any fixed bigint works; this one is the ASCII
# of "harrier" read as a number, so it is recognisable in pg_locks.
MIGRATION_LOCK_KEY = 0x68_61_72_72_69_65_72


def _scrub(message: str, where: UrlParts) -> str:
    # Every occurrence of every form is found in the text as written, and
    # the union of those spans is replaced. Replacing the longest form first
    # broke a shorter one that overlapped it, and left its other part behind
    # (privacy review of PR #243).
    # The withheld user, database name and query values too: the server
    # quotes a database or user it does not know (post-merge review of PR
    # #243).
    spans: list[tuple[int, int]] = []
    for form in {*where.passwords, *where.withheld}:
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


def server_reason(error: psycopg.Error, url: str) -> str:
    """One line for a driver error raised after connecting, scrubbed.

    The server's primary message, which quotes SQL and can quote a database
    or role name, with every form `_scrub` knows masked. An error without
    one, such as a lost connection, is named by its class. Used by `harrier
    store status` and `harrier store migrate` (spec 103).
    """
    reason = error.diag.message_primary or type(error).__name__
    try:
        where = url_parts(url)
    except ValueError:
        # store_target refuses such a URL; a caller that came another way
        # gets the class alone, since the message cannot be scrubbed.
        return type(error).__name__
    return _scrub(reason, where)


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
        where = url_parts(url)
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

    Returns the version before and after. The runner takes a session-level
    advisory lock before it reads the version before, and holds it until
    its last migration commits, so a second runner started with it waits,
    then finds the first's work and reports the version it found under the
    lock (spec 128). Each migration still runs in its own transaction,
    which takes the transaction-level lock on the same key and re-reads the
    version under it (spec 090's rule, spec 103); a session holding an
    advisory lock is granted it again at either level. Postgres DDL is
    transactional, so a statement that fails rolls back the whole migration
    and leaves no version row.
    """
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
    try:
        return _apply_under_the_session_lock(conn)
    finally:
        # A connection the server ended has released the lock with its
        # session, and a statement on it would raise over the error that
        # ended it.
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))


def _apply_under_the_session_lock(
    conn: psycopg.Connection[tuple[object, ...]],
) -> tuple[int, int]:
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
    """`harrier store migrate` on Postgres: connect, migrate, close.

    A driver error after connecting becomes `StoreServerError` with
    `server_reason`'s one line (spec 128).
    """
    conn = postgres_connect(url)
    import psycopg

    try:
        try:
            return apply_postgres_migrations(conn)
        except psycopg.Error as error:
            message = server_reason(error, url)
    finally:
        conn.close()
    # Raised after the except block, as postgres_connect raises, so the
    # driver's error is not attached as `__context__`.
    raise StoreServerError(message)


def read_postgres_version(url: str) -> int:
    """`harrier store status` on Postgres: connect, read the version, close.

    A driver error after connecting becomes `StoreServerError` with
    `server_reason`'s one line, as for `migrate_postgres` (spec 128).
    """
    conn = postgres_connect(url)
    import psycopg

    try:
        try:
            return postgres_version(conn)
        except psycopg.Error as error:
            message = server_reason(error, url)
    finally:
        conn.close()
    raise StoreServerError(message)


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
