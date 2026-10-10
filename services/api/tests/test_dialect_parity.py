"""The SQLite and Postgres stores build the same tracker (spec 103).

The schema has one definition with two dialects: SQLite's migrations and
the Postgres baseline and later migrations in `harrier.tracker.schema`. Nothing but this file
holds them together. It opens a fresh store of each and checks that they
have the same tables, and for each table:

- the same columns in the same order;
- the same type for each column, in the types the schema uses: a TEXT
  column is `text` with no length, an INTEGER column that holds a row id
  is `bigint`, and any other INTEGER column is `integer`. A narrower or
  wider Postgres type is a difference;
- the same nullability, but for the one difference named in
  `KNOWN_NULLABILITY_DIFFERENCES`;
- the same primary key, and the same unique constraints with the same
  partial predicates, with no two Postgres keys that read as one SQLite
  key;
- the same foreign keys, with the same actions, but for the one named in
  `KNOWN_FOREIGN_KEY_DIFFERENCES`, which SQLite holds as two triggers;
- for a row inserted with only its required columns, equal defaults, and
  each default timestamp in SQLite's shape and within two minutes of the
  current UTC time. The Postgres session and the test process are set to
  a zone that is not UTC first, so a default that follows the local zone
  is caught on either side, even on a host that runs in UTC.

Then it runs every probe against both, and each must be refused, or
accepted, in both. The probes are the spec's table, one accepted insert
per status, per track kind and per event kind and actor that the outcome
rule allows (and a refused one for each it does not), and a refusal or
acceptance for each column a CHECK, foreign key or trigger guards.

What it cannot see: a CHECK or trigger that changes no column, default,
key or probe outcome above. TRUNCATE is one such gap. SQLite has no
TRUNCATE statement, so no probe that runs on both stores can reach it,
and Postgres row triggers do not fire on it; spec 105 adds the
statement-level refusal.

The spec asks for this test to fail when one Postgres column is renamed, one
default is changed, or one probe's constraint is removed. The pull request
for spec 103 records those three runs.

Since spec 105 the two stores differ on purpose: Postgres has an owner_id on
every owned table, and some keys are per owner there and global here. Those
differences are declared in `harrier.tracker.schema` (HOSTED_ONLY_COLUMNS,
OWNER_SCOPED_KEYS), never skipped. This test removes the declared columns,
maps each declared key to its SQLite form, and fails on a declaration that
names a column or key neither store has. The Postgres side runs as the test
superuser with one synthetic owner's claims, so each owner_id default
resolves; the policy itself is `tests/test_owner_policy.py`'s to prove.

The SQLite probes run everywhere. The Postgres side needs a server and skips
locally without one (`tests/pg_support.py`).
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from pg_support import fresh_database, new_owner, set_session_owner

from harrier.db import connect
from harrier.pgstore import migrate_postgres, postgres_connect
from harrier.tracker.reasons import ACTORS, KINDS
from harrier.tracker.schema import HOSTED_ONLY_COLUMNS, OWNER_SCOPED_KEYS, STATUSES
from harrier.tracks import TRACK_KINDS

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[tuple[object, ...]]

Dialect = Literal["sqlite", "postgres"]
DIALECTS: tuple[Dialect, ...] = ("sqlite", "postgres")

# A foreign key as its columns, the table and columns it references, and its
# ON UPDATE and ON DELETE actions.
ForeignKey = tuple[tuple[str, ...], str, tuple[str, ...], str, str]


@dataclass(frozen=True)
class Shape:
    """What one table looks like, in terms both dialects can state."""

    columns: tuple[str, ...]
    # Each column's type as Postgres spells it, with its length if it has
    # one. For SQLite, the type its column must have on Postgres.
    types: dict[str, str]
    nullable: dict[str, bool]
    primary_key: tuple[str, ...]
    # Each unique constraint or unique index other than the primary key, as
    # its columns and its partial predicate ('' when it covers every row).
    unique: frozenset[tuple[tuple[str, ...], str]]
    foreign_keys: frozenset[ForeignKey]


# --- stores ---


@contextmanager
def sqlite_store(tmp_path: Path) -> Generator[sqlite3.Connection]:
    """A fresh SQLite store, migrated by the ordinary open, in autocommit."""
    with closing(connect(tmp_path / "parity.db")) as conn:
        conn.isolation_level = None
        yield conn


@contextmanager
def postgres_store() -> Generator[PgConnection]:
    """A fresh Postgres store, migrated by `harrier store migrate`'s path,
    with one synthetic owner's claims set for the session (spec 105). The
    owner exists in the shim's auth.users, so it has its track 1."""
    with fresh_database() as url:
        migrate_postgres(url)
        with closing(postgres_connect(url)) as conn:
            set_session_owner(conn, new_owner(conn))
            yield conn


# --- shapes ---


def _predicate(text: str) -> str:
    """A partial-index predicate reduced to a form both dialects print alike.

    SQLite keeps the text as written (`url != ''`); Postgres prints its own
    rendering (`(url <> ''::text)`).
    """
    text = text.lower().replace("::text", "").replace("!=", "<>")
    return re.sub(r"[\s()]", "", text)


