"""Database connection and schema versioning.

One SQLite file at data/tracker.db (ADR-003), holding tracker and profile data
(ADR-008). WAL mode serves several processes under one kernel: the API, the
CLI and the scheduler.

Under one kernel only. WAL coordinates processes through shared memory, so a
process under the Docker Desktop VM and one under macOS reaching the same
file through a bind mount each build their own view of it. That corrupted the
database on 2026-09-18. Every open of the live file therefore asks who owns
it first (spec 061, ADR-011).
"""

from __future__ import annotations

import atexit
import os
import sqlite3
import time
from pathlib import Path

from harrier import container, hostlease
from harrier.demo import demo_data_dir, is_demo_mode
from harrier.paths import repo_root

DB_FILENAME = "tracker.db"

# How long a writer waits for another writer before giving up. Long enough to
# outlast any transaction this system holds, which are all single-statement or
# single-row, and short enough that a genuinely stuck writer still surfaces as
# an error rather than hanging a scheduled run forever.
BUSY_TIMEOUT_MS = 5000


def data_dir() -> Path:
    override = os.environ.get("HARRIER_DATA_DIR", "").strip()
    if override:
        return Path(override)
    # A demo run writes to a temp directory, never into the clone (spec 021).
    if is_demo_mode():
        return demo_data_dir()
    # Anchored to the repository root, not the working directory. Relative to
    # cwd, `just export` (which cd's into services/api) opened a second, empty
    # database and wrote a header-only CSV, exit code 0 both times: two
    # databases where ADR-003 says one. It also put a never-in-git directory
    # outside the root-anchored .gitignore patterns, so the tracker and the
    # logs were writable to a path git would have offered to commit.
    return repo_root() / "data"


def default_db_path() -> Path:
    return data_dir() / DB_FILENAME


# The exit status a refused CLI command returns: EX_TEMPFAIL. The command is
# not wrong, the moment is; it works once the container stops (spec 061).
EXIT_DATABASE_OWNED = 75


class DatabaseOwnershipError(Exception):
    """This process may not open the live database right now (spec 061).

    Deliberately not a RuntimeError, ValueError, OSError or sqlite3.Error. The
    CLI catches those broadly to print a one-line error and exit 1, and
    logging setup catches the last two to carry on without redaction. A
    refusal caught by any of them would read as an ordinary failure, or as no
    failure at all.
    """


class DatabaseOwnedByContainer(DatabaseOwnershipError):
    """The running harrier container has the live database mounted."""


class DatabaseOwnershipUnknown(DatabaseOwnershipError):
    """The Docker engine could not say whether the container has it."""


class DatabaseOwnedByHost(DatabaseOwnershipError):
    """Inside the container: a host process holds a lease on the database.

    `subcommand` and `since` describe the oldest lease, and are what the API
    answers with (spec 075). Neither carries an argument value.
    """

    def __init__(self, subcommand: str, since: str) -> None:
        super().__init__(
            f"a host process ({subcommand}, since {since}) holds the tracker database; "
            "it is refused here until that process ends. See `harrier doctor`."
        )
        self.subcommand = subcommand
        self.since = since


# The subcommand a lease records. The CLI sets it; any other host process
# that opens the database (`uv run python`, a host uvicorn) is "python".
_lease_subcommand = "python"
_held_lease: Path | None = None
_release_registered = False


def lease_directory() -> Path:
    return live_data_root() / hostlease.LEASE_DIRNAME


def set_lease_subcommand(subcommand: str) -> None:
    global _lease_subcommand
    _lease_subcommand = subcommand


def holds_host_lease() -> bool:
    """Whether this process holds a lease on the live database it would open.

    Compared by directory as well as existence: a lease held on some other
    data directory is no lease on this one.
    """
    return (
        _held_lease is not None and _held_lease.parent == lease_directory() and _held_lease.exists()
    )


def acquire_host_lease() -> None:
    """Write this process's lease, once, and drop it when the process ends."""
    global _held_lease, _release_registered
    if holds_host_lease():
        return
    release_host_lease()
    _held_lease = hostlease.write_lease(lease_directory(), os.getpid(), _lease_subcommand)
    if not _release_registered:
        atexit.register(release_host_lease)
        _release_registered = True


def release_host_lease() -> None:
    global _held_lease
    if _held_lease is not None:
        _held_lease.unlink(missing_ok=True)
    _held_lease = None


def live_data_root() -> Path:
    """The directory the container mounts at /app/data: this checkout's data/.

    `docker-compose.yml` mounts `./data`, relative to the checkout it is run
    from, so only a database under this directory can be the one a running
    container has open. Everything else (a test's tmp_path, a demo directory,
    an extracted backup, a HARRIER_DATA_DIR elsewhere) is never live and never
    costs an engine query.
    """
    return repo_root() / "data"


def _is_live(path: Path) -> bool:
    return path.resolve().is_relative_to(live_data_root().resolve())


def probe_database_ownership(path: Path) -> None:
    """Raise unless this process may open `path`, without taking a lease.

    The question alone, for a caller that only decides: the CLI asking
    whether to run a command here or hand it to the container. A process that
    hands its command over never opens the database, so it never takes a
    lease (spec 075).
    """
    if not _is_live(path):
        return
    if container.running_in() == "container":
        hold = hostlease.oldest_hold(lease_directory())
        if hold is not None:
            raise DatabaseOwnedByHost(*hold)
    _engine_verdict(path.resolve())


