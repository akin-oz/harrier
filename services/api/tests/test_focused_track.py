"""A focused track scores bounded roles by its own configuration (spec 137).

Every database is built under `tmp_path`. Every track, company, posting and
weight is synthetic (spec 060, ADR-008); the screening document is the
committed example's.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pg_support import fresh_database, new_owner, set_session_owner

import harrier.tracker.schema as schema
from harrier.connection import Connection
from harrier.db import connect
from harrier.profile.store import ProfileDocumentError, put_document
from harrier.screening.focused import (
    ScreeningDocumentError,
    blocker_penalty,
    focused_score,
    parse_screening,
    score_bounds,
)
from harrier.screening.normalized import make_normalized_job
from harrier.tracker import list_jobs
from harrier.tracks import default_scope, list_tracks, resolve_scope
from harrier_cli.main import main

SLUG = "bounded"
EXAMPLE = Path(__file__).resolve().parents[3] / "config" / "screening.focused.example.json"

REMOTE_EMEA = "Remote, Europe"
FIGMA_POSTING = "Build interfaces from Figma designs with React and TypeScript."


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def focused(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Path]:
    """A focused track with no screening document yet."""
    assert main(["tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded roles"]) == 0
    capsys.readouterr()
    yield data_dir


@pytest.fixture()
def configured(focused: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[Path]:
    """A focused track holding the committed example as its screening document."""
    assert main(["--track", SLUG, "profile", "put", "screening", "--file", str(EXAMPLE)]) == 0
    capsys.readouterr()
    yield focused


def example() -> dict[str, object]:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def add(
    company: str,
    *,
    title: str = "Junior Frontend Developer",
    location: str = REMOTE_EMEA,
    description: str = FIGMA_POSTING,
    slug: str | None = SLUG,
) -> int:
    argv = [] if slug is None else ["--track", slug]
    argv += ["add", "--company", company, "--title", title, "--location", location]
    argv += ["--url", f"https://jobs.example.com/{company.lower().replace(' ', '-')}"]
    argv += ["--description", description]
    return main(argv)


def rows(slug: str | None = SLUG) -> list[dict[str, str]]:
    conn = connect()
    try:
        scope = default_scope(conn) if slug is None else resolve_scope(conn, slug)
        return list_jobs(conn, scope)
    finally:
        conn.close()


def row_for(company: str, slug: str | None = SLUG) -> dict[str, str]:
    found = [row for row in rows(slug) if row["company"] == company]
    assert len(found) == 1, found
    return found[0]


# --- the kind ------------------------------------------------------------------


def test_a_focused_track_can_be_added(focused: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tracks", "list"]) == 0
    listed = capsys.readouterr().out.splitlines()
    assert any(SLUG in line and "focused" in line for line in listed), listed


def test_a_second_focused_track_is_refused(
    focused: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "add", "other", "--kind", "focused", "--label", "Other"]) == 2
    assert f"track {SLUG} is the focused track" in capsys.readouterr().err
    conn = connect()
    try:
        assert [track.slug for track in list_tracks(conn)] == ["job", SLUG]
    finally:
        conn.close()
    # An archived focused track does not count.
    assert main(["tracks", "archive", SLUG]) == 0
    assert main(["tracks", "add", "other", "--kind", "focused", "--label", "Other"]) == 0


# --- the score on add ------------------------------------------------------------


def test_focused_add_scores_with_the_tracks_document(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert add("Example Studio") == 0
    row = row_for("Example Studio")
    signals = row["signals"].split("|")
    assert signals[0] == "scorer=focused"
    assert "bonus=level" in signals
    assert "bonus=figma" in signals
    assert not any(signal.startswith("gate=") for signal in signals), signals
    # Base 30. No exact title: "Junior Frontend Developer" is not one of the
    # titles and does not start with one. One title keyword ("frontend") 6,
    # remote 10, region 8, the level bonus 8, the figma bonus 8.
    assert int(row["fit_score"]) == 30 + 6 + 10 + 8 + 8 + 8
    assert row["status"] == "prospect"
    assert row["scoring_version"] not in {"", "unknown"}


def test_a_penalty_ranks_and_never_rejects(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert add("Example Studio") == 0
    assert add("Other Studio", description=f"{FIGMA_POSTING} Includes an on-call rotation.") == 0
    plain = row_for("Example Studio")
    paged = row_for("Other Studio")
    assert int(plain["fit_score"]) - int(paged["fit_score"]) == 15
    assert "penalty=on-call" in paged["signals"].split("|")
    assert paged["status"] == "prospect"


def test_focused_add_names_a_failed_gate(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A manual add lands whatever its gates say, as on the industry track
    (spec 137, amendment of 2026-10-14); the failed gate is named."""
    assert add("Office Studio", location="Amsterdam (On-site)") == 0
    row = row_for("Office Studio")
    assert "gate=location says hybrid/on-site" in row["signals"].split("|")
    assert row["status"] == "prospect"