def sqlite_affinity(declared: str) -> str:
    """The affinity SQLite gives a declared column type, by its own rules
    applied in its order (https://sqlite.org/datatype3.html, section 3.1)."""
    upper = declared.upper()
    if "INT" in upper:
        return "INTEGER"
    if any(part in upper for part in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in upper or not upper:
        return "BLOB"
    if any(part in upper for part in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def expected_postgres_type(declared: str, holds_row_id: bool) -> str:
    """The Postgres type a SQLite column must have.

    Only the types the schema uses. Mapping every type of the same affinity
    let `varchar(4)` pass for `text` and `integer` for a `bigint` id (review
    of PR #218). Any other affinity keeps its SQLite name, which no Postgres
    type is spelled as, so it is a difference rather than a match.
    """
    affinity = sqlite_affinity(declared)
    if affinity == "TEXT":
        return "text"
    if affinity == "INTEGER":
        return "bigint" if holds_row_id else "integer"
    return affinity


def postgres_type(data_type: str, length: object) -> str:
    """A Postgres column's type, with its length when it has one, so a
    `character varying(4)` can never read as `text`."""
    return data_type if length is None else f"{data_type}({length})"


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def sqlite_shape(conn: sqlite3.Connection, table: str) -> Shape:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    columns = tuple(str(row["name"]) for row in info)
    primary_key = tuple(
        str(row["name"]) for row in sorted((r for r in info if r["pk"]), key=lambda r: r["pk"])
    )
    # A lone INTEGER PRIMARY KEY is the rowid. It can never hold NULL, since
    # SQLite assigns one, though table_info reports it as nullable.
    rowid_alias = (
        primary_key[0]
        if len(primary_key) == 1
        and any(r["name"] == primary_key[0] and str(r["type"]).upper() == "INTEGER" for r in info)
        else None
    )
    nullable = {str(row["name"]): not row["notnull"] and row["name"] != rowid_alias for row in info}
    unique: set[tuple[tuple[str, ...], str]] = set()
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not index["unique"] or index["origin"] == "pk":
            continue
        name = str(index["name"])
        index_columns = tuple(
            str(row["name"]) for row in conn.execute(f"PRAGMA index_info({name})").fetchall()
        )
        predicate = ""
        if index["partial"]:
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
            ).fetchone()
            match = re.search(r"\bWHERE\b(.*)$", str(sql_row[0]), re.IGNORECASE | re.DOTALL)
            assert match is not None, f"{table}: partial index {name} has no WHERE clause"
            predicate = _predicate(match.group(1))
        unique.add((index_columns, predicate))
    # A row id: the table's own `id`, or a column that references one, by a
    # foreign key or by the triggers that stand in for one.
    references = sqlite_foreign_keys(conn, table) | {
        key for known_table, key in KNOWN_FOREIGN_KEY_DIFFERENCES if known_table == table
    }
    row_ids = {"id"} if rowid_alias == "id" else set[str]()
    row_ids |= {
        columns[0] for columns, _, parent_columns, *_ in references if parent_columns == ("id",)
    }
    types = {
        str(row["name"]): expected_postgres_type(str(row["type"]), row["name"] in row_ids)
        for row in info
    }
    return Shape(
        columns,
        types,
        nullable,
        primary_key,
        frozenset(unique),
        sqlite_foreign_keys(conn, table),
    )


def sqlite_foreign_keys(conn: sqlite3.Connection, table: str) -> frozenset[ForeignKey]:
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(int(row["id"]), []).append(row)
    keys: set[ForeignKey] = set()
    for parts in grouped.values():
        parts.sort(key=lambda row: int(row["seq"]))
        # A REFERENCES clause without columns reports none here. The schema
        # always names them, and comparing a guess would hide a difference.
        assert all(row["to"] is not None for row in parts), (
            f"{table}: a foreign key to {parts[0]['table']} names no parent columns"
        )
        keys.add(
            (
                tuple(str(row["from"]) for row in parts),
                str(parts[0]["table"]),
                tuple(str(row["to"]) for row in parts),
                str(parts[0]["on_update"]),
                str(parts[0]["on_delete"]),
            )
        )
    return frozenset(keys)


def postgres_tables(conn: PgConnection) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    ).fetchall()
    return {str(row[0]) for row in rows}


_PG_INDEXES = """
SELECT i.indisprimary,
       array_agg(a.attname::text ORDER BY k.ord),
       coalesce(pg_get_expr(i.indpred, i.indrelid), '')
FROM pg_index i
JOIN pg_class t ON t.oid = i.indrelid
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
WHERE t.relname = %s AND t.relnamespace = 'public'::regnamespace AND i.indisunique
GROUP BY i.indexrelid, i.indisprimary, i.indpred, i.indrelid
"""


_PG_FOREIGN_KEYS = """
SELECT array(
           SELECT a.attname::text
           FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
           ORDER BY k.ord
       ),
       p.relname::text,
       array(
           SELECT a.attname::text
           FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
           JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
           ORDER BY k.ord
       ),
       c.confupdtype::text,
       c.confdeltype::text
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_class p ON p.oid = c.confrelid
WHERE c.contype = 'f' AND t.relname = %s AND t.relnamespace = 'public'::regnamespace
"""

# pg_constraint's action codes, spelled as SQLite's foreign_key_list spells them.
_PG_ACTIONS = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def postgres_foreign_keys(conn: PgConnection, table: str) -> frozenset[ForeignKey]:
    keys: set[ForeignKey] = set()
    for columns, parent, parent_columns, on_update, on_delete in conn.execute(
        _PG_FOREIGN_KEYS, (table,)
    ).fetchall():
        keys.add(
            (
                tuple(str(name) for name in cast("list[object]", columns)),
                str(parent),
                tuple(str(name) for name in cast("list[object]", parent_columns)),
                _PG_ACTIONS[str(on_update)],
                _PG_ACTIONS[str(on_delete)],
            )
        )
    return frozenset(keys)


