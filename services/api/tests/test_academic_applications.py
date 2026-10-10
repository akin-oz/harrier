"""An academic track writes its own CV, letter and answers through the same
gates (spec 101): the wiring that opens the three commands on it.

The prompt, CV shape and page-gate pieces are in test_academic_documents.py.
Everything here is synthetic: the committed examples, invented posting text
and an invented track slug.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pg_support import fresh_database, new_owner, set_session_owner
from resume_support import REPO_ROOT, example_bundle_raw, store_example
from test_apply_claims import (
    CHECKOUT,
    CHECKOUT_EVIDENCE,
    COMPANY,
    INVOICES,
    POSTING,
    ROLE,
    TRUTH,
    candidate,
    letter_json,
)

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
import harrier.resume.pdf as pdf_module
from harrier.apply.claims import ClaimCheckError
from harrier.apply.profile import load_profile_markdown
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume.markdown import UnverifiedClaimError
from harrier.resume.tailor import run_tailor
from harrier.tracker import add_job, get_job, schema
from harrier.tracks import Scope, add_track, default_scope, resolve_scope
from harrier_cli.main import main

SLUG = "lab"
CONFIG = REPO_ROOT / "config"
INDUSTRY_SIGN = "Industry positioning that only the default track holds."
ACADEMIC_SIGN = "Academic positioning that only the lab track holds."


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(REPO_ROOT)
    conn = connect()
    yield conn
    conn.close()


def _pages(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        stdout = f"Producer: stub\nPages:          {count}\n"
        return subprocess.CompletedProcess(args=["pdfinfo"], returncode=0, stdout=stdout)

    monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def store_candidate(conn: sqlite3.Connection) -> None:
    candidate_document = {"candidate": {"name": "Deniz Örnek", "location": "Exampleland"}}
    put_document(conn, "candidate", "candidate.json", "json", json.dumps(candidate_document))


def profile_pair(sign: str) -> tuple[str, str]:
    markdown = (CONFIG / "application-profile.example.md").read_text(encoding="utf-8")
    profile = json.loads((CONFIG / "application-profile.example.json").read_text(encoding="utf-8"))
    profile["core_positioning"]["identity"] = sign
    return f"{markdown}\n{sign}\n", json.dumps(profile)


def store_profile(conn: sqlite3.Connection, track_id: int, sign: str) -> None:
    markdown, profile = profile_pair(sign)
    put_document(
        conn,
        "application_profile",
        "application-profile.md",
        "markdown",
        markdown,
        track_id=track_id,
    )
    put_document(
        conn, "application_profile", "application-profile.json", "json", profile, track_id=track_id
    )


def lab(conn: sqlite3.Connection, *, truth: str | None = None) -> Scope:
    """The industry documents on the default track, the academic examples on
    a lab track, one truth for both."""
    store_example(conn)
    store_candidate(conn)
    store_profile(conn, 1, INDUSTRY_SIGN)
    add_track(conn, SLUG, "academic", "Lab search")
    scope = resolve_scope(conn, SLUG)
    framing = (CONFIG / "resume-framing.academic.example.json").read_text(encoding="utf-8")
    put_document(conn, "resume_framing", "academic.json", "json", framing, track_id=scope.track.id)
    store_profile(conn, scope.track.id, ACADEMIC_SIGN)
    pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
    body = "\n".join(pool.values()) if truth is None else truth
    put_document(conn, "resume_truth", "truth.md", "markdown", body)
    return scope


def call(conn: sqlite3.Connection, scope: Scope) -> int:
    return add_job(
        conn,
        {
            "company": "Example University",
            "title": "Research Software Engineer",
            "url": "https://calls.example.test/7",
            "source": "manual",
            "status": "shortlisted",
        },
        scope=scope,
    )


# --- the CV ----------------------------------------------------------------------


def test_academic_tailor_uses_the_track_framing_and_shape(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = lab(db)
    job_id = call(db, scope)
    _pages(monkeypatch, 2)
    result = run_tailor(
        db,
        scope,
        job_id,
        jd_text="A research post.",
        no_ai=True,
        output_dir=tmp_path / "cv",
        render=_fake_render,
    )
    markdown = result.markdown_path.read_text(encoding="utf-8")
    headings = [line for line in markdown.splitlines() if line.startswith("## ")]
    assert headings.index("## EDUCATION") < headings.index("## SELECTED ACHIEVEMENTS")
    assert "Research Software Engineer" in markdown.splitlines()[1]
    assert get_job(db, scope, job_id)["status"] == "tailored_cv_requested"


def test_academic_cv_refuses_an_unsupported_bullet(
    db: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = lab(db, truth="- A line that supports no bullet.\n")
    job_id = call(db, scope)
    _pages(monkeypatch, 2)
    with pytest.raises(UnverifiedClaimError) as refused:
        run_tailor(
            db,
            scope,
            job_id,
            jd_text="A research post.",
            no_ai=True,
            output_dir=tmp_path / "cv",
            render=_fake_render,
        )
    pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
    assert refused.value.bullet_id in pool
    assert get_job(db, scope, job_id)["status"] == "shortlisted"


# --- the letter and the answers --------------------------------------------------


def _sent(monkeypatch: pytest.MonkeyPatch, module: object, response: str) -> list[str]:
    seen: list[str] = []

    def fake(system_prompt: str, user_input: str) -> str:
        seen.append(user_input)
        return response

    monkeypatch.setattr(module, "generate_text", fake)
    return seen


def test_academic_documents_read_only_their_track(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = lab(db, truth=TRUTH)
    letters = _sent(monkeypatch, letters_module, letter_json())
    letters_module.generate_cover_letter(db, COMPANY, ROLE, jd_text=POSTING, scope=scope)
    claims = [candidate(CHECKOUT, CHECKOUT_EVIDENCE)]
    answers = _sent(
        monkeypatch,
        answers_module,
        json.dumps(
            {
                "answers": [
                    {
                        "question": "What relevant experience do you have?",
                        "short_answer": "The checkout work is close to this.",
                        "medium_answer": CHECKOUT,
                        "notes": [],
                        "claims": claims,
                    }
                ]
            }
        ),
    )
    answers_module.generate_answer_set(
        db,
        COMPANY,
        ROLE,
        ["What relevant experience do you have?"],
        jd_text=POSTING,
        scope=scope,
    )
    for payload in (*letters, *answers):
        assert ACADEMIC_SIGN in payload
        assert INDUSTRY_SIGN not in payload


def test_academic_letters_pass_the_same_claims_checks(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = lab(db, truth=TRUTH)
    sentence = "I ran the research group's data platform."
    claims = [candidate(sentence, "Ran the research group's data platform")]
    _sent(monkeypatch, letters_module, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    with pytest.raises(ClaimCheckError) as refused:
        letters_module.generate_cover_letter(db, COMPANY, ROLE, jd_text=POSTING, scope=scope)
    assert any("unverified evidence" in violation for violation in refused.value.violations)


def test_a_track_with_no_application_profile_is_refused(db: sqlite3.Connection) -> None:
    store_example(db)
    store_candidate(db)
    put_document(db, "resume_truth", "truth.md", "markdown", TRUTH)
    add_track(db, SLUG, "academic", "Lab search")
    scope = resolve_scope(db, SLUG)
    with pytest.raises(ValueError, match=f"track {SLUG} has no application profile"):
        letters_module.generate_cover_letter(db, COMPANY, ROLE, jd_text=POSTING, scope=scope)


# --- migration 12 ----------------------------------------------------------------


def test_migration_12_owns_the_application_profile(data_dir_path: Path) -> None:
    path = data_dir_path / "tracker.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in schema.MIGRATIONS:
        if version >= 12:
            continue
        for statement in statements:
            raw.execute(statement)
        raw.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    shared = [
        ("application_profile", "application-profile.md", "markdown", "# P\r\n", "2026-01-01"),
        ("application_profile", "application-profile.json", "json", "{}", "2026-01-02"),
        ("resume_truth", "truth.md", "markdown", "- A fact.\n", "2026-01-03"),
    ]
    raw.executemany(
        "INSERT INTO profile_documents (kind, name, format, content, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        shared,
    )
    raw.commit()
    raw.close()

    conn = connect(path)
    try:
        rows = conn.execute(
            "SELECT kind, name, format, content, updated_at, track_id FROM profile_documents "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(row[:5]) for row in rows] == shared
    assert [row[5] for row in rows] == [1, 1, None]
    # The default track reads them as before.
    conn = connect(path)
    try:
        assert load_profile_markdown(conn, default_scope(conn)) == "# P\r\n"
    finally:
        conn.close()


def test_migration_12_owns_the_application_profile_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each owner's application profile goes to that owner's own track 1.
    Skips locally without a test server; fails in CI without one."""
    import psycopg

    from harrier.pgstore import migrate_postgres

    real = list(schema.POSTGRES_MIGRATIONS)
    with fresh_database() as url:
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", [m for m in real if m[0] < 12])
        migrate_postgres(url)
        with psycopg.connect(url, autocommit=True) as conn:
            owners = [new_owner(conn), new_owner(conn)]
            for owner in owners:
                set_session_owner(conn, owner)
                for kind, name in (
                    ("application_profile", "application-profile.md"),
                    ("resume_truth", "truth.md"),
                ):
                    conn.execute(
                        "INSERT INTO profile_documents (kind, name, content) VALUES (%s, %s, %s)",
                        (kind, name, f"{name} of {owner}"),
                    )
        monkeypatch.setattr(schema, "POSTGRES_MIGRATIONS", real)
        assert migrate_postgres(url)[1] >= 12
        with psycopg.connect(url, autocommit=True) as conn:
            found = conn.execute(
                "SELECT owner_id::text, kind, track_id, content FROM profile_documents"
            ).fetchall()
    for owner in owners:
        mine = {kind: (track, content) for who, kind, track, content in found if who == owner}
        assert mine == {
            "application_profile": (1, f"application-profile.md of {owner}"),
            "resume_truth": (None, f"truth.md of {owner}"),
        }