def test_a_blocked_posting_ranks_below_every_unblocked_one() -> None:
    document = parse_screening(example())
    low, _ = score_bounds(document)
    job = make_normalized_job(
        source="manual",
        company="Example Corp",
        title="Junior Frontend Developer",
        location=REMOTE_EMEA,
        url="https://jobs.example.com/blocked",
        description=f"{FIGMA_POSTING} This is a W-2 position.",
        source_label="manual:example corp",
    )
    scored = focused_score(job, document)
    assert any(reason.startswith("blocker=employment") for reason in scored.reasons)
    assert scored.score < low
    assert blocker_penalty(document) > 0


def test_focused_add_without_configuration_is_refused(
    focused: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert add("Example Studio") == 2
    out = capsys.readouterr().out
    assert f"track {SLUG} has no screening configuration" in out
    assert f"harrier --track {SLUG} profile put screening --file PATH" in out
    assert rows() == []


# --- the screening document --------------------------------------------------------


def test_a_malformed_screening_document_is_refused(
    focused: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = example()
    penalties = document["penalties"]
    assert isinstance(penalties, list)
    penalties[2]["points"] = -4
    bad = tmp_path / "screening.json"
    bad.write_text(json.dumps(document), encoding="utf-8")
    assert main(["--track", SLUG, "profile", "put", "screening", "--file", str(bad)]) == 2
    assert "penalties[2].points" in capsys.readouterr().err
    # Nothing was stored, so an add is still refused for want of one.
    assert add("Example Studio") == 2


def test_an_unknown_key_is_refused() -> None:
    document = example()
    document["scoring_extra"] = {}
    with pytest.raises(ScreeningDocumentError) as refused:
        parse_screening(document)
    assert refused.value.path == "scoring_extra"


def test_screening_is_owned_by_a_focused_track_only(
    focused: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["profile", "put", "screening", "--file", str(EXAMPLE)]) == 2
    assert "a screening document belongs to a focused track" in capsys.readouterr().err
    conn = connect()
    try:
        with pytest.raises(ProfileDocumentError):
            put_document(
                conn,
                "screening",
                "screening.json",
                "json",
                "{}",
                track_id=default_scope(conn).track.id,
            )
        stored = conn.execute(
            "SELECT count(*) FROM profile_documents WHERE kind = 'screening'"
        ).fetchone()
        assert stored[0] == 0
    finally:
        conn.close()


def test_an_archived_focused_track_refuses_its_screening_document(
    focused: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "archive", SLUG]) == 0
    capsys.readouterr()
    assert main(["--track", SLUG, "profile", "put", "screening", "--file", str(EXAMPLE)]) == 2
    assert "archived" in capsys.readouterr().err


# --- what later specs open -----------------------------------------------------------


HELD: list[tuple[list[str], str]] = [
    (["discover"], "spec 139"),
    (["tailor", "--job-id", "1"], "spec 140"),
    (["cover-letter", "--job-id", "1"], "spec 140"),
    (["answers", "--job-id", "1", "--question", "Why this role?"], "spec 140"),
]


def test_focused_refuses_what_later_specs_open(
    configured: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv, opened_by in HELD:
        assert main(["--track", SLUG, *argv]) == 2, argv
        err = capsys.readouterr().err
        assert f"is not available on a focused track until {opened_by}" in err, (argv, err)


# --- the migration -------------------------------------------------------------------


def _tracks(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT id, slug, kind, label, created_at, archived_at FROM tracks ORDER BY id"
    ).fetchall()


def test_the_kind_migration_keeps_every_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tracker.db"
    real = list(schema.MIGRATIONS)
    with monkeypatch.context() as before:
        before.setattr(schema, "MIGRATIONS", [m for m in real if m[0] < 20])
        connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = ON")
    with raw:
        raw.execute(
            "INSERT INTO tracks (slug, kind, label, archived_at) "
            "VALUES ('old-search', 'academic', 'Old', '2026-01-01 00:00:00')"
        )
        raw.execute("INSERT INTO tracks (slug, kind, label) VALUES ('lab', 'academic', 'Lab')")
        raw.execute(
            "INSERT INTO profile_documents (kind, name, format, content, track_id) "
            "VALUES ('resume_framing', 'academic.json', 'json', '{}', 3)"
        )
        raw.execute(
            "INSERT INTO jobs (company, title, url, status, track_id) "
            "VALUES ('Example Lab', 'Research Engineer', 'https://calls.example.com/1', "
            "'prospect', 3)"
        )
    kept = _tracks(raw)
    raw.close()

    connect(path).close()

    raw = sqlite3.connect(path)
    raw.execute("PRAGMA foreign_keys = ON")
    try:
        assert _tracks(raw) == kept
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        assert raw.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert raw.execute("SELECT max(version) FROM schema_version").fetchone() == (20,)
        triggers = {
            row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
        }
        assert {
            "tracks_are_never_deleted",
            "tracks_keep_their_id",
            "jobs_track_must_exist_on_insert",
            "jobs_track_must_exist_on_update",
        } <= triggers
        with raw:
            raw.execute("INSERT INTO tracks (slug, kind, label) VALUES ('f', 'focused', 'F')")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            raw.execute("DELETE FROM tracks WHERE slug = 'f'")
        with pytest.raises(sqlite3.IntegrityError, match="unknown track"):
            raw.execute(
                "INSERT INTO jobs (company, title, url, status, track_id) "
                "VALUES ('A', 'B', 'https://jobs.example.com/x', 'prospect', 99)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO tracks (slug, kind, label) VALUES ('g', 'other', 'G')")
    finally:
        raw.close()


def test_the_kind_migration_keeps_every_track_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skips locally without a test server; fails in CI without one."""
    import psycopg

    from harrier.pgstore import migrate_postgres

    real = list(schema.POSTGRES_MIGRATIONS)
    with fresh_database() as url:
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [m for m in real if m[0] < 20])
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            owner = new_owner(conn)
            set_session_owner(conn, owner)
            conn.execute(
                "INSERT INTO tracks (slug, kind, label, archived_at) "
                "VALUES ('old-search', 'academic', 'Old', '2026-01-01 00:00:00')"
            )
            conn.execute("INSERT INTO tracks (slug, kind, label) VALUES ('lab', 'academic', 'Lab')")
            query = (
                "SELECT owner_id::text, id, slug, kind, label, created_at, archived_at "
                "FROM tracks ORDER BY owner_id, id"
            )
            kept = conn.execute(query).fetchall()
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", real)
        assert migrate_postgres(url)[1] >= 20
        with psycopg.connect(url, autocommit=True) as conn:
            assert conn.execute(query).fetchall() == kept
            set_session_owner(conn, owner)
            conn.execute("INSERT INTO tracks (slug, kind, label) VALUES ('f', 'focused', 'F')")
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute("INSERT INTO tracks (slug, kind, label) VALUES ('g', 'other', 'G')")


# --- the industry track ----------------------------------------------------------------


def _industry_run(
    data: Path, monkeypatch: pytest.MonkeyPatch, *, with_focused: bool
) -> tuple[str, str, str]:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(data))
    if with_focused:
        assert main(["tracks", "add", SLUG, "--kind", "focused", "--label", "Bounded"]) == 0
        assert main(["--track", SLUG, "profile", "put", "screening", "--file", str(EXAMPLE)]) == 0
    assert add("Industry Co", title="Senior Frontend Engineer", slug=None) == 0
    row = row_for("Industry Co", slug=None)
    return row["fit_score"], row["signals"], row["scoring_version"]


def test_the_industry_track_is_unchanged_by_a_focused_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    without = _industry_run(tmp_path / "a", monkeypatch, with_focused=False)
    capsys.readouterr()
    assert main(["next"]) == 0
    assert main(["review"]) == 0
    printed_without = capsys.readouterr().out

    with_focused = _industry_run(tmp_path / "b", monkeypatch, with_focused=True)
    capsys.readouterr()
    assert main(["next"]) == 0
    assert main(["review"]) == 0
    printed_with = capsys.readouterr().out

    assert with_focused == without
    assert printed_with == printed_without


def test_focused_rows_stay_in_their_own_track(configured: Path) -> None:
    assert add("Example Studio") == 0
    assert rows(None) == []
    conn: Connection = connect()
    try:
        assert len(list_jobs(conn, resolve_scope(conn, SLUG))) == 1
    finally:
        conn.close()
