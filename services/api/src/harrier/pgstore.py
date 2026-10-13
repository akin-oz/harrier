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

from contextlib import suppress
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
    from psycopg.rows import TupleRow
    from psycopg_pool import ConnectionPool

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


def postgres_versions(conn: psycopg.Connection[tuple[object, ...]]) -> set[int]:
    """Every version the store records; empty when it has no schema_version."""
    row = conn.execute("SELECT to_regclass('schema_version') IS NOT NULL").fetchone()
    if row is None or not row[0]:
        return set()
    return {int(str(found[0])) for found in conn.execute("SELECT version FROM schema_version")}


def skipped_versions(recorded: set[int]) -> list[int]:
    """Each declared Postgres migration with statements that the store
    passed without applying: below its highest recorded version, and not
    recorded itself.

    The runner applies only versions above the store's highest, so a
    migration numbered below a version a store already reached is never
    applied. Two branches numbering migrations at once can declare one
    (spec 116, Amendment of the local review of PR #310). A version with no
    statements, declared for the other dialect, changes nothing and is not
    counted.
    """
    from harrier.tracker.schema import POSTGRES_MIGRATIONS

    if not recorded:
        return []
    top = max(recorded)
    return sorted(
        version
        for version, statements in POSTGRES_MIGRATIONS
        if statements and version < top and version not in recorded
    )


def skipped_message(recorded: set[int], skipped: list[int]) -> str:
    named = ", ".join(str(version) for version in skipped)
    return (
        f"the store is at version {max(recorded)} but never applied migration {named}, "
        "declared below it after the store passed it. harrier store migrate applies "
        "migrations in order only; renumber the migration above the store's version."
    )


def check_no_skipped_versions(recorded: set[int]) -> None:
    """Refuse a store that passed a declared migration without applying it."""
    skipped = skipped_versions(recorded)
    if skipped:
        raise StoreVersionError(skipped_message(recorded, skipped))


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
        _release_the_session_lock(conn)


def _release_the_session_lock(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    """Unlock, best effort: never over the runner's own result or error.

    A session the server ended has released the lock with it, and psycopg
    learns of the end only on the next statement, so `conn.closed` can still
    be false here. The unlock then raised, and replaced a finished migration
    or a migration's refusal with the driver's error (privacy review of PR
    #281). Any session-level lock goes when its session does, so an unlock
    that fails leaves nothing held past the connection.
    """
    import psycopg

    with suppress(psycopg.Error):
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
    # Before anything is applied: applying the rest would move the store on
    # and leave the skipped migration further behind.
    check_no_skipped_versions(postgres_versions(conn))
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
        check_no_skipped_versions(postgres_versions(conn))
    except BaseException:
        conn.close()
        raise
    return conn


# --- the hosted API's pool (spec 134) ---

POOL_UNREACHABLE = "could not connect to the store"


class RequestPool:
    """The hosted API's connections, as the login role in the URL (spec 134).

    Each is in autocommit mode, as the seam requires, with prepared
    statements off (`prepare_threshold=None`): a transaction-mode pooler may
    hand each transaction to another server connection, where a statement
    prepared on the last one does not exist (spec 115, A5).

    `lease` hands one out and `release` takes it back. A connection that is
    closed, broken or not idle is discarded on release rather than handed
    out again: `idle_in_transaction_session_timeout` ends the server's
    connection, not only its transaction, and a connection left in a
    transaction would carry it to the next request. The pool would roll such
    a transaction back and keep the connection; this discards it.
    """

    def __init__(self, url: str, *, min_size: int, max_size: int, wait_seconds: float) -> None:
        require_driver()
        from psycopg_pool import ConnectionPool, PoolTimeout

        self._pool: ConnectionPool[psycopg.Connection[TupleRow]] = ConnectionPool(
            url,
            min_size=min_size,
            max_size=max_size,
            timeout=wait_seconds,
            open=False,
            name="harrier-requests",
            kwargs={"autocommit": True, "prepare_threshold": None, "connect_timeout": 10},
        )
        try:
            self._pool.open(wait=True, timeout=wait_seconds)
            opened = True
        except PoolTimeout:
            opened = False
        if not opened:
            self._pool.close()
            # Raised after the except block, so the pool's error is not
            # chained. It names no URL, host or user (spec 134, The gate).
            raise StoreConnectionError(POOL_UNREACHABLE)

    def lease(self) -> psycopg.Connection[TupleRow]:
        """One connection, waiting at most the pool's wait. Raises the
        pool's `PoolTimeout` when none comes free in time."""
        return self._pool.getconn()

    def release(self, conn: psycopg.Connection[TupleRow]) -> None:
        from psycopg.pq import TransactionStatus

        if not conn.closed and conn.info.transaction_status != TransactionStatus.IDLE:
            conn.close()
        self._pool.putconn(conn)

    def handed_out(self) -> int:
        """How many connections the pool has handed out since it opened."""
        return int(self._pool.get_stats().get("requests_num", 0))

    def close(self) -> None:
        self._pool.close()


def transaction_failed(conn: psycopg.Connection[TupleRow]) -> bool:
    """Whether `conn` is in a transaction a statement has failed in. A COMMIT
    there rolls back and reports no error, so a caller that would commit
    must ask first."""
    from psycopg.pq import TransactionStatus

    return conn.info.transaction_status == TransactionStatus.INERROR
