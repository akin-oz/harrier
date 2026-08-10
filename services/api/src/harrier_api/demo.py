"""Demo mode seeding: a throwaway database filled from public fixtures.

The switch and its two substitutions live in harrier.demo (spec 021); this
module is the API-side seeding that runs at startup. A stranger with no
personal database, no config files, and no keys gets a temp-dir database
holding synthetic tracker rows and the synthetic profile documents the
committed examples carry, so the resume, cover letter, outreach, and offer
surfaces have something real-shaped to work on.

The seeded persona is fictional (config/*.example.*, spec 013 onward); the
same files are the public test fixtures, so a broken example breaks CI.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

from harrier.db import DB_FILENAME, connect, data_dir
from harrier.demo import is_demo_mode, repo_root
from harrier.profile import put_document
from harrier.tracker import add_job

FIXTURE_ENV = "HARRIER_DEMO_FIXTURE"

# Committed example -> the (kind, name, format) its consumer reads. The kinds
# match the constants in the consuming modules (harrier.apply.profile,
# harrier.resume.content, harrier.offers.stories, harrier.outreach.messages).
# The demo has no personal documents to import, so this is the whole store.
PROFILE_SEEDS: tuple[tuple[str, str, str, str], ...] = (
    ("config/candidate.example.json", "candidate", "candidate.json", "json"),
    (
        "config/application-profile.example.json",
        "application_profile",
        "application-profile.json",
        "json",
    ),
    (
        "config/application-profile.example.md",
        "application_profile",
        "application-profile.md",
        "markdown",
    ),
    ("config/resume-content.example.json", "resume_data", "resume-candidate-data.json", "json"),
    ("config/story-seeds.example.json", "story_seeds", "story-seeds.json", "json"),
    ("config/outreach-defaults.example.json", "outreach_defaults", "defaults.json", "json"),
)

__all__ = [
    "FIXTURE_ENV",
    "PROFILE_SEEDS",
    "default_fixture_path",
    "demo_db_path",
    "is_demo_mode",
    "seed_demo_db",
]


def demo_db_path() -> Path:
    """Where the demo database lives. Follows data_dir(), which is the demo
    temp directory under HARRIER_DEMO, so the database and the state written
    beside it never end up in different places."""
    return data_dir() / DB_FILENAME


# Directories a discovery run writes beside the database. seed_demo_db drops
# them: without that, the second boot serves a freshly wiped tracker whose
# sources all report every posting as already seen, so a stranger pressing
# "Run discovery" watches a successful run add nothing (spec 021).
DERIVED_STATE_DIRS = ("discovery", "descriptions", "incoming", "runs")


def default_fixture_path() -> Path:
    override = os.environ.get(FIXTURE_ENV, "")
    if override:
        return Path(override)
    return repo_root() / "fixtures" / "demo-jobs.json"


def seed_profile_documents(conn: sqlite3.Connection) -> list[str]:
    """Load every committed example into the profile store. Returns the
    relative paths that were missing, which test_demo.py asserts is empty."""
    root = repo_root()
    missing: list[str] = []
    for relative, kind, name, fmt in PROFILE_SEEDS:
        path = root / relative
        if not path.is_file():
            missing.append(relative)
            continue
        put_document(conn, kind, name, fmt, path.read_text(encoding="utf-8"))
    return missing


def seed_demo_db(fixture: Path | None = None) -> Path:
    """Create a fresh demo database from the fixtures. Idempotent per boot."""
    db_path = demo_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (db_path, db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        stale.unlink(missing_ok=True)
    for name in DERIVED_STATE_DIRS:
        shutil.rmtree(db_path.parent / name, ignore_errors=True)

    fixture_path = fixture if fixture is not None else default_fixture_path()
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    jobs: list[dict[str, str]] = [
        {str(key): str(value) for key, value in entry.items()} for entry in raw
    ]

    conn = connect(db_path)
    try:
        for job in jobs:
            add_job(conn, job)
        seed_profile_documents(conn)
    finally:
        conn.close()
    return db_path
