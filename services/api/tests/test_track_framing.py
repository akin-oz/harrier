"""An academic track holds its own framing over the shared facts (spec 099).

Synthetic content only: the committed examples, an invented track slug and
invented values.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pg_support import fresh_database, new_owner, set_session_owner
from resume_support import example_bundle_raw, example_documents, store_example

from harrier.db import connect
from harrier.profile.store import (
    ProfileDocumentError,
    export_to,
    get_document,
    put_document,
)
from harrier.resume.content import ResumeBundleError, load_bundle, parse_bundle
from harrier.tracker import schema
from harrier.tracks import default_scope, resolve_scope
from harrier_cli.main import main

SLUG = "lab-search"
ACADEMIC_NAME = "academic.json"
MIGRATION_11 = next(statements for version, statements in schema.MIGRATIONS if version == 11)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def academic(data_dir: Path, capsys: pytest.CaptureFixture[str]) -> Iterator[sqlite3.Connection]:
    assert main(["tracks", "add", SLUG, "--kind", "academic", "--label", "Lab search"]) == 0
    capsys.readouterr()
    conn = connect()
    store_example(conn)
    pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(pool.values()))
    yield conn
    conn.close()


def academic_framing() -> dict[str, Any]:
    _, framing = example_documents()
    framing = copy.deepcopy(framing)
    framing["candidate"]["primary_identity"] = "Research Software Engineer"
    return framing


def rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT id, kind, name, format, content, updated_at, track_id "
            "FROM profile_documents ORDER BY id"
        ).fetchall()
    ]


# --- migration 11 ----------------------------------------------------------------

EARLIER_DOCUMENTS = [
    (1, "resume_truth", "truth.md", "markdown", "- A fact.\n", "2026-01-01 00:00:00"),
    (2, "resume_facts", "resume-facts.json", "json", '{"a": 1}', "2026-01-02 00:00:00"),
    (3, "resume_framing", "industry.json", "json", '{"b": 2}', "2026-01-03 00:00:00"),
    (4, "application_profile", "application-profile.md", "markdown", "x\r\n", "2026-01-04"),
]


def at_version_ten(path: Path) -> None:
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in schema.MIGRATIONS:
        if version >= 11:
            continue
        for statement in statements:
            raw.execute(statement)
        raw.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    raw.executemany(
        "INSERT INTO profile_documents (id, kind, name, format, content, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        EARLIER_DOCUMENTS,
    )
    raw.commit()
    raw.close()


def test_migration_11_keeps_every_document_and_owns_the_framing(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir.mkdir(parents=True)
    path = data_dir / "tracker.db"
    at_version_ten(path)
    # Through 11 only: 12 gives the application profile to a track (spec 101).
    monkeypatch.setattr(schema, "MIGRATIONS", [m for m in schema.MIGRATIONS if m[0] <= 11])
    conn = connect(path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 11
        after = rows(conn)
        assert [row[:6] for row in after] == EARLIER_DOCUMENTS
        assert {row[1]: row[6] for row in after} == {
            "resume_truth": None,
            "resume_facts": None,
            "resume_framing": 1,
            "application_profile": None,
        }
        written = export_to(conn, tmp_path / "export")
    finally:
        conn.close()
    exported = {path.relative_to(tmp_path / "export").as_posix(): path for path in written}
    for _, kind, name, _, content, _ in EARLIER_DOCUMENTS:
        key = f"{kind}/{name}" if kind != "resume_framing" else f"tracks/job/{kind}/{name}"
        assert exported[key].read_bytes() == content.encode("utf-8")


def test_migration_11_owns_the_framing_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Postgres migration gives each owner's framing to that owner's own
    track 1, and shares the rest (spec 103 rule 3, spec 105). Skips locally
    without a test server; fails in CI without one."""
    import psycopg

    from harrier.pgstore import migrate_postgres

    real = list(schema.POSTGRES_MIGRATIONS)
    with fresh_database() as url:
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [m for m in real if m[0] < 11])
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            owners = [new_owner(conn), new_owner(conn)]
            for owner in owners:
                set_session_owner(conn, owner)
                for _, kind, name, fmt, content, updated_at in EARLIER_DOCUMENTS:
                    conn.execute(
                        "INSERT INTO profile_documents (kind, name, format, content, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (kind, name, fmt, content, updated_at),
                    )
        # Through 11 only: 12 gives the application profile to a track (spec 101).
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [m for m in real if m[0] <= 11])
        assert migrate_postgres(url)[1] == 11
        with psycopg.connect(url, autocommit=True) as conn:
            found = conn.execute(
                "SELECT owner_id::text, kind, track_id, content FROM profile_documents"
            ).fetchall()
    for owner in owners:
        mine = {kind: (track_id, content) for who, kind, track_id, content in found if who == owner}
        assert mine == {
            kind: (1 if kind == "resume_framing" else None, content)
            for _, kind, _, _, content, _ in EARLIER_DOCUMENTS
        }


