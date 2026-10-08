"""Every tracker row belongs to a named search track (spec 091, ADR-012).

Every database is built under `tmp_path` with synthetic rows (spec 060).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.tracker import TrackerError, add_job, get_job, list_jobs
from harrier.tracker.schema import MIGRATIONS
from harrier.tracks import (
    DEFAULT_TRACK_ID,
    TRACK_KINDS,
    InvalidSlugError,
    Scope,
    Track,
    UnknownTrackError,
    default_scope,
    list_tracks,
    resolve_scope,
    validate_slug,
)
from harrier_cli.main import main

SYNTHETIC_ROWS = [
    {
        "company": "Example Co",
        "title": "Senior Frontend Engineer",
        "url": "https://boards.example.com/example/1",
        "status": "prospect",
    },
    {
        "company": "Example Labs",
        "title": "Staff Engineer",
        "url": "https://boards.example.com/example/2",
        "status": "applied",
        "applied_date": "2026-08-02",
    },
    {
        "company": "Other Works",
        "title": "Product Engineer",
        "url": "https://boards.example.com/example/3",
        "status": "rejected",
        "rejection_reason": "stack",
    },
]

MIGRATION_8 = next(statements for version, statements in MIGRATIONS if version == 8)


def at_version_seven(path: Path) -> None:
    """A database migrated through seven, as the real runner leaves it, with
    rows and events, and nothing from migration eight."""
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in MIGRATIONS:
        if version >= 8:
            continue
        for statement in statements:
            raw.execute(statement)
        raw.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    raw.commit()
    raw.close()


def seed_through_the_store(path: Path) -> None:
    """Rows and their events, written by the write path against a database
    whose migrations stop before eight."""
    conn = connect(path)
    try:
        for row in SYNTHETIC_ROWS:
            add_job(conn, row, scope=default_scope(conn))
    finally:
        conn.close()


@pytest.fixture()
def seven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A populated database at version seven: migrations stop before eight,
    the store writes rows and events through the real runner, then the
    migration list is restored so the next open applies eight."""
    path = tmp_path / "tracker.db"
    at_version_seven(path)
    monkeypatch.setattr("harrier.tracker.schema.MIGRATIONS", [m for m in MIGRATIONS if m[0] < 8])
    # The store stamps track_id; before migration 8 there is no such column,
    # so the seed writes rows the way the schema at seven allowed.
    raw = sqlite3.connect(path)
    for index, row in enumerate(SYNTHETIC_ROWS, start=1):
        raw.execute(
            "INSERT INTO jobs (company, title, url, status) VALUES (?, ?, ?, ?)",
            (row["company"], row["title"], row["url"], row["status"]),
        )
        raw.execute(
            "INSERT INTO job_events (job_id, kind, actor, to_status) VALUES (?, ?, ?, ?)",
            (index, "created", "system", row["status"]),
        )
    raw.commit()
    raw.close()
    monkeypatch.setattr("harrier.tracker.schema.MIGRATIONS", MIGRATIONS)
    return path


def events(path: Path) -> list[tuple[object, ...]]:
    raw = sqlite3.connect(path)
    try:
        return [tuple(row) for row in raw.execute("SELECT * FROM job_events ORDER BY id")]
    finally:
        raw.close()


# --- migration 8 --------------------------------------------------------------


def test_migration_8_keeps_every_row_in_the_default_track(seven: Path) -> None:
    before = events(seven)
    conn = connect(seven)
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == 8
        rows = list_jobs(conn)
        assert len(rows) == len(SYNTHETIC_ROWS)
        assert {row["track_id"] for row in rows} == {str(DEFAULT_TRACK_ID)}
        tracks = list_tracks(conn)
        assert [(t.id, t.slug, t.kind, t.archived) for t in tracks] == [
            (1, "job", "industry", False)
        ]
    finally:
        conn.close()
    assert events(seven) == before


def test_migration_8_never_touches_job_events(seven: Path) -> None:
    before = events(seven)
    connect(seven).close()
    assert events(seven) == before
    # And the append-only triggers still fire, as spec 079 pins in
    # test_job_events.py::test_job_events_is_append_only.
    raw = sqlite3.connect(seven)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("UPDATE job_events SET to_status = 'applied' WHERE id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM job_events WHERE id = 1")
    finally:
        raw.close()