@pytest.fixture()
def data_dir_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


# --- profile put application_profile ---------------------------------------------


def stored(conn: sqlite3.Connection, track_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT name, content FROM profile_documents "
        "WHERE kind = 'application_profile' AND track_id = ?",
        (track_id,),
    ).fetchall()
    return {str(row[0]): str(row[1]) for row in rows}


def test_profile_put_stores_an_application_profile(
    db: sqlite3.Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_track(db, SLUG, "academic", "Lab search")
    track_id = resolve_scope(db, SLUG).track.id
    put = ["--track", SLUG, "profile", "put", "application_profile", "--file"]

    markdown = tmp_path / "profile.md"
    markdown.write_bytes(b"# Application Profile\r\n\r\nResearch positioning.\r\n")
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"core_positioning": {}}), encoding="utf-8")
    assert main([*put, str(markdown)]) == 0
    assert main([*put, str(profile)]) == 0
    out = capsys.readouterr().out
    assert f"stored application_profile/application-profile.md for track {SLUG}" in out
    assert stored(db, track_id) == {
        "application-profile.md": markdown.read_bytes().decode("utf-8"),
        "application-profile.json": profile.read_text(encoding="utf-8"),
    }
    before = stored(db, track_id)

    def refused(name: str, body: str, message: str) -> None:
        bad = tmp_path / name
        bad.write_text(body, encoding="utf-8")
        assert main([*put, str(bad)]) == 1
        assert message in capsys.readouterr().err
        assert stored(db, track_id) == before

    refused("empty.md", "  \n", "application_profile file is empty")
    refused("list.json", "[1, 2]", "is not a JSON object")
    refused("notes.txt", "Research positioning.", "must end in .md or .json")


def test_the_academic_examples_store_and_check(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """The committed academic examples are a valid track: the framing parses
    with the facts and every bullet is in the truth used here."""
    store_example(db)
    pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
    put_document(db, "resume_truth", "truth.md", "markdown", "\n".join(pool.values()))
    add_track(db, SLUG, "academic", "Lab search")
    for kind, name in (
        ("resume_framing", "resume-framing.academic.example.json"),
        ("application_profile", "application-profile.academic.example.md"),
        ("application_profile", "application-profile.academic.example.json"),
    ):
        assert main(["--track", SLUG, "profile", "put", kind, "--file", str(CONFIG / name)]) == 0
    assert main(["--track", SLUG, "profile", "check"]) == 0
    assert f"resume content for track {SLUG}: valid" in capsys.readouterr().out
