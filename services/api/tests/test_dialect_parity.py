"""The SQLite and Postgres stores build the same tracker (spec 103).

The schema has one definition with two dialects: SQLite's nine migrations
and the Postgres baseline in `harrier.tracker.schema`. Nothing but this file
holds them together. It opens a fresh store of each and proves that they
have the same tables, the same columns in the same order, the same
nullability, the same unique constraints and primary keys, and that a row
inserted with only its required columns gets defaults of the same shape.
Then it runs every probe in the spec's table against both, and each must be
refused, or accepted, in both.

The spec asks for this test to fail when one Postgres column is renamed, one
default is changed, or one probe's constraint is removed. The pull request
for spec 103 records those three runs.

The SQLite probes run everywhere. The Postgres side needs a server and skips
locally without one (`tests/pg_support.py`).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import pytest
from pg_support import fresh_database

from harrier.db import connect
from harrier.pgstore import migrate_postgres, postgres_connect

if TYPE_CHECKING:
    import psycopg

    PgConnection = psycopg.Connection[tuple[object, ...]]

Dialect = Literal["sqlite", "postgres"]
DIALECTS: tuple[Dialect, ...] = ("sqlite", "postgres")


@dataclass(frozen=True)
class Shape:
    """What one table looks like, in terms both dialects can state."""

    columns: tuple[str, ...]
    nullable: dict[str, bool]
    primary_key: tuple[str, ...]
    # Each unique constraint or unique index other than the primary key, as
    # its columns and its partial predicate ('' when it covers every row).
    unique: frozenset[tuple[tuple[str, ...], str]]


# --- stores ---


@contextmanager
def sqlite_store(tmp_path: Path) -> Generator[sqlite3.Connection]:
    """A fresh SQLite store, migrated by the ordinary open, in autocommit."""
    with closing(connect(tmp_path / "parity.db")) as conn:
        conn.isolation_level = None
        yield conn


@contextmanager
def postgres_store() -> Generator[PgConnection]:
    """A fresh Postgres store, migrated by `harrier store migrate`'s path."""
    with fresh_database() as url:
        migrate_postgres(url)
        with closing(postgres_connect(url)) as conn:
            yield conn


# --- shapes ---


def _predicate(text: str) -> str:
    """A partial-index predicate reduced to a form both dialects print alike.

    SQLite keeps the text as written (`url != ''`); Postgres prints its own
    rendering (`(url <> ''::text)`).
    """
    text = text.lower().replace("::text", "").replace("!=", "<>")
    return re.sub(r"[\s()]", "", text)


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
    return Shape(columns, nullable, primary_key, frozenset(unique))


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


def postgres_shape(conn: PgConnection, table: str) -> Shape:
    info = conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    columns = tuple(str(row[0]) for row in info)
    nullable = {str(row[0]): row[1] == "YES" for row in info}
    primary_key: tuple[str, ...] = ()
    unique: set[tuple[tuple[str, ...], str]] = set()
    for is_primary, names, predicate in conn.execute(_PG_INDEXES, (table,)).fetchall():
        index_columns = tuple(str(name) for name in cast("list[object]", names))
        if is_primary:
            primary_key = index_columns
        else:
            unique.add((index_columns, _predicate(str(predicate))))
    return Shape(columns, nullable, primary_key, frozenset(unique))


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

SQLITE_NOW = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
ISO_NOW = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def timestamp_pattern(table: str, column: str) -> re.Pattern[str] | None:
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


def compare_rows(table: str, lite: dict[str, object], pg: dict[str, object]) -> list[str]:
    problems: list[str] = []
    for column, lite_value in lite.items():
        pg_value = pg.get(column)
        pattern = timestamp_pattern(table, column)
        if pattern is None:
            if lite_value != pg_value:
                problems.append(
                    f"{table}.{column}: default is {lite_value!r} in SQLite, "
                    f"{pg_value!r} in Postgres"
                )
            continue
        for dialect, value in (("SQLite", lite_value), ("Postgres", pg_value)):
            if not isinstance(value, str) or not pattern.match(value):
                problems.append(
                    f"{table}.{column}: {dialect} default {value!r} does not match "
                    f"{pattern.pattern}"
                )
    return problems


def test_both_dialects_build_the_same_tracker(tmp_path: Path) -> None:
    with sqlite_store(tmp_path) as lite, postgres_store() as pg:
        lite_tables = sqlite_tables(lite)
        pg_tables = postgres_tables(pg)
        assert lite_tables == pg_tables, (
            f"tables differ. Only in SQLite: {sorted(lite_tables - pg_tables)}. "
            f"Only in Postgres: {sorted(pg_tables - lite_tables)}"
        )
        problems: list[str] = []
        for table in sorted(lite_tables):
            problems += compare_shapes(table, sqlite_shape(lite, table), postgres_shape(pg, table))
        assert not problems, "\n".join(problems)

        lite_rows = sqlite_rows(lite)
        pg_rows = postgres_rows(pg)
        for table, _, _ in MINIMAL_ROWS:
            problems += compare_rows(table, lite_rows[table], pg_rows[table])
        assert not problems, "\n".join(problems)

        # The table list for the defaults is written by hand; a new table
        # must join it, or its defaults go unchecked.
        assert {table for table, _, _ in MINIMAL_ROWS} == lite_tables - {"schema_version"}


def test_the_known_difference_is_real(tmp_path: Path) -> None:
    """`job_runs.job` takes NULL in SQLite: the entry above is not a guess."""
    with sqlite_store(tmp_path) as lite:
        lite.execute("INSERT INTO job_runs (job, last_success_at) VALUES (NULL, 'x')")
        assert lite.execute("SELECT count(*) FROM job_runs WHERE job IS NULL").fetchone()[0] == 1


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
        "outcome-by-candidate",
        "INSERT INTO job_events (job_id, kind, actor, to_status) "
        "VALUES (1, 'outcome', 'candidate', 'rejected')",
        refused=True,
        setup=(ONE_JOB,),
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
