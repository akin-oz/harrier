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

import os
import sqlite3
from pathlib import Path

from harrier import container
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


def live_data_root() -> Path:
    """The directory the container mounts at /app/data: this checkout's data/.

    `docker-compose.yml` mounts `./data`, relative to the checkout it is run
    from, so only a database under this directory can be the one a running
    container has open. Everything else (a test's tmp_path, a demo directory,
    an extracted backup, a HARRIER_DATA_DIR elsewhere) is never live and never
    costs an engine query.
    """
    return repo_root() / "data"


def check_database_ownership(path: Path) -> None:
    """Raise unless this process may open, copy or replace `path` (spec 061).

    Refuses when the running harrier container has mounted a directory that
    contains `path`, and when the engine cannot say. Callers are `connect`,
    the backup snapshot and verify, and `create_backup` and `restore_backup`,
    which copy and replace the data directory without opening it through
    SQLite. Inside the container no engine socket is mounted, so the answer
    there is "unreachable" and the container's own opens go through.
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(live_data_root().resolve()):
        return
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
    conn.execute("PRAGMA journal_mode=WAL")
    # Several processes under one kernel write this database: the API and
    # CLI runs, in the container or on the host, never both at once (spec
    # 061). A second writer arriving mid-transaction still fails immediately
    # without this, and "database is locked" on a scheduled run is a failure
    # nobody is watching for. Waiting is the correct behaviour for a writer
    # that will get its turn in milliseconds. This comment used to say WAL
    # lets the container and the host interleave; across the bind mount it
    # does not, and that is how the file was corrupted on 2026-09-18.
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_schema(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    version = row["v"] if row is not None else None
    return int(version) if version is not None else 0


def _apply_schema(conn: sqlite3.Connection) -> None:
    from harrier.tracker.schema import MIGRATIONS

    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    current = schema_version(conn)
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        with conn:
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
