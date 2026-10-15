"""A track's own framing over the shared facts (spec 099), and its own
application profile (spec 101).

`profile put resume_framing --file PATH` stores the scope track's framing
after checking it against the shared facts. `profile check` reports, for the
scope's track, whether the resume content is valid and which bullets the
truth documents support. Neither prints a field value or a bullet's text.
`profile put resume_facts|resume_truth --file PATH` stores a shared
document once no track's check gains a failure from it (spec 144).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from harrier.apply.profile import APPLICATION_PROFILE_KIND
from harrier.connection import Connection, by_code_point
from harrier.profile.store import put_document
from harrier.resume.content import (
    RESUME_TRUTH_KIND,
    ResumeBundleError,
    bundle_from_documents,
    framing_name,
    load_bundle,
    load_facts,
    load_truth_sources,
)
from harrier.resume.documents import FACTS_KIND, FACTS_NAME, FRAMING_KIND
from harrier.tracker.store import lock_tracks
from harrier.tracks import Scope, refuse_if_archived, rules_for


@dataclass(frozen=True)
class CommandOutcome:
    exit_code: int
    lines: tuple[str, ...]


def put_framing(conn: Connection, scope: Scope, text: str) -> CommandOutcome:
    """Store `text` as the scope track's framing, as read, once the facts and
    it parse as one bundle. Any refusal writes nothing."""
    # The CLI refuses an archived track first; this holds for every other
    # caller (review of PR #219).
    refuse_if_archived(conn, scope)
    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as exc:
        return CommandOutcome(1, (f"{FRAMING_KIND} file is not valid JSON: {exc}",))
    if not isinstance(raw, dict):
        return CommandOutcome(1, (f"{FRAMING_KIND} file is not a JSON object",))
    try:
        bundle = bundle_from_documents(load_facts(conn), cast("dict[str, object]", raw))
    except ResumeBundleError as exc:
        return CommandOutcome(1, (str(exc),))
    limit = rules_for(scope.track.kind).profile_max_sentences
    if limit is not None:
        # The profile the CV opens with: the lead, the experience sentence and
        # the summary (spec 140). Its skill phrase changes per posting; its
        # sentence count does not.
        from harrier.resume.plan import build_profile, sentence_count

        count = sentence_count(build_profile(bundle, list(bundle.positioning_technologies)))
        if count > limit:
            return CommandOutcome(
                1,
                (
                    f"the {scope.track.kind} CV profile has {count} sentences, at most "
                    f"{limit}: profile_lead, the experience statement and profile_summary",
                ),
            )
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
    conn: Connection, scope: Scope, suffix: str, text: str
) -> CommandOutcome:
    """Store `text` as the scope track's application profile, as read. The
    extension picks the document; JSON must be an object and markdown must
    not be empty. Any refusal writes nothing."""
    refuse_if_archived(conn, scope)
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


def check_resume(conn: Connection, scope: Scope) -> CommandOutcome:
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
    lines = [_valid_line(scope)]
    lines.extend(
        f"bullet {bullet_id}: not supported by the truth documents" for bullet_id in unsupported
    )
    return CommandOutcome(1 if unsupported else 0, tuple(lines))


# The shared documents `profile put` stores (spec 144): the format and the
# name a first copy takes. An existing copy keeps its name.
SHARED_RESUME_DOCUMENTS = {
    FACTS_KIND: ("json", FACTS_NAME),
    RESUME_TRUTH_KIND: ("markdown", "resume-truth-source.md"),
}


class _EditBreaksATrack(Exception):
    """Raised inside the write's transaction, so the write rolls back."""

    def __init__(self, lines: tuple[str, ...]) -> None:
        super().__init__("the edit breaks a track's resume content")
        self.lines = lines


def put_shared_resume_document(conn: Connection, kind: str, text: str) -> CommandOutcome:
    """Store `text` as the shared `resume_facts` or `resume_truth`, as read
    (spec 144). Refused, exit 2 and nothing stored, when the facts are not a
    JSON object, the truth is empty, or the new document breaks a track whose
    resume content loaded before it: the content no longer loads, or a
    bullet the truth supported is no longer supported. A track whose content
    did not load before (no facts yet, no framing) cannot be broken by it
    (amendment 2)."""
    fmt, first_name = SHARED_RESUME_DOCUMENTS[kind]
    if fmt == "json":
        try:
            raw: object = json.loads(text)
        except json.JSONDecodeError as exc:
            return CommandOutcome(2, (f"{kind} file is not valid JSON: {exc}",))
        if not isinstance(raw, dict):
            return CommandOutcome(2, (f"{kind} file is not a JSON object",))
    elif not text.strip():
        return CommandOutcome(2, (f"{kind} file is empty",))
    try:
        with conn.transaction(immediate=True):
            # Every track, archived ones too, read under the lock: two shared
            # edits at once each check against the other's result.
            scopes = [Scope(track) for track in lock_tracks(conn)]
            before = {scope.track.slug: _check_state(conn, scope) for scope in scopes}
            name = _shared_name(conn, kind) or first_name
            put_document(conn, kind, name, fmt, text)
            broken = tuple(
                f"track {scope.track.slug}: {line}"
                for scope in scopes
                for line in _broken(before[scope.track.slug], _check_state(conn, scope))
            )
            if broken:
                raise _EditBreaksATrack(broken)
    except _EditBreaksATrack as refused:
        return CommandOutcome(
            2, (f"{kind} not stored: it breaks the resume content of a track", *refused.lines)
        )
    return CommandOutcome(0, (f"stored {kind}/{name}",))


def _shared_name(conn: Connection, kind: str) -> str | None:
    """The name of the shared copy readers take: the first by code point
    (`_document_by_kind` in `harrier.resume.content`)."""
    row = conn.execute(
        "SELECT name FROM profile_documents WHERE kind = ? AND track_id IS NULL "
        f"ORDER BY {by_code_point(conn, 'name')} LIMIT 1",
        (kind,),
    ).fetchone()
    return str(row[0]) if row is not None else None


@dataclass(frozen=True)
class _CheckState:
    """A track's `profile check`: whether its content loaded, and the lines
    that report a failure (the load error, or each unsupported bullet)."""

    loaded: bool
    failures: frozenset[str]


def _check_state(conn: Connection, scope: Scope) -> _CheckState:
    outcome = check_resume(conn, scope)
    valid = _valid_line(scope)
    loaded = outcome.lines[:1] == (valid,)
    return _CheckState(loaded, frozenset(line for line in outcome.lines if line != valid))


def _broken(before: _CheckState, after: _CheckState) -> list[str]:
    """What the edit broke in one track: nothing unless its content loaded
    before; then every failure line it did not have."""
    if not before.loaded:
        return []
    return sorted(after.failures - before.failures)


def _valid_line(scope: Scope) -> str:
    return f"resume content for track {scope.track.slug}: valid"
