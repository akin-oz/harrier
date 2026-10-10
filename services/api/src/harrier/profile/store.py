"""Profile document storage and the one-shot import from the old repo.

Documents are stored as-is; structured schemas and validation arrive with the
specs that consume them (013+). Export must reproduce imported files
byte-identically (spec 004 acceptance).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from harrier.tracks import DEFAULT_TRACK_ID

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


# The kinds a track owns (specs 099, 101). Every other kind is shared by every
# track. The write path holds this rule rather than a CHECK, so a later spec
# can widen it without rebuilding the table.
TRACK_OWNED_KINDS: frozenset[str] = frozenset({"resume_framing", "application_profile"})


class ProfileDocumentError(ValueError):
    pass


# One upsert per owner, each naming the partial index it can conflict on
# (migration 10): NULL is shared, and SQLite treats NULLs as distinct.
_UPSERT_SHARED = """
    INSERT INTO profile_documents (kind, name, format, content, updated_at)
    VALUES (?, ?, ?, ?, datetime('now'))
    ON CONFLICT (kind, name) WHERE track_id IS NULL DO UPDATE SET
        format = excluded.format,
        content = excluded.content,
        updated_at = datetime('now')
"""
_UPSERT_OWNED = """
    INSERT INTO profile_documents (track_id, kind, name, format, content, updated_at)
    VALUES (?, ?, ?, ?, ?, datetime('now'))
    ON CONFLICT (track_id, kind, name) WHERE track_id IS NOT NULL DO UPDATE SET
        format = excluded.format,
        content = excluded.content,
        updated_at = datetime('now')
"""


def _upsert(
    conn: sqlite3.Connection, kind: str, name: str, fmt: str, content: str, track_id: int | None
) -> None:
    if kind in TRACK_OWNED_KINDS and track_id is None:
        raise ProfileDocumentError(f"{kind} is owned by a track; it cannot be shared")
    if kind not in TRACK_OWNED_KINDS and track_id is not None:
        raise ProfileDocumentError(f"{kind} is shared by every track; it cannot be owned")
    if track_id is None:
        conn.execute(_UPSERT_SHARED, (kind, name, fmt, content))
    else:
        conn.execute(_UPSERT_OWNED, (track_id, kind, name, fmt, content))


def put_document(
    conn: sqlite3.Connection,
    kind: str,
    name: str,
    fmt: str,
    content: str,
    *,
    track_id: int | None = None,
) -> None:
    """Write one document: shared when `track_id` is None, else owned by that
    track. Which kinds may be owned is `TRACK_OWNED_KINDS` (spec 099)."""
    with conn:
        _upsert(conn, kind, name, fmt, content, track_id)


def put_documents_and_rekind(
    conn: sqlite3.Connection,
    documents: Sequence[tuple[str, str, str, str, int | None]],
    rekind: tuple[str, str, str],
) -> None:
    """Write each `(kind, name, format, content, track_id)` and move the shared
    row named by `(kind, name)` to the third value's kind, in one
    transaction: all of it or none of it (spec 098). The moved row keeps its
    name, content and `updated_at`."""
    old_kind, name, new_kind = rekind
    with conn:
        for kind, document_name, fmt, content, track_id in documents:
            _upsert(conn, kind, document_name, fmt, content, track_id)
        conn.execute(
            "UPDATE profile_documents SET kind = ? "
            "WHERE kind = ? AND name = ? AND track_id IS NULL",
            (new_kind, old_kind, name),
        )


def get_document(conn: sqlite3.Connection, kind: str, name: str) -> str | None:
    """A shared document. No reader of a shared kind sees an owned row."""
    # Positional access: works with any row factory, not only harrier.db.connect's.
    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = ? AND name = ? AND track_id IS NULL",
        (kind, name),
    ).fetchone()
    return str(row[0]) if row is not None else None


def owned_documents(conn: sqlite3.Connection, kind: str, track_id: int) -> list[tuple[str, str]]:
    """Every `(name, content)` of this kind that the track owns, by name."""
    rows = conn.execute(
        "SELECT name, content FROM profile_documents WHERE kind = ? AND track_id = ? ORDER BY name",
        (kind, track_id),
    ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def list_documents(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Every document, with its owner: `shared`, or `track <slug>`."""
    rows = conn.execute(
        "SELECT p.kind, p.name, p.format, p.updated_at, t.slug "
        "FROM profile_documents p LEFT JOIN tracks t ON t.id = p.track_id "
        "ORDER BY p.kind, p.name, t.slug"
    ).fetchall()
    return [
        {
            "kind": str(row[0]),
            "name": str(row[1]),
            "format": str(row[2]),
            "updated_at": str(row[3]),
            "owner": "shared" if row[4] is None else f"track {row[4]}",
        }
        for row in rows
    ]


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
        # The old system had one search, so an owned kind is the default
        # track's (specs 099, 101).
        track_id = DEFAULT_TRACK_ID if kind in TRACK_OWNED_KINDS else None
        put_document(conn, kind, source.name, fmt, _read_exact(source), track_id=track_id)
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
    """Write every document byte-identical to import: a shared one to
    dest/<kind>/<name>, a track's own to dest/tracks/<slug>/<kind>/<name>
    (spec 099)."""
    written: list[Path] = []
    rows = conn.execute(
        "SELECT p.kind, p.name, p.content, t.slug "
        "FROM profile_documents p LEFT JOIN tracks t ON t.id = p.track_id"
    ).fetchall()
    for row in rows:
        kind, name, content = (str(row[0]), str(row[1]), str(row[2]))
        base = dest if row[3] is None else dest / "tracks" / str(row[3])
        target = base / kind / name
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_exact(target, content)
        written.append(target)
    return sorted(written)
