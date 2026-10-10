"""`harrier profile split-resume`: one `resume_data` document becomes the
facts and the framing (spec 098).

A dry run by default, printing field paths and never a value, because the
output can reach a run log. `--write` first proves the two documents give
the bundle the one document gave, then writes both and keeps the original
under another kind, in one transaction.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from harrier.profile.store import put_documents_and_rekind
from harrier.resume.content import (
    RESUME_DATA_KIND,
    ResumeBundleError,
    bundle_from_documents,
    parse_bundle,
)
from harrier.resume.documents import (
    FACTS_KIND,
    FACTS_NAME,
    FRAMING_KIND,
    FRAMING_NAME,
    field_paths,
    split_document,
)
from harrier.tracks import DEFAULT_TRACK_ID

PRESPLIT_KIND = "resume_data_presplit"


@dataclass(frozen=True)
class SplitOutcome:
    exit_code: int
    lines: tuple[str, ...]


def _names(conn: sqlite3.Connection, kind: str) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM profile_documents WHERE kind = ? ORDER BY name", (kind,)
    ).fetchall()
    return [str(row[0]) for row in rows]


def _refuse(message: str) -> SplitOutcome:
    return SplitOutcome(1, (message,))


def _dumped(document: dict[str, object]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def split_resume(conn: sqlite3.Connection, *, write: bool) -> SplitOutcome:
    data_names = _names(conn, RESUME_DATA_KIND)
    has_facts = bool(_names(conn, FACTS_KIND))
    has_framing = bool(_names(conn, FRAMING_KIND))
    if data_names and (has_facts or has_framing):
        return _refuse(
            f"both {RESUME_DATA_KIND} and {FACTS_KIND}/{FRAMING_KIND} are stored; keep one shape"
        )
    if not data_names:
        if has_facts and has_framing:
            return SplitOutcome(0, (f"already split: {FACTS_KIND} and {FRAMING_KIND} are stored",))
        return _refuse(f"no {RESUME_DATA_KIND} document to split")
    if len(data_names) > 1:
        return _refuse(f"more than one {RESUME_DATA_KIND} document: {', '.join(data_names)}")
    name = data_names[0]
    if name in _names(conn, PRESPLIT_KIND):
        return _refuse(f"{PRESPLIT_KIND}/{name} is already stored; remove it before splitting")

    row = conn.execute(
        "SELECT content FROM profile_documents WHERE kind = ? AND name = ?",
        (RESUME_DATA_KIND, name),
    ).fetchone()
    try:
        raw: object = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        return _refuse(f"{RESUME_DATA_KIND} document is not valid JSON: {exc}")
    if not isinstance(raw, dict):
        return _refuse(f"{RESUME_DATA_KIND} document is not an object")
    raw = cast("dict[str, object]", raw)
    try:
        original = parse_bundle(raw)
    except ResumeBundleError as exc:
        return _refuse(str(exc))

    facts, framing, unplaced = split_document(raw)
    if unplaced:
        return _refuse("keys the split cannot place: " + ", ".join(unplaced))
    try:
        rebuilt = bundle_from_documents(facts, framing)
    except ResumeBundleError as exc:
        return _refuse(f"split would change the bundle: {exc}")
    for field in dataclasses.fields(original):
        if getattr(original, field.name) != getattr(rebuilt, field.name):
            return _refuse(f"split would change the bundle: {field.name}")

    # The facts are shared; the framing is the default track's (spec 099).
    plan: list[tuple[str, str, dict[str, object], int | None]] = [
        (FACTS_KIND, FACTS_NAME, facts, None),
        (FRAMING_KIND, FRAMING_NAME, framing, DEFAULT_TRACK_ID),
    ]
    if not write:
        lines: list[str] = []
        for kind, document_name, document, _ in plan:
            lines.append(f"{kind}/{document_name}:")
            lines.extend(f"  {path}" for path in field_paths(document))
        lines.append("dry run: nothing written; run with --write to store")
        return SplitOutcome(0, tuple(lines))

    put_documents_and_rekind(
        conn,
        [
            (kind, document_name, "json", _dumped(document), track_id)
            for kind, document_name, document, track_id in plan
        ],
        (RESUME_DATA_KIND, name, PRESPLIT_KIND),
    )
    return SplitOutcome(
        0,
        (
            f"stored {FACTS_KIND}/{FACTS_NAME}",
            f"stored {FRAMING_KIND}/{FRAMING_NAME}",
            f"kept the original as {PRESPLIT_KIND}/{name}",
        ),
    )
