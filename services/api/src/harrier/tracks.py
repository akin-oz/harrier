"""Search tracks: which search a tracker row belongs to (spec 091, ADR-012).

A track is a second kind of search by the same person, held as a row in
`tracks`; a tenant is a second person, held as a store boundary this module
never sees. Every reader and writer of tracker rows works in a `Scope`, a
value passed down from the entry point that opened the connection, never a
module global, so two scopes in one process cannot mix.

Nothing here creates, archives or renames a track. Migration 8 seeds the one
track every existing row belongs to; spec 093 adds the verbs.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Literal

TrackKind = Literal["industry", "academic"]

# The kinds that have a policy. The `tracks.kind` CHECK in migration 8 derives
# from this tuple the way the status CHECK derives from STATUSES; adding a
# kind is a migration plus the code that kind needs, never a runtime insert.
TRACK_KINDS: tuple[TrackKind, ...] = ("industry", "academic")

# Seeded by migration 8. Its slug and label are rows, not constants; only its
# id is fixed, because every row that existed before tracks lands in it.
DEFAULT_TRACK_ID = 1

SLUG_MAX_LENGTH = 32
_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")
SLUG_RULE = (
    f"one to {SLUG_MAX_LENGTH} characters: a lowercase letter first, then lowercase "
    "letters, digits and hyphens"
)


class TrackError(Exception):
    pass


class UnknownTrackError(TrackError):
    pass


class InvalidSlugError(TrackError):
    pass


@dataclass(frozen=True)
class Track:
    id: int
    slug: str
    kind: str
    label: str
    created_at: str
    archived_at: str

    @property
    def archived(self) -> bool:
        return bool(self.archived_at)


@dataclass(frozen=True)
class Scope:
    """The track a command or request works in.

    Its own type, not a bare `Track`, so that signatures do not change again
    when per-track configuration lands and a scope also says which
    configuration rows apply.
    """

    track: Track


def validate_slug(slug: str) -> str:
    """The slug rule, in code with a message; the CHECK in the database is
    the backstop that says the same thing without one."""
    if not slug or len(slug) > SLUG_MAX_LENGTH or _SLUG.match(slug) is None:
        raise InvalidSlugError(f"invalid track slug {slug!r}; a slug is {SLUG_RULE}")
    return slug


_COLUMNS = "id, slug, kind, label, created_at, archived_at"


def _track_from_row(row: sqlite3.Row | tuple[object, ...]) -> Track:
    values = tuple(row)
    return Track(
        id=int(str(values[0])),
        slug=str(values[1]),
        kind=str(values[2]),
        label=str(values[3]),
        created_at=str(values[4]),
        archived_at=str(values[5] or ""),
    )


def list_tracks(conn: sqlite3.Connection) -> list[Track]:
    """Every track, archived ones included, in id order."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM tracks ORDER BY id").fetchall()
    return [_track_from_row(row) for row in rows]


def resolve_scope(conn: sqlite3.Connection, slug: str | None) -> Scope:
    """The scope a slug names; `None` names the default track."""
    if slug is None:
        return default_scope(conn)
    validate_slug(slug)
    row = conn.execute(f"SELECT {_COLUMNS} FROM tracks WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise UnknownTrackError(f"unknown track {slug!r}; `harrier tracks list` shows them")
    return Scope(track=_track_from_row(row))


def default_scope(conn: sqlite3.Connection) -> Scope:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (DEFAULT_TRACK_ID,)
    ).fetchone()
    if row is None:
        # Migration 8 seeds it; a database without it was not opened through
        # `harrier.db.connect`, which is the only path that migrates.
        raise UnknownTrackError("the default track is missing; the database was not migrated")
    return Scope(track=_track_from_row(row))