def postgres_shape(conn: PgConnection, table: str) -> Shape:
    info = conn.execute(
        "SELECT column_name, is_nullable, data_type, character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    columns = tuple(str(row[0]) for row in info)
    nullable = {str(row[0]): row[1] == "YES" for row in info}
    types = {str(row[0]): postgres_type(str(row[2]), row[3]) for row in info}
    primary_key: tuple[str, ...] = ()
    unique: set[tuple[tuple[str, ...], str]] = set()
    for is_primary, names, predicate in conn.execute(_PG_INDEXES, (table,)).fetchall():
        index_columns = tuple(str(name) for name in cast("list[object]", names))
        if is_primary:
            primary_key = index_columns
        else:
            unique.add((index_columns, _predicate(str(predicate))))
    return Shape(
        columns,
        types,
        nullable,
        primary_key,
        frozenset(unique),
        postgres_foreign_keys(conn, table),
    )


# A difference the parity test found and that the Postgres baseline cannot
# close. SQLite lets a non-INTEGER primary key hold NULL (a documented SQLite
# quirk), so `job_runs.job TEXT PRIMARY KEY` accepts a NULL job there; a
# Postgres primary key is always NOT NULL. Closing it takes a SQLite
# migration that rebuilds `job_runs`, which is its own change. Pinned here so
# it can neither spread nor be fixed without this entry being removed.
KNOWN_NULLABILITY_DIFFERENCES: dict[tuple[str, str], tuple[bool, bool]] = {
    # (table, column): (nullable in SQLite, nullable in Postgres)
    ("job_runs", "job"): (True, False),
}

# A foreign key one dialect has and the other holds some other way. SQLite
# refuses a REFERENCES clause on an added column whose default is not NULL,
# so migration 8 holds `jobs.track_id` to `tracks` with two triggers
# (`jobs_track_must_exist_on_insert` and `..._on_update`); the Postgres
# baseline declares the foreign key. The probes `job-unknown-track` and
# `job-track-update-unknown` hold the two to the same refusals.
KNOWN_FOREIGN_KEY_DIFFERENCES: dict[tuple[str, ForeignKey], Dialect] = {
    # (table, foreign key): the one dialect that declares it
    ("jobs", (("track_id",), "tracks", ("id",), "NO ACTION", "NO ACTION")): "postgres",
}


def compare_foreign_keys(table: str, lite: Shape, pg: Shape) -> list[str]:
    problems: list[str] = []
    only: dict[Dialect, frozenset[ForeignKey]] = {
        "sqlite": lite.foreign_keys - pg.foreign_keys,
        "postgres": pg.foreign_keys - lite.foreign_keys,
    }
    for dialect, keys in only.items():
        for key in sorted(keys):
            if KNOWN_FOREIGN_KEY_DIFFERENCES.get((table, key)) != dialect:
                problems.append(f"{table}: foreign key {key} is declared in {dialect} only")
    for (known_table, key), dialect in KNOWN_FOREIGN_KEY_DIFFERENCES.items():
        if known_table == table and key not in only[dialect]:
            problems.append(
                f"{table}: the known foreign key difference {key} ({dialect} only) is gone; "
                "update KNOWN_FOREIGN_KEY_DIFFERENCES"
            )
    return problems


def compare_shapes(table: str, lite: Shape, pg: Shape) -> list[str]:
    """Every way the two shapes of `table` differ, each naming the column."""
    problems: list[str] = []
    if lite.columns != pg.columns:
        only_lite = [c for c in lite.columns if c not in pg.columns]
        only_pg = [c for c in pg.columns if c not in lite.columns]
        problems.append(
            f"{table}: columns differ. Only in SQLite: {only_lite}. Only in Postgres: "
            f"{only_pg}. SQLite order: {list(lite.columns)}. Postgres order: {list(pg.columns)}"
        )
    for column in lite.columns:
        if column not in pg.nullable:
            continue
        if lite.types[column] != pg.types[column]:
            problems.append(
                f"{table}.{column}: SQLite's column needs {lite.types[column]} on Postgres, "
                f"which has {pg.types[column]}"
            )
        found = (lite.nullable[column], pg.nullable[column])
        expected = KNOWN_NULLABILITY_DIFFERENCES.get((table, column))
        if expected is not None:
            if found != expected:
                problems.append(
                    f"{table}.{column}: the known nullability difference {expected} is now "
                    f"{found}; update KNOWN_NULLABILITY_DIFFERENCES"
                )
        elif found[0] != found[1]:
            problems.append(
                f"{table}.{column}: nullable in SQLite is {found[0]}, in Postgres {found[1]}"
            )
    if lite.primary_key != pg.primary_key:
        problems.append(
            f"{table}: primary key is {list(lite.primary_key)} in SQLite, "
            f"{list(pg.primary_key)} in Postgres"
        )
    for columns, predicate in sorted(lite.unique - pg.unique):
        problems.append(f"{table}: unique {list(columns)} where '{predicate}' is in SQLite only")
    for columns, predicate in sorted(pg.unique - lite.unique):
        problems.append(f"{table}: unique {list(columns)} where '{predicate}' is in Postgres only")
    problems += compare_foreign_keys(table, lite, pg)
    return problems


# --- the declared differences (spec 105) ---

OwnerKeys = dict[tuple[str, tuple[str, ...]], tuple[str, ...] | None]


def _keys(shape: Shape) -> set[tuple[str, ...]]:
    return {shape.primary_key, *(columns for columns, _ in shape.unique)} - {()}


def declared_view(
    table: str,
    pg: Shape,
    hosted_columns: tuple[tuple[str, str], ...] = HOSTED_ONLY_COLUMNS,
    owner_keys: OwnerKeys = OWNER_SCOPED_KEYS,
) -> Shape:
    """`pg` as SQLite should see it: hosted-only columns removed, and each
    per-owner key in its SQLite form, or gone if only Postgres has it.

    A foreign key loses its hosted-only columns the same way, with the
    parent columns they pair with: `jobs (owner_id, track_id)` to `tracks
    (owner_id, id)` is `jobs (track_id)` to `tracks (id)` on SQLite. One whose
    columns are all hosted only (`owner_id` to `auth.users`) has no SQLite
    form and is dropped. Nothing else about a key is excused.
    """
    hidden = {column for owner_table, column in hosted_columns if owner_table == table}

    def mapped(key: tuple[str, ...]) -> tuple[str, ...] | None:
        return owner_keys.get((table, key), key)

    def local_form(key: ForeignKey) -> ForeignKey | None:
        columns, parent, parent_columns, on_update, on_delete = key
        kept = [(c, p) for c, p in zip(columns, parent_columns, strict=True) if c not in hidden]
        if not kept:
            return None
        return (
            tuple(c for c, _ in kept),
            parent,
            tuple(p for _, p in kept),
            on_update,
            on_delete,
        )

    return Shape(
        columns=tuple(column for column in pg.columns if column not in hidden),
        types={column: v for column, v in pg.types.items() if column not in hidden},
        nullable={column: v for column, v in pg.nullable.items() if column not in hidden},
        primary_key=mapped(pg.primary_key) or (),
        unique=frozenset(
            (key, predicate)
            for columns, predicate in pg.unique
            if (key := mapped(columns)) is not None
        ),
        foreign_keys=frozenset(
            local for key in pg.foreign_keys if (local := local_form(key)) is not None
        ),
    )


def merged_keys(table: str, pg: Shape, owner_keys: OwnerKeys = OWNER_SCOPED_KEYS) -> list[str]:
    """Every pair of Postgres unique keys that read as one key on SQLite.

    `declared_view` collects keys into a set, so a global key left beside a
    per-owner key whose SQLite form it equals was merged away, and the
    extra key was never reported (review of PR #216). Each such pair is a
    difference.
    """
    problems: list[str] = []
    seen: dict[tuple[tuple[str, ...], str], tuple[str, ...]] = {}
    for columns, predicate in sorted(pg.unique):
        local = owner_keys.get((table, columns), columns)
        if local is None:
            continue
        first = seen.setdefault((local, predicate), columns)
        if first != columns:
            problems.append(
                f"{table}: Postgres keys {list(first)} and {list(columns)} where "
                f"'{predicate}' both read as unique {list(local)} on SQLite"
            )
    return problems


def stale_declarations(
    lite: dict[str, Shape],
    pg: dict[str, Shape],
    hosted_columns: tuple[tuple[str, str], ...] = HOSTED_ONLY_COLUMNS,
    owner_keys: OwnerKeys = OWNER_SCOPED_KEYS,
) -> list[str]:
    """Every declaration that names something one store does not have.

    A declaration that matches nothing would excuse a difference that is not
    there, and keep excusing it after the schema moves on. Like
    KNOWN_NULLABILITY_DIFFERENCES, a stale entry fails.
    """
    problems: list[str] = []
    for table, column in hosted_columns:
        if table not in pg or column not in pg[table].columns:
            problems.append(f"HOSTED_ONLY_COLUMNS names {table}.{column}, not in Postgres")
        elif table in lite and column in lite[table].columns:
            problems.append(f"HOSTED_ONLY_COLUMNS names {table}.{column}, which SQLite has too")
    for (table, pg_key), lite_key in owner_keys.items():
        if table not in pg or pg_key not in _keys(pg[table]):
            problems.append(f"OWNER_SCOPED_KEYS names {table} {list(pg_key)}, not in Postgres")
        if pg_key[:1] != ("owner_id",):
            problems.append(f"OWNER_SCOPED_KEYS names {table} {list(pg_key)}, not led by owner_id")
        if lite_key is None:
            continue
        if lite_key != pg_key[1:]:
            problems.append(
                f"OWNER_SCOPED_KEYS maps {table} {list(pg_key)} to {list(lite_key)}, "
                "not to the same key without owner_id"
            )
        if table not in lite or lite_key not in _keys(lite[table]):
            problems.append(
                f"OWNER_SCOPED_KEYS maps {table} {list(pg_key)} to {list(lite_key)}, not in SQLite"
            )
    return problems


# --- defaults ---

# One row per table, with only the columns it requires, in insert order (the
# job event names job 1, the job inserted before it). Written once and run on
# both stores, so the statements are valid in both dialects.
MINIMAL_ROWS: tuple[tuple[str, str, str], ...] = (
    # (table, insert, select of the inserted row)
    (
        "tracks",
        "INSERT INTO tracks (slug, kind, label) VALUES ('parity', 'industry', 'Parity')",
        "SELECT * FROM tracks WHERE slug = 'parity'",
    ),
    ("jobs", "INSERT INTO jobs DEFAULT VALUES", "SELECT * FROM jobs"),
    (
        "job_events",
        "INSERT INTO job_events (job_id, kind, actor, to_status) "
        "VALUES (1, 'created', 'system', 'prospect')",
        "SELECT * FROM job_events",
    ),
    (
        "job_runs",
        "INSERT INTO job_runs (job, last_success_at) VALUES ('parity', '2026-10-10 00:00:00')",
        "SELECT * FROM job_runs",
    ),
    (
        "profile_documents",
        "INSERT INTO profile_documents (kind, name) VALUES ('truth', 'parity')",
        "SELECT * FROM profile_documents",
    ),
    (
        "user_config",
        "INSERT INTO user_config (kind) VALUES ('parity')",
        "SELECT * FROM user_config",
    ),
    ("contacts", "INSERT INTO contacts DEFAULT VALUES", "SELECT * FROM contacts"),
)


@dataclass(frozen=True)
class TimestampShape:
    pattern: re.Pattern[str]
    # The strptime format for the same text, read as UTC.
    format: str


SQLITE_NOW = TimestampShape(
    re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"), "%Y-%m-%d %H:%M:%S"
)
ISO_NOW = TimestampShape(
    re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"), "%Y-%m-%dT%H:%M:%SZ"
)
# How far a default timestamp may sit from the test's own clock reading.
CLOCK_TOLERANCE = timedelta(minutes=2)
# Any zone but UTC. A default that follows the session's zone rather than
# UTC is then hours off, not a match by accident.
NOT_UTC = "Asia/Tokyo"


def timestamp_shape(table: str, column: str) -> TimestampShape | None:
    """The shape a column's default timestamp must have, or None if not one."""
    if table == "job_events" and column == "at":
        return ISO_NOW
    if column in {"created_at", "updated_at"}:
        return SQLITE_NOW
    return None


def sqlite_rows(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for table, insert, select in MINIMAL_ROWS:
        conn.execute(insert)
        row = conn.execute(select).fetchone()
        assert row is not None, f"{table}: the SQLite minimal row is missing"
        rows[table] = dict(zip(row.keys(), tuple(row), strict=True))
    return rows


def postgres_rows(conn: PgConnection) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for table, insert, select in MINIMAL_ROWS:
        conn.execute(insert.encode())
        cursor = conn.execute(select.encode())
        row = cursor.fetchone()
        assert row is not None, f"{table}: the Postgres minimal row is missing"
        names = [column.name for column in cursor.description or ()]
        rows[table] = dict(zip(names, row, strict=True))
    return rows


def compare_rows(
    table: str, lite: dict[str, object], pg: dict[str, object], now: datetime
) -> list[str]:
    problems: list[str] = []
    for column, lite_value in lite.items():
        pg_value = pg.get(column)
        shape = timestamp_shape(table, column)
        if shape is None:
            if lite_value != pg_value:
                problems.append(
                    f"{table}.{column}: default is {lite_value!r} in SQLite, "
                    f"{pg_value!r} in Postgres"
                )
            continue
        for dialect, value in (("SQLite", lite_value), ("Postgres", pg_value)):
            if not isinstance(value, str) or not shape.pattern.match(value):
                problems.append(
                    f"{table}.{column}: {dialect} default {value!r} does not match "
                    f"{shape.pattern.pattern}"
                )
                continue
            at = datetime.strptime(value, shape.format).replace(tzinfo=UTC)
            if abs(at - now) > CLOCK_TOLERANCE:
                problems.append(
                    f"{table}.{column}: {dialect} default {value!r} is not the current UTC "
                    f"time ({now:%Y-%m-%d %H:%M:%S})"
                )
    return problems


@contextmanager
def process_zone(zone: str) -> Generator[None]:
    """Run in `zone` as the process's local time, then restore the zone.

    SQLite's 'localtime' reads the C library's zone, which `time.tzset`
    reloads from TZ. Without this the SQLite half of the UTC check could not
    fail on a host that runs in UTC (review of PR #218).
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_the_sqlite_side_runs_in_a_zone_that_is_not_utc(tmp_path: Path) -> None:
    """A default that read local time would match UTC on a UTC host. Inside
    `process_zone` it cannot."""
    with process_zone(NOT_UTC), sqlite_store(tmp_path) as lite:
        row = lite.execute("SELECT datetime('now', 'localtime') <> datetime('now')").fetchone()
        assert row is not None and row[0] == 1


def test_both_dialects_build_the_same_tracker(tmp_path: Path) -> None:
    with process_zone(NOT_UTC), sqlite_store(tmp_path) as lite, postgres_store() as pg:
        lite_tables = sqlite_tables(lite)
        pg_tables = postgres_tables(pg)
        assert lite_tables == pg_tables, (
            f"tables differ. Only in SQLite: {sorted(lite_tables - pg_tables)}. "
            f"Only in Postgres: {sorted(pg_tables - lite_tables)}"
        )
        lite_shapes = {table: sqlite_shape(lite, table) for table in lite_tables}
        pg_shapes = {table: postgres_shape(pg, table) for table in pg_tables}
        problems = stale_declarations(lite_shapes, pg_shapes)
        for table in sorted(lite_tables):
            problems += merged_keys(table, pg_shapes[table])
            problems += compare_shapes(
                table, lite_shapes[table], declared_view(table, pg_shapes[table])
            )
        assert not problems, "\n".join(problems)

        # A Postgres default reads the session's zone unless it converts to
        # UTC itself. A SQLite default reads the process's zone only if it
        # asks for 'localtime', which the process zone set above exposes.
        pg.execute(f"SET TIME ZONE '{NOT_UTC}'".encode())
        now = datetime.now(UTC)
        lite_rows = sqlite_rows(lite)
        pg_rows = postgres_rows(pg)
        for table, _, _ in MINIMAL_ROWS:
            problems += compare_rows(table, lite_rows[table], pg_rows[table], now)
        assert not problems, "\n".join(problems)

        # The hosted-only owner_id took the session's owner as its default.
        owner = pg.execute("SELECT auth.uid()").fetchone()
        assert owner is not None and owner[0] is not None
        hosted = {table for table, _ in HOSTED_ONLY_COLUMNS}
        assert {table: pg_rows[table]["owner_id"] for table in hosted} == dict.fromkeys(
            hosted, owner[0]
        )

        # The table list for the defaults is written by hand; a new table
        # must join it, or its defaults go unchecked.
        assert {table for table, _, _ in MINIMAL_ROWS} == lite_tables - {"schema_version"}


def test_a_declaration_naming_nothing_fails() -> None:
    """A stale OWNER_SCOPED_KEYS or HOSTED_ONLY_COLUMNS entry is a failure,
    not a silent excuse. Shapes built by hand, so no server is needed."""
    lite = {
        "t": Shape(
            ("id", "slug"),
            {"id": "bigint", "slug": "text"},
            {"id": False, "slug": False},
            ("id",),
            frozenset(),
            frozenset(),
        )
    }
    pg = {
        "t": Shape(
            ("id", "slug", "owner_id"),
            {"id": "bigint", "slug": "text", "owner_id": "uuid"},
            {"id": False, "slug": False, "owner_id": False},
            ("owner_id", "id"),
            frozenset({(("owner_id", "slug"), "")}),
            frozenset({(("owner_id",), "users", ("id",), "NO ACTION", "NO ACTION")}),
        )
    }
    keys: OwnerKeys = {("t", ("owner_id", "id")): ("id",)}
    columns = (("t", "owner_id"),)
    assert stale_declarations(lite, pg, columns, keys) == []
    view = declared_view("t", pg["t"], columns, keys)
    # The per-owner slug key is undeclared, so it is left as it is and the
    # comparison reports it.
    assert compare_shapes("t", lite["t"], view) == [
        "t: unique ['owner_id', 'slug'] where '' is in Postgres only"
    ]

    missing_key: OwnerKeys = {**keys, ("t", ("owner_id", "label")): ("label",)}
    assert stale_declarations(lite, pg, columns, missing_key) == [
        "OWNER_SCOPED_KEYS names t ['owner_id', 'label'], not in Postgres",
        "OWNER_SCOPED_KEYS maps t ['owner_id', 'label'] to ['label'], not in SQLite",
    ]
    assert stale_declarations(lite, pg, (*columns, ("t", "tenant")), keys) == [
        "HOSTED_ONLY_COLUMNS names t.tenant, not in Postgres"
    ]


def test_a_global_key_beside_its_per_owner_form_is_a_difference() -> None:
    """A global unique url left beside the per-owner one read as the same
    SQLite key, and the set in `declared_view` merged the two (review of PR
    #216). Shapes built by hand, so no server is needed."""
    per_owner = (("owner_id", "url"), "url<>''")
    global_key = (("url",), "url<>''")
    pg = Shape(
        ("id", "url", "owner_id"),
        {"id": "bigint", "url": "text", "owner_id": "uuid"},
        {"id": False, "url": False, "owner_id": False},
        ("id",),
        frozenset({per_owner, global_key}),
        frozenset(),
    )
    keys: OwnerKeys = {("t", ("owner_id", "url")): ("url",)}

    assert merged_keys("t", pg, keys) == [
        "t: Postgres keys ['owner_id', 'url'] and ['url'] where 'url<>''' "
        "both read as unique ['url'] on SQLite"
    ]
    alone = Shape(
        pg.columns, pg.types, pg.nullable, pg.primary_key, frozenset({per_owner}), frozenset()
    )
    assert merged_keys("t", alone, keys) == []


def test_a_narrower_postgres_type_is_a_difference(tmp_path: Path) -> None:
    """Every type of one affinity used to match, so a `varchar(4)` text
    column and an `integer` id passed (review of PR #218)."""
    problems: list[str] = []
    with sqlite_store(tmp_path) as lite, postgres_store() as pg:
        pg.execute(b"ALTER TABLE tracks ALTER COLUMN archived_at TYPE varchar(4)")
        pg.execute(b"ALTER TABLE contacts ALTER COLUMN id TYPE integer")
        for table in ("tracks", "contacts"):
            problems += compare_shapes(
                table, sqlite_shape(lite, table), declared_view(table, postgres_shape(pg, table))
            )
    assert problems == [
        "tracks.archived_at: SQLite's column needs text on Postgres, "
        "which has character varying(4)",
        "contacts.id: SQLite's column needs bigint on Postgres, which has integer",
    ]


def test_the_known_difference_is_real(tmp_path: Path) -> None:
    """`job_runs.job` takes NULL in SQLite: the entry above is not a guess."""
    with sqlite_store(tmp_path) as lite:
        lite.execute("INSERT INTO job_runs (job, last_success_at) VALUES (NULL, 'x')")
        assert lite.execute("SELECT count(*) FROM job_runs WHERE job IS NULL").fetchone()[0] == 1


def test_the_known_foreign_key_difference_is_real(tmp_path: Path) -> None:
    """SQLite has no foreign key on `jobs.track_id`, and still refuses an
    unknown track, with the triggers' message rather than a foreign key's."""
    with sqlite_store(tmp_path) as lite:
        declared = sqlite_foreign_keys(lite, "jobs")
        assert all("track_id" not in columns for columns, *_ in declared)
        lite.execute(ONE_JOB)
        for statement in (
            "INSERT INTO jobs (track_id) VALUES (99)",
            "UPDATE jobs SET track_id = 99",
        ):
            with pytest.raises(sqlite3.IntegrityError) as refused:
                lite.execute(statement)
            assert str(refused.value) == "unknown track", statement


# --- probes ---


@dataclass(frozen=True)
class Probe:
    """One statement both stores must refuse, or both accept.

    `setup` runs first and must succeed. A refusal may name the message both
    dialects raise; where the dialects refuse differently (a trigger in
    SQLite, a foreign key in Postgres) only the refusal is asserted, as the
    spec says. An accepted probe may carry a count query and the count it
    must return afterwards.
    """

    name: str
    statement: str
    refused: bool
    message: str | None = None
    setup: tuple[str, ...] = ()
    check: tuple[str, int] | None = None


ONE_JOB = "INSERT INTO jobs DEFAULT VALUES"
ONE_EVENT = (
    "INSERT INTO job_events (job_id, kind, actor, to_status) "
    "VALUES (1, 'created', 'system', 'prospect')"
)


def _track(slug: str) -> str:
    return f"INSERT INTO tracks (slug, kind, label) VALUES ('{slug}', 'industry', 'Probe')"


def _deadline(value: str) -> str:
    return f"INSERT INTO jobs (deadline) VALUES ('{value}')"


def _event(kind: str, actor: str, *, job_id: int = 1, backfilled: int = 0) -> str:
    return (
        "INSERT INTO job_events (job_id, kind, actor, to_status, backfilled) "
        f"VALUES ({job_id}, '{kind}', '{actor}', 'prospect', {backfilled})"
    )


def _every_status() -> tuple[Probe, ...]:
    """Each status the lifecycle names is one a job can hold, in both."""
    return tuple(
        Probe(
            f"status-{status}",
            f"INSERT INTO jobs (status) VALUES ('{status}')",
            refused=False,
            check=(f"SELECT count(*) FROM jobs WHERE status = '{status}'", 1),
        )
        for status in STATUSES
    )


def _every_track_kind() -> tuple[Probe, ...]:
    return tuple(
        Probe(
            f"track-kind-{kind}",
            f"INSERT INTO tracks (slug, kind, label) VALUES ('kind-probe', '{kind}', 'Probe')",
            refused=False,
            check=(f"SELECT count(*) FROM tracks WHERE slug = 'kind-probe' AND kind = '{kind}'", 1),
        )
        for kind in TRACK_KINDS
    )


def _every_event_kind_and_actor() -> tuple[Probe, ...]:
    """Every kind with every actor: accepted exactly when the outcome rule
    holds, that an outcome is the company's and only an outcome is. The
    spec's own probe, an outcome by the candidate, is one of these."""
    probes: list[Probe] = []
    for kind in KINDS:
        for actor in ACTORS:
            allowed = (kind == "outcome") == (actor == "company")
            probes.append(
                Probe(
                    f"event-{kind}-by-{actor}",
                    _event(kind, actor),
                    refused=not allowed,
                    setup=(ONE_JOB,),
                    check=(
                        "SELECT count(*) FROM job_events "
                        f"WHERE kind = '{kind}' AND actor = '{actor}'",
                        1,
                    )
                    if allowed
                    else None,
                )
            )
    return tuple(probes)


PROBES: tuple[Probe, ...] = (
    Probe("status-bogus", "INSERT INTO jobs (status) VALUES ('bogus')", refused=True),
    Probe("slug-uppercase-underscore", _track("Bad_Slug"), refused=True),
    Probe("slug-leading-digit", _track("1abc"), refused=True),
    Probe("slug-33-letters", _track("a" * 33), refused=True),
    Probe(
        "slug-ok-1",
        _track("ok-1"),
        refused=False,
        check=("SELECT count(*) FROM tracks WHERE slug = 'ok-1'", 1),
    ),
    Probe("deadline-unpadded", _deadline("2026-1-01"), refused=True),
    Probe(
        "deadline-empty",
        _deadline(""),
        refused=False,
        check=("SELECT count(*) FROM jobs WHERE deadline = ''", 1),
    ),
    Probe(
        "deadline-date",
        _deadline("2026-10-10"),
        refused=False,
        check=("SELECT count(*) FROM jobs WHERE deadline = '2026-10-10'", 1),
    ),
    Probe(
        "job-events-update",
        "UPDATE job_events SET reason_text = 'edited'",
        refused=True,
        message="job_events is append-only",
        setup=(ONE_JOB, ONE_EVENT),
    ),
    Probe(
        "job-events-delete",
        "DELETE FROM job_events",
        refused=True,
        message="job_events is append-only",
        setup=(ONE_JOB, ONE_EVENT),
    ),
    Probe("job-unknown-track", "INSERT INTO jobs (track_id) VALUES (99)", refused=True),
    Probe(
        "track-delete",
        "DELETE FROM tracks WHERE id = 1",
        refused=True,
        message="tracks are archived, never deleted",
    ),
    Probe(
        "track-id-update",
        "UPDATE tracks SET id = 5 WHERE id = 1",
        refused=True,
        message="a track id never changes",
    ),
    Probe(
        "duplicate-url",
        "INSERT INTO jobs (url) VALUES ('https://example.com/jobs/1')",
        refused=True,
        setup=("INSERT INTO jobs (url) VALUES ('https://example.com/jobs/1')",),
    ),
    Probe(
        "two-empty-urls",
        "INSERT INTO jobs (url) VALUES ('')",
        refused=False,
        setup=("INSERT INTO jobs (url) VALUES ('')",),
        check=("SELECT count(*) FROM jobs WHERE url = ''", 2),
    ),
    Probe(
        "track-without-id-after-seed",
        _track("second"),
        refused=False,
        check=("SELECT count(*) FROM tracks WHERE slug = 'second' AND id <> 1", 1),
    ),
    Probe(
        "duplicate-user-config-kind",
        "INSERT INTO user_config (kind) VALUES ('watchlist')",
        refused=True,
        setup=("INSERT INTO user_config (kind) VALUES ('watchlist')",),
    ),
    Probe(
        "duplicate-profile-document",
        "INSERT INTO profile_documents (kind, name) VALUES ('truth', 'probe')",
        refused=True,
        setup=("INSERT INTO profile_documents (kind, name) VALUES ('truth', 'probe')",),
    ),
    # Beyond the spec's table: a refusal, or an acceptance, for each column a
    # CHECK, foreign key or trigger guards, so removing or narrowing one in
    # a single dialect changes an outcome (post-merge review of PR #207).
    Probe(
        "track-kind-bogus",
        "INSERT INTO tracks (slug, kind, label) VALUES ('kind-probe', 'bogus', 'Probe')",
        refused=True,
    ),
    Probe("event-kind-bogus", _event("bogus", "system"), refused=True, setup=(ONE_JOB,)),
    Probe("event-actor-bogus", _event("created", "bogus"), refused=True, setup=(ONE_JOB,)),
    Probe(
        "event-backfilled-1",
        _event("created", "system", backfilled=1),
        refused=False,
        setup=(ONE_JOB,),
        check=("SELECT count(*) FROM job_events WHERE backfilled = 1", 1),
    ),
    Probe(
        "event-backfilled-2",
        _event("created", "system", backfilled=2),
        refused=True,
        setup=(ONE_JOB,),
    ),
    Probe("event-for-a-missing-job", _event("created", "system", job_id=99), refused=True),
    Probe(
        "job-track-update-unknown",
        "UPDATE jobs SET track_id = 99",
        refused=True,
        setup=(ONE_JOB,),
    ),
    Probe(
        "job-events-update-to-status",
        "UPDATE job_events SET to_status = 'x'",
        refused=True,
        message="job_events is append-only",
        setup=(ONE_JOB, ONE_EVENT),
    ),
    Probe(
        "track-archive",
        "UPDATE tracks SET archived_at = 'x' WHERE id = 1",
        refused=False,
        check=("SELECT count(*) FROM tracks WHERE id = 1 AND archived_at = 'x'", 1),
    ),
    Probe(
        "track-relabel",
        "UPDATE tracks SET label = 'y' WHERE id = 1",
        refused=False,
        check=("SELECT count(*) FROM tracks WHERE id = 1 AND label = 'y'", 1),
    ),
    *_every_status(),
    *_every_track_kind(),
    *_every_event_kind_and_actor(),
)


@dataclass(frozen=True)
class Outcome:
    refused: bool
    message: str = ""
    count: int | None = None


def sqlite_outcome(conn: sqlite3.Connection, probe: Probe) -> Outcome:
    for statement in probe.setup:
        conn.execute(statement)
    try:
        conn.execute(probe.statement)
    except sqlite3.IntegrityError as error:
        return Outcome(refused=True, message=str(error))
    if probe.check is None:
        return Outcome(refused=False)
    row = conn.execute(probe.check[0]).fetchone()
    return Outcome(refused=False, count=int(row[0]))


def postgres_outcome(conn: PgConnection, probe: Probe) -> Outcome:
    import psycopg

    for statement in probe.setup:
        conn.execute(statement.encode())
    try:
        conn.execute(probe.statement.encode())
    except (psycopg.errors.IntegrityError, psycopg.errors.RaiseException) as error:
        return Outcome(refused=True, message=error.diag.message_primary or "")
    if probe.check is None:
        return Outcome(refused=False)
    row = conn.execute(probe.check[0].encode()).fetchone()
    assert row is not None
    return Outcome(refused=False, count=int(str(row[0])))


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("probe", PROBES, ids=[probe.name for probe in PROBES])
def test_a_probe_is_refused_or_accepted_on_both(
    probe: Probe, dialect: Dialect, tmp_path: Path
) -> None:
    if dialect == "sqlite":
        with sqlite_store(tmp_path) as lite:
            outcome = sqlite_outcome(lite, probe)
    else:
        with postgres_store() as pg:
            outcome = postgres_outcome(pg, probe)

    verdict = "refused" if probe.refused else "accepted"
    assert outcome.refused == probe.refused, (
        f"probe {probe.name} on {dialect}: expected {verdict}, "
        f"got {'refused: ' + outcome.message if outcome.refused else 'accepted'}"
    )
    if probe.message is not None:
        assert outcome.message == probe.message, (
            f"probe {probe.name} on {dialect}: refused with {outcome.message!r}, "
            f"expected {probe.message!r}"
        )
    if probe.check is not None:
        assert outcome.count == probe.check[1], (
            f"probe {probe.name} on {dialect}: {probe.check[0]} gave {outcome.count}, "
            f"expected {probe.check[1]}"
        )
