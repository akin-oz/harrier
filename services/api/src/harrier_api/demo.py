"""Demo mode: boot against a throwaway seeded database (spec 005).

A stranger runs `just demo` with no personal database present; the app seeds
synthetic rows from fixtures/demo-jobs.json (public class per the
classification table) into a temp-dir database at startup.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from harrier.db import connect
from harrier.tracker import add_job

FIXTURE_ENV = "HARRIER_DEMO_FIXTURE"
_DEMO_DIR_NAME = "harrier-demo"


def is_demo_mode() -> bool:
    return os.environ.get("HARRIER_DEMO", "") == "1"


def demo_db_path() -> Path:
    return Path(tempfile.gettempdir()) / _DEMO_DIR_NAME / "demo.db"


def default_fixture_path() -> Path:
    override = os.environ.get(FIXTURE_ENV, "")
    if override:
        return Path(override)
    # repo_root/fixtures/demo-jobs.json relative to this file:
    # services/api/src/harrier_api/demo.py -> repo root is four parents up.
    return Path(__file__).resolve().parents[4] / "fixtures" / "demo-jobs.json"


def seed_demo_db(fixture: Path | None = None) -> Path:
    """Create a fresh demo database from the fixture. Idempotent per boot."""
    db_path = demo_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (db_path, db_path.with_suffix(".db-wal"), db_path.with_suffix(".db-shm")):
        stale.unlink(missing_ok=True)

    fixture_path = fixture if fixture is not None else default_fixture_path()
    raw = json.loads(fixture_path.read_text(encoding="utf-8"))
    jobs: list[dict[str, str]] = [
        {str(key): str(value) for key, value in entry.items()} for entry in raw
    ]

    conn = connect(db_path)
    try:
        for job in jobs:
            add_job(conn, job)
    finally:
        conn.close()
    return db_path
