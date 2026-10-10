"""A track's own framing over the shared facts (spec 099), and its own
application profile (spec 101).

`profile put resume_framing --file PATH` stores the scope track's framing
after checking it against the shared facts. `profile check` reports, for the
scope's track, whether the resume content is valid and which bullets the
truth documents support. Neither prints a field value or a bullet's text.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from harrier.apply.profile import APPLICATION_PROFILE_KIND
from harrier.profile.store import put_document
from harrier.resume.content import (
    ResumeBundleError,
    bundle_from_documents,
    framing_name,
    load_bundle,
    load_facts,
    load_truth_sources,
)
from harrier.resume.documents import FRAMING_KIND
from harrier.tracks import Scope


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int
    lines: tuple[str, ...]


def put_framing(conn: sqlite3.Connection, scope: Scope, text: str) -> CommandOutcome:
    """Store `text` as the scope track's framing, as read, once the facts and
    it parse as one bundle. Any refusal writes nothing."""
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as exc:
        return CommandOutcome(1, (f"{FRAMING_KIND} file is not valid JSON: {exc}",))
    if not isinstance(raw, dict):
        return CommandOutcome(1, (f"{FRAMING_KIND} file is not a JSON object",))
    try:
        bundle_from_documents(load_facts(conn), cast("dict[str, object]", raw))
    except ResumeBundleError as exc:
        return CommandOutcome(1, (str(exc),))
    name = framing_name(scope)
    slug = scope.track.slug
    put_document(conn, FRAMING_KIND, name, "json", text, track_id=scope.track.id)
    return CommandOutcome(
        0,
        (
            f"stored {FRAMING_KIND}/{name} for track {slug}",
            f"run harrier --track {slug} profile check to see which bullets the truth supports",
        ),
    )


# A track's application profile, by the file's extension (spec 101).
PROFILE_FILES = {
    ".md": ("application-profile.md", "markdown"),
    ".json": ("application-profile.json", "json"),
}


def put_application_profile(
    conn: sqlite3.Connection, scope: Scope, suffix: str, text: str
) -> CommandOutcome:
    """Store `text` as the scope track's application profile, as read. The
    extension picks the document; JSON must be an object and markdown must
    not be empty. Any refusal writes nothing."""
    if suffix.lower() not in PROFILE_FILES:
        return CommandOutcome(1, ("application_profile file must end in .md or .json",))
    name, fmt = PROFILE_FILES[suffix.lower()]
    if fmt == "json":
        try:
            raw: object = json.loads(text)
        except json.JSONDecodeError as exc:
            return CommandOutcome(1, (f"application_profile file is not valid JSON: {exc}",))
        if not isinstance(raw, dict):
            return CommandOutcome(1, ("application_profile file is not a JSON object",))
    elif not text.strip():
        return CommandOutcome(1, ("application_profile file is empty",))
    slug = scope.track.slug
    put_document(conn, APPLICATION_PROFILE_KIND, name, fmt, text, track_id=scope.track.id)
    return CommandOutcome(0, (f"stored {APPLICATION_PROFILE_KIND}/{name} for track {slug}",))


def check_resume(conn: sqlite3.Connection, scope: Scope) -> CommandOutcome:
    """Validate the scope's bundle and run the truth gate on every bullet in
    its pool. Bullet ids only, never their text."""
    try:
        bundle = load_bundle(conn, scope)
        sources = load_truth_sources(conn)
    except ResumeBundleError as exc:
        return CommandOutcome(1, (str(exc),))
    unsupported = [
        bullet_id for bullet_id, text in bundle.bullet_pool.items() if not sources.contains(text)
    ]
    lines = [f"resume content for track {scope.track.slug}: valid"]
    lines.extend(
        f"bullet {bullet_id}: not supported by the truth documents" for bullet_id in unsupported
    )
    return CommandOutcome(1 if unsupported else 0, tuple(lines))