def test_migration_11_is_whole_or_nothing(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir.mkdir(parents=True)
    path = data_dir / "tracker.db"
    at_version_ten(path)
    broken = [
        *[(v, s) for v, s in schema.MIGRATIONS if v < 11],
        (11, [*MIGRATION_11, "THIS IS NOT SQL"]),
    ]
    monkeypatch.setattr(schema, "MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError):
        connect(path)
    raw = sqlite3.connect(path)
    try:
        assert raw.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 10
        columns = [row[1] for row in raw.execute("PRAGMA table_info(profile_documents)")]
        assert "track_id" not in columns
        assert raw.execute("SELECT COUNT(*) FROM profile_documents").fetchone()[0] == len(
            EARLIER_DOCUMENTS
        )
    finally:
        raw.close()


def test_profile_documents_unique_per_owner(academic: sqlite3.Connection) -> None:
    track_id = resolve_scope(academic, SLUG).track.id
    insert = "INSERT INTO profile_documents (kind, name, track_id) VALUES (?, ?, ?)"
    academic.execute(insert, ("note", "same.md", None))
    academic.execute(insert, ("note", "same.md", track_id))
    with pytest.raises(sqlite3.IntegrityError):
        academic.execute(insert, ("note", "same.md", None))
    with pytest.raises(sqlite3.IntegrityError):
        academic.execute(insert, ("note", "same.md", track_id))
    academic.rollback()


def test_only_the_framing_is_owned(academic: sqlite3.Connection) -> None:
    with pytest.raises(ProfileDocumentError, match="owned by a track"):
        put_document(academic, "resume_framing", "industry.json", "json", "{}")
    with pytest.raises(ProfileDocumentError, match="shared by every track"):
        put_document(academic, "candidate", "candidate.json", "json", "{}", track_id=1)


# --- reading ---------------------------------------------------------------------


def test_a_track_reads_only_its_own_framing(academic: sqlite3.Connection) -> None:
    track_id = resolve_scope(academic, SLUG).track.id
    framing = json.dumps(academic_framing())
    put_document(academic, "resume_framing", ACADEMIC_NAME, "json", framing, track_id=track_id)

    industry = load_bundle(academic, default_scope(academic))
    lab = load_bundle(academic, resolve_scope(academic, SLUG))
    assert industry == parse_bundle(example_bundle_raw())
    assert lab.primary_identity == "Research Software Engineer"
    assert industry.primary_identity != lab.primary_identity
    # No reader of a shared kind can see an owned row.
    assert get_document(academic, "resume_framing", ACADEMIC_NAME) is None
    assert get_document(academic, "resume_framing", "industry.json") is None


def test_no_framing_means_no_fallback(academic: sqlite3.Connection) -> None:
    with pytest.raises(ResumeBundleError) as refused:
        load_bundle(academic, resolve_scope(academic, SLUG))
    message = str(refused.value)
    assert f"track {SLUG} has no resume framing" in message
    assert f"harrier --track {SLUG} profile put resume_framing --file PATH" in message


def test_two_framings_on_one_track_are_refused(academic: sqlite3.Connection) -> None:
    put_document(academic, "resume_framing", "second.json", "json", "{}", track_id=1)
    with pytest.raises(ResumeBundleError, match="track job owns more than one resume framing"):
        load_bundle(academic, default_scope(academic))


# --- profile put -----------------------------------------------------------------


def stored_framing(conn: sqlite3.Connection, track_id: int) -> str | None:
    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = 'resume_framing' AND track_id = ?",
        (track_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def test_profile_put_validates_before_it_writes(
    academic: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    track_id = resolve_scope(academic, SLUG).track.id
    put = ["--track", SLUG, "profile", "put", "resume_framing", "--file"]

    # As read, byte for byte: CRLF line endings survive.
    good = tmp_path / "framing.json"
    text = json.dumps(academic_framing(), indent=2).replace("\n", "\r\n")
    good.write_bytes(text.encode("utf-8"))
    assert main([*put, str(good)]) == 0
    assert f"stored resume_framing/{ACADEMIC_NAME} for track {SLUG}" in capsys.readouterr().out
    assert stored_framing(academic, track_id) == text

    def refused(framing: object, message: str) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(framing), encoding="utf-8")
        assert main([*put, str(bad)]) == 1
        assert message in capsys.readouterr().err
        assert stored_framing(academic, track_id) == text

    unknown_role = academic_framing()
    unknown_role["roles"].append({"id": "no-such-role"})
    refused(unknown_role, "names unknown role no-such-role")
    facts_key = academic_framing()
    facts_key["candidate"]["email"] = "someone@example.test"
    refused(facts_key, "candidate.email belongs in resume_facts")
    refused([1, 2], "is not a JSON object")

    academic.execute("DELETE FROM profile_documents WHERE kind = 'resume_facts'")
    academic.commit()
    refused(academic_framing(), "no resume_facts document; run harrier profile split-resume first")

    assert main(["tracks", "archive", SLUG]) == 0
    capsys.readouterr()
    assert main([*put, str(good)]) == 2


# --- profile check ---------------------------------------------------------------


def test_profile_check_reports_bullet_ids_only(
    academic: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["profile", "check"]) == 0
    assert "resume content for track job: valid" in capsys.readouterr().out

    pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
    dropped_id, dropped_text = next(iter(pool.items()))
    kept = [text for bullet_id, text in pool.items() if bullet_id != dropped_id]
    put_document(academic, "resume_truth", "truth.md", "markdown", "\n".join(kept))
    assert main(["profile", "check"]) == 1
    out = capsys.readouterr().out
    assert f"bullet {dropped_id}: not supported by the truth documents" in out
    assert dropped_text not in out

    assert main(["--track", SLUG, "profile", "check"]) == 1
    assert f"track {SLUG} has no resume framing" in capsys.readouterr().out


# --- list and export -------------------------------------------------------------


def test_profile_list_and_export_show_the_owner(
    academic: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    track_id = resolve_scope(academic, SLUG).track.id
    put_document(academic, "resume_framing", ACADEMIC_NAME, "json", "{}", track_id=track_id)
    assert main(["profile", "list"]) == 0
    out = capsys.readouterr().out
    assert f"resume_framing/{ACADEMIC_NAME} (json, updated" in out
    assert f"(track {SLUG})" in out
    assert "resume_facts/resume-facts.json" in out and "(shared)" in out

    written = export_to(academic, tmp_path / "export")
    names = {path.relative_to(tmp_path / "export").as_posix() for path in written}
    assert f"tracks/{SLUG}/resume_framing/{ACADEMIC_NAME}" in names
    assert "tracks/job/resume_framing/industry.json" in names
    assert "resume_facts/resume-facts.json" in names
