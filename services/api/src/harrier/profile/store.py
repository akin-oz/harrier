"""Profile document storage and the one-shot import from the old repo.

Documents are stored as-is; structured schemas and validation arrive with the
specs that consume them (013+). Export must reproduce imported files
byte-identically (spec 004 acceptance).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Old-repo-relative path -> (kind, format). Read-only sources.
PROFILE_SOURCES: dict[str, tuple[str, str]] = {
    "config/candidate.json": ("candidate", "json"),
    "config/resume-candidate-data.json": ("resume_data", "json"),
    "config/resume-truth-source.md": ("resume_truth", "markdown"),
    "config/latest-project-achievements.md": ("achievements", "markdown"),
    "config/application-profile.md": ("application_profile", "markdown"),
    "config/application-profile.json": ("application_profile", "json"),
    "config/outreach/defaults.json": ("outreach_defaults", "json"),
}

INTERVIEW_PREP_DIR = "interview-prep"


def put_document(conn: sqlite3.Connection, kind: str, name: str, fmt: str, content: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO profile_documents (kind, name, format, content, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT (kind, name) DO UPDATE SET
                format = excluded.format,
                content = excluded.content,
                updated_at = datetime('now')
            """,
            (kind, name, fmt, content),
        )


def get_document(conn: sqlite3.Connection, kind: str, name: str) -> str | None:
    # Positional access: works with any row factory, not only harrier.db.connect's.
    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = ? AND name = ?", (kind, name)
    ).fetchone()
    return str(row[0]) if row is not None else None


def list_documents(conn: sqlite3.Connection) -> list[dict[str, str]]:
    columns = ("kind", "name", "format", "updated_at")
    rows = conn.execute(
        f"SELECT {', '.join(columns)} FROM profile_documents ORDER BY kind, name"
    ).fetchall()
    return [dict(zip(columns, (str(value) for value in row), strict=True)) for row in rows]


def _format_for(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return {"md": "markdown", "json": "json"}.get(suffix, "text")


def _read_exact(path: Path) -> str:
    # newline="" disables universal-newline translation so CRLF content
    # round-trips byte-identically (spec 004 acceptance).
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_exact(path: Path, content: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def import_from(conn: sqlite3.Connection, old_root: Path) -> tuple[list[str], list[str]]:
    """Read the old repo's profile files (read-only) into profile_documents.

    Returns (imported descriptions, missing paths).
    """
    imported: list[str] = []
    missing: list[str] = []

    for rel_path, (kind, fmt) in PROFILE_SOURCES.items():
        source = old_root / rel_path
        if not source.is_file():
            missing.append(rel_path)
            continue
        put_document(conn, kind, source.name, fmt, _read_exact(source))
        imported.append(f"{kind}/{source.name} <- {rel_path}")

    prep_dir = old_root / INTERVIEW_PREP_DIR
    if prep_dir.is_dir():
        for source in sorted(prep_dir.iterdir()):
            if not source.is_file() or source.name.startswith("."):
                continue
            put_document(
                conn,
                "interview_prep",
                source.name,
                _format_for(source),
                _read_exact(source),
            )
            imported.append(f"interview_prep/{source.name} <- {INTERVIEW_PREP_DIR}/{source.name}")
    else:
        missing.append(INTERVIEW_PREP_DIR)

    return imported, missing


def export_to(conn: sqlite3.Connection, dest: Path) -> list[Path]:
    """Write every document to dest/<kind>/<name>, byte-identical to import."""
    written: list[Path] = []
    rows = conn.execute("SELECT kind, name, content FROM profile_documents").fetchall()
    for row in rows:
        kind, name, content = (str(row[0]), str(row[1]), str(row[2]))
        target = dest / kind / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_exact(target, content)
        written.append(target)
    return sorted(written)