def check_database_ownership(path: Path) -> None:
    """Raise unless this process may open, copy or replace `path` (specs 061, 075).

    Inside the container, any host lease refuses the open. On the host, the
    process takes its lease first and asks the engine second, so a container
    that starts between the two sees the lease, and one that started before
    is seen by the engine. Refused, it gives the lease back: a refusal never
    leaves one behind. Once this process holds a lease the container is the
    one refused, so later opens here go through.

    Callers are `connect`, the backup snapshot and verify, and
    `create_backup` and `restore_backup`, which copy and replace the data
    directory without opening it through SQLite.
    """
    if not _is_live(path):
        return
    if container.running_in() == "container":
        probe_database_ownership(path)
        return
    if holds_host_lease():
        return
    acquire_host_lease()
    try:
        _engine_verdict(path.resolve())
    except DatabaseOwnershipError:
        release_host_lease()
        raise


def _engine_verdict(resolved: Path) -> None:
    """Refuse when the running container has `resolved` mounted, or when the
    engine cannot say (spec 061). Inside the container no engine socket is
    mounted, so the answer there is "unreachable"."""
    state = container.detect()
    if state.engine == container.UNKNOWN:
        raise DatabaseOwnershipUnknown(
            f"cannot tell whether the {container.CONTAINER_NAME} container owns the tracker "
            f"database ({state.reason}). Refusing to open it. See `harrier doctor`."
        )
    mounted = state.data_source
    if state.running and mounted is not None and resolved.is_relative_to(mounted.resolve()):
        raise DatabaseOwnedByContainer(
            f"the {container.CONTAINER_NAME} container owns the tracker database. "
            f"Run the command inside it: docker exec {container.CONTAINER_NAME} "
            "harrier <command>. Or stop the container first. See `harrier doctor`."
        )


def connect(db_path: Path | None = None, *, same_thread: bool = True) -> sqlite3.Connection:
    """Open the database, creating the directory and schema if needed.

    same_thread=False relaxes sqlite3's own check that a connection is used
    from the thread that made it. Only the API needs it, and only because
    FastAPI runs a sync dependency and the sync endpoint it feeds on
    different threadpool threads: the connection is handed between them, but
    never used by two at once, since each request opens and closes its own
    (harrier_api/deps.py, proven by test_api_jobs.py::
    test_concurrent_requests_do_not_trip_the_sqlite_thread_check).
    """
    path = db_path if db_path is not None else default_db_path()
    check_database_ownership(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=same_thread)
    conn.row_factory = sqlite3.Row
    # Several processes under one kernel write this database: the API and
    # CLI runs, in the container or on the host, never both at once (spec
    # 061). A second writer arriving mid-transaction still fails immediately
    # without this, and "database is locked" on a scheduled run is a failure
    # nobody is watching for. Waiting is the correct behaviour for a writer
    # that will get its turn in milliseconds. This comment used to say WAL
    # lets the container and the host interleave; across the bind mount it
    # does not, and that is how the file was corrupted on 2026-09-18.
    #
    # Set before the journal mode: a second process opening a brand-new file
    # reaches the journal-mode pragma while the first still holds the write
    # lock for migration one, and without a timeout it fails there instead of
    # waiting (spec 090).
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    _set_journal_mode_wal(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_schema(conn)
    except BaseException:
        # The caller never receives this connection, so nobody else can
        # close it (spec 076), and an open handle on a failed open would hold
        # the file and its -wal and -shm companions for the life of the
        # process (spec 090).
        conn.close()
        raise
    return conn


def _set_journal_mode_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL, waiting out another connection's write (spec 090).

    The journal-mode pragma answers SQLITE_BUSY without consulting the busy
    handler when another connection holds the file mid-write, which is what
    a first open looks like to a second first open arriving while the first
    is still applying migration one. Seen as "database is locked" from the
    second process in `tests/test_migration_runner.py` before this loop
    existed. The retry is bounded by the same timeout every other wait on
    this connection gets.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    version = row["v"] if row is not None else None
    return int(version) if version is not None else 0


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply every pending migration, each whole or not at all (spec 090).

    This used to run each migration under `with conn:`. The sqlite3 module
    opens that transaction only before a data statement, so a CREATE or
    ALTER was on disk the moment it returned, and a migration that failed
    on its third statement left its first two behind with no version row
    to say so. SQLite can roll schema changes back; it has to be asked.

    So the migration phase runs with the module's own transaction handling
    off, and takes the write lock first: BEGIN IMMEDIATE, then re-read the
    version under the lock, so two processes opening a new file at once
    apply each migration once, with the second finding the first's work
    instead of its CREATE TABLE.
    """
    from harrier.tracker.schema import MIGRATIONS

    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    current = schema_version(conn)
    pending = [(version, statements) for version, statements in MIGRATIONS if version > current]
    if not pending:
        # An ordinary open: no transaction, no lock.
        return

    previous_isolation_level = conn.isolation_level
    conn.isolation_level = None
    try:
        for version, statements in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if version <= schema_version(conn):
                    # Another process applied it between our read and our lock.
                    conn.execute("ROLLBACK")
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
    finally:
        conn.isolation_level = previous_isolation_level
