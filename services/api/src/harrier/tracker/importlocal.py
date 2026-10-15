"""A local store imports into one empty hosted account (spec 141).

`harrier store import-local --owner <user id>` reads `data/tracker.db`
read-only and copies the eight tables that hold the operator's search into
one hosted account, in one owner transaction. It is the one write path for
this move (ADR-003): the SQL is here, and the hosted writes go through the
owner connection the seam opens (spec 106, spec 112), so the row policy
decides which account a row lands in. `owner_id` is never written.

The order of a run:

1. The local store opens as a host command opens it (spec 061's container
   check, spec 075's lease), then read-only. Never through
   `harrier.db.connect()`, which migrates.
2. The local version, then the hosted login role, then the hosted version
   (opening the owner connection reads it as the tenant).
3. Inside the owner's transaction: lock track 1, require an empty account,
   compare columns, write, require that nothing else wrote, commit.

Every refusal is a `StoreImportRefused` (a `StoreError`), raised before the first
insert or inside the transaction, so nothing is kept. Nothing here prints:
the caller prints table names and counts only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harrier.apply.brief import APPLICATION_BRIEF_KIND
from harrier.connection import (
    URL_VARIABLE,
    Connection,
    StoreImportRefused,
    StoreUrlError,
    open_store,
    owner_claims,
    store_target,
)
from harrier.db import check_database_ownership
from harrier.loginrole import login_role_problems
from harrier.tracker.schema import MIGRATIONS

IMPORT_URL_VARIABLE = "HARRIER_IMPORT_DATABASE_URL"

# The eight tables the import copies, in the order it reports them.
TABLES: tuple[str, ...] = (
    "tracks",
    "jobs",
    "job_events",
    "job_eligibility",
    "contacts",
    "profile_documents",
    "user_config",
    "job_runs",
)

# The tables an account must hold none of, in the order a refusal names the
# first non-empty one. `tracks` is checked on its own: track 1 is allowed.
_OTHER_TABLES: tuple[str, ...] = TABLES[1:]

# The ordering column of each table: local id order is the history's order.
_ORDER_BY = {table: "id" for table in TABLES} | {"job_runs": "job"}

# How long the import waits for another writer's lock on the account. A
# second import for one owner blocks on track 1's row and then finds rows
# (spec 141, Failure modes); the seam's default would give up after seconds.
LOCK_WAIT_MS = 60_000

FIRST_TRACK_ID = 1

START_ONCE = "start the local app once, so it migrates, and stop it again"


@dataclass(frozen=True)
class LocalTable:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]

    def values(self, column: str) -> list[object]:
        at = self.columns.index(column)
        return [row[at] for row in self.rows]


@dataclass(frozen=True)
class LocalStore:
    tables: Mapping[str, LocalTable]

    def table(self, name: str) -> LocalTable:
        return self.tables[name]


# --- the local store, read-only ---


def latest_local_version() -> int:
    return max(version for version, _ in MIGRATIONS)


def read_local_store(path: Path) -> LocalStore:
    """The eight tables of the store at `path`, read without writing.

    Raises DatabaseOwnershipError when a container or a host lease holds the
    file (specs 061, 075), and StoreImportRefused for a missing file, a version
    other than the latest, or a file SQLite cannot read.
    """
    check_database_ownership(path)
    if not path.is_file():
        raise StoreImportRefused("the local store does not exist; there is nothing to import")
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as raw:
            version = _local_version(raw)
            latest = latest_local_version()
            if version < latest:
                raise StoreImportRefused(
                    f"the local store is at version {version}; this code needs {latest}. "
                    f"Please {START_ONCE}."
                )
            if version > latest:
                raise StoreImportRefused(
                    f"the local store is at version {version}, newer than this code knows "
                    f"({latest}). Run a newer harrier."
                )
            # One snapshot for all eight tables.
            raw.execute("BEGIN")
            tables = {name: _read_table(raw, name) for name in TABLES}
    except sqlite3.Error:
        # The class only: SQLite's text can name a path or quote a value.
        raise StoreImportRefused("the local store could not be read") from None
    return LocalStore(tables)


def _local_version(raw: sqlite3.Connection) -> int:
    found = raw.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if found is None:
        return 0
    row = raw.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def _read_table(raw: sqlite3.Connection, name: str) -> LocalTable:
    cursor = raw.execute(f"SELECT * FROM {name} ORDER BY {_ORDER_BY[name]}")
    columns = tuple(str(column[0]) for column in cursor.description)
    return LocalTable(columns, tuple(tuple(row) for row in cursor.fetchall()))


# --- the references inside the local rows ---


def check_references(local: LocalStore) -> None:
    """Refuse a local store whose rows name a track or job it does not hold.

    Run before the hosted store is reached: a reference with no row behind it
    has no hosted id to be rewritten to.
    """
    track_ids = set(local.table("tracks").values("id"))
    if FIRST_TRACK_ID not in track_ids:
        raise StoreImportRefused("the local store holds no track 1")
    job_ids = set(local.table("jobs").values("id"))
    _require_known(local.table("jobs").values("track_id"), track_ids, "jobs", "track")
    for table in ("job_events", "job_eligibility"):
        _require_known(local.table(table).values("job_id"), job_ids, table, "job")
    documents = local.table("profile_documents")
    _require_known(
        [value for value in documents.values("track_id") if value is not None],
        track_ids,
        "profile_documents",
        "track",
    )
    for kind, name in zip(documents.values("kind"), documents.values("name"), strict=True):
        if kind == APPLICATION_BRIEF_KIND and _job_number(name) not in job_ids:
            raise StoreImportRefused(
                "profile_documents holds an application brief for a job the local store "
                "does not hold"
            )


def _require_known(values: Sequence[object], known: set[object], table: str, what: str) -> None:
    if any(value not in known for value in values):
        raise StoreImportRefused(f"{table} holds a row for a {what} the local store does not hold")


def _require_job_number(name: object) -> int:
    number = _job_number(name)
    if number is None:
        raise StoreImportRefused("profile_documents holds an application brief named by no job")
    return number


def _job_number(name: object) -> int | None:
    text = str(name).strip()
    return int(text) if text.isascii() and text.isdigit() else None


def rewrite_linked_jobs(value: object, job_map: Mapping[int, int]) -> object:
    """A contact's `linked_jobs` with each link's `job_id` rewritten through
    `job_map` (spec 141, amendment 1).

    The column is a JSON list of links, each naming a tracked job by id
    (`harrier.outreach.joblink`). A link whose id the map does not hold
    names no job the store has, and is blanked rather than left to name
    whichever hosted job now has that number; its company, title and URL
    stay, as an unresolved link keeps them. Anything that is not a JSON list
    of objects is returned as written.
    """
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        payload: object = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(payload, list):
        return value
    changed = False
    links: list[object] = []
    for entry in cast("list[object]", payload):
        item: object = entry
        if isinstance(entry, dict):
            link = cast("dict[str, object]", entry)
            old = link.get("job_id", "")
            if str(old).strip():
                number = _job_number(old)
                new = job_map.get(number) if number is not None else None
                if new is None:
                    rewritten: object = ""
                elif isinstance(old, int) and not isinstance(old, bool):
                    rewritten = new
                else:
                    rewritten = str(new)
                item = {**link, "job_id": rewritten}
                changed = True
        links.append(item)
    return json.dumps(links, ensure_ascii=False) if changed else value


# --- the hosted store ---


def hosted_url(environ: Mapping[str, str]) -> str:
    """The URL `HARRIER_IMPORT_DATABASE_URL` names, or a refusal that names
    the variable and never its value."""
    raw = environ.get(IMPORT_URL_VARIABLE, "")
    if not raw.strip():
        raise StoreImportRefused(
            f"{IMPORT_URL_VARIABLE} is not set; export the run login's URL in the shell "
            "for this command"
        )
    try:
        target = store_target({URL_VARIABLE: raw})
    except StoreUrlError as error:
        raise StoreImportRefused(str(error).replace(URL_VARIABLE, IMPORT_URL_VARIABLE)) from None
    return target.url


def check_login_role(url: str) -> None:
    """Refuse a login role that can step past the row policy (spec 134's
    checks, the run login's). Connects as the login role, with no claims,
    and reads the catalog in a transaction that writes nothing. The
    migrator's URL fails here, before any owner connection."""
    from harrier.pgstore import postgres_connect

    driver = postgres_connect(url)
    try:
        conn = Connection.postgres(driver)
        with conn.transaction():
            row = conn.execute("SELECT current_user::text").fetchone()
            role = str(row[0]) if row is not None else ""
            problems = login_role_problems(conn, role)
    finally:
        driver.close()
    if problems:
        raise StoreImportRefused(
            f"{IMPORT_URL_VARIABLE} names a login role the import refuses: "
            + "; ".join(map(str, problems))
        )


def _is_empty(conn: Connection, table: str) -> bool:
    row = conn.execute(f"SELECT EXISTS (SELECT 1 FROM {table})").fetchone()
    return row is None or not row[0]


def require_empty_account(conn: Connection) -> None:
    """Lock track 1, then refuse unless the owner sees track 1 and nothing
    else (spec 141, check 4)."""
    held = conn.execute(
        "SELECT id FROM tracks WHERE id = ? FOR UPDATE", (FIRST_TRACK_ID,)
    ).fetchone()
    tracks = [row[0] for row in conn.execute("SELECT id FROM tracks ORDER BY id")]
    if held is None and not tracks:
        raise StoreImportRefused(
            "that owner sees no track: the account does not exist or has not been invited"
        )
    if tracks != [FIRST_TRACK_ID]:
        raise StoreImportRefused("the account already holds data: tracks holds more than track 1")
    for table in _OTHER_TABLES:
        if not _is_empty(conn, table):
            raise StoreImportRefused(f"the account already holds data: {table} holds rows")


def refuse_if_the_account_changed(conn: Connection, written: Mapping[str, int]) -> None:
    """Check 4 again, just before commit, counting only rows this
    transaction did not write: each table holds what the import wrote and no
    more. Track 1 existed before the import and is updated, not written, so
    the track count includes it."""
    for table in TABLES:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        found = int(row[0]) if row is not None else 0
        if found > written[table]:
            raise StoreImportRefused(
                f"the account changed during the import: {table} holds a row the import "
                "did not write"
            )


def check_columns(conn: Connection, local: LocalStore) -> None:
    """Refuse a difference in columns before any insert (spec 141, check 5):
    every local column has a hosted one of the same name, and every hosted
    column the copy does not fill has a default."""
    for table in TABLES:
        hosted: dict[str, bool] = {}
        for name, has_default, is_identity in conn.execute(
            "SELECT column_name, column_default IS NOT NULL, identity_generation IS NOT NULL "
            "FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ):
            hosted[str(name)] = bool(has_default) or bool(is_identity)
        columns = local.table(table).columns
        for column in columns:
            if column not in hosted:
                raise StoreImportRefused(f"the hosted {table} table has no column {column}")
        filled = set(_without_id(columns))
        for column, has_default in hosted.items():
            # A track's id is numbered by a trigger when the insert gives none.
            if column in filled or has_default or (table == "tracks" and column == "id"):
                continue
            raise StoreImportRefused(
                f"the hosted {table} table has a column {column} with no default"
            )


def _without_id(columns: Sequence[str]) -> list[str]:
    """What an insert names: the local columns less `id`. The hosted store
    numbers rows itself: identity columns shared by every owner, and tracks
    numbered per owner. `job_runs` has no `id`."""
    return [column for column in columns if column != "id"]


# --- the write ---


@dataclass(frozen=True)
class Written:
    """What an import copied, per table, in `TABLES` order."""

    counts: Mapping[str, int]


def import_local(
    owner: str, environ: Mapping[str, str], path: Path, *, lock_wait_ms: int = LOCK_WAIT_MS
) -> Written:
    """Copy the local store at `path` into the empty account `owner` names.

    `environ` supplies `HARRIER_IMPORT_DATABASE_URL`. Raises StoreError (an
    StoreImportRefused for a check) with nothing written, and
    DatabaseOwnershipError for a local store a container or host holds.
    """
    claims = owner_claims(owner)
    url = hosted_url(environ)
    local = read_local_store(path)
    check_references(local)
    check_login_role(url)
    conn = open_store({URL_VARIABLE: url}, owner=str(claims["sub"]))
    try:
        conn.busy_timeout_ms = lock_wait_ms
        with conn.transaction():
            require_empty_account(conn)
            check_columns(conn, local)
            written = _write_all(conn, local)
            refuse_if_the_account_changed(conn, written)
    finally:
        conn.close()
    return Written(written)


def _write_all(conn: Connection, local: LocalStore) -> dict[str, int]:
    track_map = _write_tracks(conn, local.table("tracks"))
    job_map = _write_jobs(conn, local.table("jobs"), track_map)
    for table in ("job_events", "job_eligibility"):
        _write_rows(conn, table, local.table(table), job_id=job_map)
    documents = local.table("profile_documents")
    _write_rows(conn, "profile_documents", documents, track_id=track_map, briefs=job_map)
    _write_rows(conn, "contacts", local.table("contacts"), linked_jobs=job_map)
    _write_rows(conn, "user_config", local.table("user_config"))
    _write_rows(conn, "job_runs", local.table("job_runs"))
    return {table: len(local.table(table).rows) for table in TABLES}


def _insert_sql(table: str, columns: Sequence[str], *, returning: bool) -> str:
    names = ", ".join(columns)
    marks = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({names}) VALUES ({marks})"
    return f"{sql} RETURNING id" if returning else sql


def _write_tracks(conn: Connection, table: LocalTable) -> dict[int, int]:
    """Track 1 takes local track 1's values; the rest are inserted in local
    id order and numbered by the store. Returns local id to hosted id."""
    columns = _without_id(table.columns)
    track_map: dict[int, int] = {}
    for row in table.rows:
        values = dict(zip(table.columns, row, strict=True))
        local_id = int(str(values["id"]))
        params = [values[column] for column in columns]
        if local_id == FIRST_TRACK_ID:
            sets = ", ".join(f"{column} = ?" for column in columns)
            conn.execute(f"UPDATE tracks SET {sets} WHERE id = ?", [*params, FIRST_TRACK_ID])
            track_map[local_id] = FIRST_TRACK_ID
            continue
        created = conn.execute(_insert_sql("tracks", columns, returning=True), params).fetchone()
        if created is None:
            raise StoreImportRefused("the hosted store numbered no track")
        track_map[local_id] = int(created[0])
    return track_map


def _write_jobs(
    conn: Connection, table: LocalTable, track_map: Mapping[int, int]
) -> dict[int, int]:
    columns = _without_id(table.columns)
    sql = _insert_sql("jobs", columns, returning=True)
    job_map: dict[int, int] = {}
    for row in table.rows:
        values = dict(zip(table.columns, row, strict=True))
        values["track_id"] = track_map[int(str(values["track_id"]))]
        created = conn.execute(sql, [values[column] for column in columns]).fetchone()
        if created is None:
            raise StoreImportRefused("the hosted store numbered no job")
        job_map[int(str(values["id"]))] = int(created[0])
    return job_map


def _write_rows(
    conn: Connection,
    table: str,
    local: LocalTable,
    *,
    job_id: Mapping[int, int] | None = None,
    track_id: Mapping[int, int] | None = None,
    linked_jobs: Mapping[int, int] | None = None,
    briefs: Mapping[int, int] | None = None,
) -> None:
    """Insert `local`'s rows in local id order. `job_id` rewrites a
    `job_id` column, `track_id` a `track_id` column (null stays null), and
    `linked_jobs` the job ids a contact's links name, and `briefs` the job id
    an application brief is named by (spec 141, amendment 1)."""
    columns = _without_id(local.columns)
    sql = _insert_sql(table, columns, returning=False)
    for row in local.rows:
        values = dict(zip(local.columns, row, strict=True))
        if job_id is not None:
            values["job_id"] = job_id[int(str(values["job_id"]))]
        if track_id is not None and values["track_id"] is not None:
            values["track_id"] = track_id[int(str(values["track_id"]))]
        if linked_jobs is not None:
            values["linked_jobs"] = rewrite_linked_jobs(values["linked_jobs"], linked_jobs)
        if briefs is not None and values["kind"] == APPLICATION_BRIEF_KIND:
            values["name"] = str(briefs[_require_job_number(values["name"])])
        conn.execute(sql, [values[column] for column in columns])