def test_an_unknown_track_is_refused_by_the_database(tmp_path: Path) -> None:
    path = tmp_path / "tracker.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    try:
        columns = "company, title, url, track_id"
        with pytest.raises(sqlite3.IntegrityError, match="unknown track"):
            raw.execute(
                f"INSERT INTO jobs ({columns}) VALUES (?, ?, ?, ?)",
                ("Example Co", "Engineer", "https://boards.example.com/x/1", 2),
            )
        raw.execute(
            f"INSERT INTO jobs ({columns}) VALUES (?, ?, ?, ?)",
            ("Example Co", "Engineer", "https://boards.example.com/x/1", 1),
        )
        with pytest.raises(sqlite3.IntegrityError, match="unknown track"):
            raw.execute("UPDATE jobs SET track_id = 2 WHERE track_id = 1")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            raw.execute("DELETE FROM tracks WHERE id = 1")
        # An id update would run none of the job triggers and strand every
        # row of the track (review of PR #180).
        with pytest.raises(sqlite3.IntegrityError, match="never changes"):
            raw.execute("UPDATE tracks SET id = 7 WHERE id = 1")
        assert [tuple(r) for r in raw.execute("SELECT id FROM tracks")] == [(1,)]
    finally:
        raw.close()


def test_the_kind_check_derives_from_the_code_list(tmp_path: Path) -> None:
    path = tmp_path / "tracker.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    try:
        for kind in TRACK_KINDS:
            raw.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES (?, ?, ?)",
                (f"probe-{kind}", kind, "probe"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES (?, ?, ?)",
                ("probe-other", "freelance", "probe"),
            )
    finally:
        raw.close()


# --- the write path -------------------------------------------------------------


def test_the_write_path_stamps_the_scopes_track(tmp_path: Path) -> None:
    conn = connect(tmp_path / "tracker.db")
    try:
        conn.execute(
            "INSERT INTO tracks (id, slug, kind, label) VALUES (2, 'second', 'academic', 'Second')"
        )
        conn.commit()
        second = resolve_scope(conn, "second")
        job_id = add_job(conn, SYNTHETIC_ROWS[0], scope=second)
        assert get_job(conn, job_id)["track_id"] == "2"
        with pytest.raises(TrackerError, match="track_id"):
            add_job(conn, {**SYNTHETIC_ROWS[1], "track_id": "1"}, scope=second)
        assert len(list_jobs(conn)) == 1
    finally:
        conn.close()


def test_a_duplicate_names_the_track_it_lives_in(tmp_path: Path) -> None:
    conn = connect(tmp_path / "tracker.db")
    try:
        add_job(conn, SYNTHETIC_ROWS[0], scope=default_scope(conn))
        with pytest.raises(TrackerError, match=r"in track job"):
            add_job(conn, SYNTHETIC_ROWS[0], scope=default_scope(conn))
    finally:
        conn.close()


# --- the slug rule and the resolver ---------------------------------------------


@pytest.mark.parametrize(
    "slug",
    ["", "a" * 33, "1job", "-job", "Job", "my job", "my.job", "job_search", "jöb", "job\n"],
)
def test_a_slug_outside_the_rule_is_refused(tmp_path: Path, slug: str) -> None:
    with pytest.raises(InvalidSlugError):
        validate_slug(slug)
    path = tmp_path / "tracker.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES (?, 'academic', 'x')", (slug,)
            )
    finally:
        raw.close()


@pytest.mark.parametrize("slug", ["job", "a", "a" * 32, "track-2027", "second-search-2"])
def test_a_slug_inside_the_rule_passes_both(tmp_path: Path, slug: str) -> None:
    assert validate_slug(slug) == slug
    path = tmp_path / "tracker.db"
    connect(path).close()
    raw = sqlite3.connect(path)
    try:
        if slug != "job":
            raw.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES (?, 'academic', 'x')", (slug,)
            )
    finally:
        raw.close()


def test_resolve_scope_names_an_unknown_slug(tmp_path: Path) -> None:
    conn = connect(tmp_path / "tracker.db")
    try:
        scope = default_scope(conn)
        assert isinstance(scope, Scope)
        assert isinstance(scope.track, Track)
        assert scope.track.id == DEFAULT_TRACK_ID
        assert resolve_scope(conn, None) == scope
        assert resolve_scope(conn, "job") == scope
        with pytest.raises(UnknownTrackError, match="'nope'"):
            resolve_scope(conn, "nope")
        with pytest.raises(InvalidSlugError):
            resolve_scope(conn, "Nope")
    finally:
        conn.close()


def test_the_module_offers_no_way_to_create_a_track() -> None:
    """Migration 8 seeds the one track; spec 093 adds the verbs."""
    import harrier.tracks as tracks

    public = {name for name in dir(tracks) if not name.startswith("_")}
    assert not {name for name in public if "add" in name or "create" in name or "archive" in name}


# --- the command ----------------------------------------------------------------


def test_tracks_list_prints_the_default_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    assert main(["tracks", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == ["1  job              industry  Job search"]
