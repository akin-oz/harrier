"""Search tracks: which search a tracker row belongs to (spec 091, ADR-012).

A track is a second kind of search by the same person, held as a row in
`tracks`; a tenant is a second person, held as a store boundary this module
never sees. The write path stamps every row with the track of the `Scope` it
is given, a value passed down from the entry point that opened the
connection, never a module global, so two scopes in one process cannot mix.
Readers are not scoped yet: `list_jobs` and the rest still return every row,
which is correct while every row is in the default track. Spec 092 makes
every reader take a `Scope` too.

Migration 8 seeds the one track every existing row belongs to. Spec 093
adds the two verbs a track has, `add_track` and `archive_track`, and the
rules each kind brings: status labels, next-action defaults, whether marking
a row applied seeds a follow-up, and how the queue is ordered. Nothing
renames a track or changes its kind.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from harrier import voices
from harrier.connection import Connection, Row, StoreIntegrityError, store_now

TrackKind = Literal["industry", "academic", "focused"]

# The kinds that have a policy. The `tracks.kind` CHECK derives from this
# tuple the way the status CHECK derives from STATUSES, in the latest
# migration to widen it (20, spec 137); adding a kind is a migration plus the
# code that kind needs, never a runtime insert.
TRACK_KINDS: tuple[TrackKind, ...] = ("industry", "academic", "focused")

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


class DuplicateTrackError(TrackError):
    pass


class TrackRefusedError(TrackError):
    """A track verb the rules refuse: a second industry track, archiving the
    default track or one already archived (spec 093)."""


class AlreadyArchivedError(TrackRefusedError):
    """Archiving a track that is already archived. Its own type, so a caller
    tells a repeated archive from a refused one by class, never by wording
    (review of PR #182)."""


class ArchivedTrackError(TrackError):
    """A write to a track that is archived: it reads and does not write
    (spec 093)."""


def refuse_if_archived(conn: Connection, scope: Scope) -> None:
    """The one check every write to a track makes. The CLI refuses earlier
    with its own exit status; this holds for every other caller (review of
    PR #182).

    It reads the track's row as the database holds it now, not the flag the
    scope was resolved with: a scope kept from before the track was archived
    must not write to it (review of PR #227)."""
    refuse_archived_track(conn, scope.track.id)


def refuse_archived_track(conn: Connection, track_id: int) -> None:
    row = conn.execute("SELECT slug, archived_at FROM tracks WHERE id = ?", (track_id,)).fetchone()
    if row is not None and row[1]:
        raise ArchivedTrackError(f"track {row[0]} is archived; it reads but does not write")


QueueOrder = Literal["stage_then_score", "nearest_deadline"]
ScreeningGates = Literal["industry", "academic"]

# The CV's section headings, in the order the industry CV has always had
# them. A kind's `cv_sections` is a permutation of these (spec 101);
# `harrier.resume.markdown` refuses any other.
INDUSTRY_CV_SECTIONS: tuple[str, ...] = (
    "## PROFILE",
    "## SELECTED ACHIEVEMENTS",
    "## EXPERIENCE",
    "## EDUCATION",
    "## CERTIFICATIONS",
    "## TECHNICAL SKILLS",
)

# A section only a kind that lists it renders (spec 140): the framing's
# `ai_bullets`. Every kind's `cv_sections` is the six above in some order,
# with this one or without it.
AI_CV_SECTION = "## AI-ASSISTED ENGINEERING"


@dataclass(frozen=True)
class KindRules:
    """What a track's kind changes, and nothing more (spec 093).

    The six statuses are shared by every kind; a kind supplies only the words
    the operator reads, the next action each status suggests, whether marking
    a row applied seeds a follow-up and the outreach block, and the order the
    queue shows rows in.

    Spec 101 adds the application documents: the voice of each prompt (the
    truth rules and return formats are shared, beside their generators), the
    CV's section order and template, the page counts the PDF gate allows,
    and the letter shape the letter check holds.
    """

    labels: Mapping[str, str] = field(default_factory=dict[str, str])
    next_action: Mapping[str, str] = field(default_factory=dict[str, str])
    seeds_follow_up: bool = True
    queue: QueueOrder = "stage_then_score"
    # Which gates `screen_jobs` applies to this kind's discovery (spec 097).
    # Industry: today's gates, unchanged. Academic: the search entry's gates,
    # with no industry title hint, remote gate, hold list or score.
    screening: ScreeningGates = "industry"
    # The voice and structure segments each prompt wraps around its shared
    # block (`harrier.voices.assemble`).
    tailor_voice: tuple[str, ...] = voices.INDUSTRY_TAILOR_VOICE
    letter_voice: tuple[str, ...] = voices.INDUSTRY_LETTER_VOICE
    answers_voice: tuple[str, ...] = voices.INDUSTRY_ANSWERS_VOICE
    cv_sections: tuple[str, ...] = INDUSTRY_CV_SECTIONS
    cv_template: str = "resume-template.html"
    # The page counts the PDF gate accepts, for the CV and the letter alike.
    pages: tuple[int, ...] = (1,)
    # The letter's paragraph count and word cap when no brief states them.
    # None means the kind has none, and its length is the page gate's.
    letter_paragraphs: int | None = 3
    letter_max_words: int | None = 240
    # Spec 140: the letter's template; the most sentences the CV's profile
    # may have (None: no limit); and whether tailoring writes the
    # missing-keywords sidecar.
    letter_template: str = "cover-letter-template.html"
    profile_max_sentences: int | None = None
    missing_keywords: bool = False
    # The CV header names email, LinkedIn, GitHub and the facts' links, and
    # the framing's entity line under them, with no location or phone.
    header_links: bool = False


KIND_RULES: dict[str, KindRules] = {
    # The next actions are the old repo's (scripts/jobs.py
    # NEXT_ACTION_DEFAULTS), which `harrier.tracker.schema.NEXT_ACTION_DEFAULTS`
    # now derives from. The labels are the words the browser has always shown
    # (spec 094 moved them here so the browser holds no copy); the command
    # line prints the stored status on this kind, so its output is unchanged.
    "industry": KindRules(
        labels={
            "prospect": "prospect",
            "shortlisted": "shortlisted",
            "tailored_cv_requested": "CV requested",
            "applied": "applied",
            "interviewing": "interviewing",
            "rejected": "rejected",
        },
        next_action={
            "prospect": "review and decide whether to apply",
            "shortlisted": "request tailored CV and review before applying",
            "tailored_cv_requested": "review tailored PDF before applying",
            "applied": "follow up if no reply within 7 days",
            "interviewing": "prepare for interview",
            "rejected": "",
        },
        seeds_follow_up=True,
        queue="stage_then_score",
    ),
    # An application to a call rather than to a job posting: no follow-up
    # cadence, no outreach, and the deadline is what orders the queue.
    "academic": KindRules(
        labels={
            "prospect": "found",
            "shortlisted": "shortlisted",
            "tailored_cv_requested": "preparing documents",
            "applied": "submitted",
            "interviewing": "interviewing",
            "rejected": "closed",
        },
        next_action={
            "prospect": "read the call and decide whether to apply",
            "shortlisted": "prepare the application documents",
            "tailored_cv_requested": "finish the documents before the deadline",
            "applied": "wait for a reply",
            "interviewing": "prepare for the interview",
            "rejected": "",
        },
        seeds_follow_up=False,
        queue="nearest_deadline",
        screening="academic",
        # A committee reads closely: its own voice, education before
        # achievements, and one or two pages with no word count (spec 101).
        tailor_voice=voices.ACADEMIC_TAILOR_VOICE,
        letter_voice=voices.ACADEMIC_LETTER_VOICE,
        answers_voice=voices.ACADEMIC_ANSWERS_VOICE,
        cv_sections=(
            "## PROFILE",
            "## EDUCATION",
            "## SELECTED ACHIEVEMENTS",
            "## EXPERIENCE",
            "## CERTIFICATIONS",
            "## TECHNICAL SKILLS",
        ),
        cv_template="resume-template-academic.html",
        pages=(1, 2),
        letter_paragraphs=None,
        letter_max_words=None,
    ),
}
# A search for roles with a bounded scope, split from the industry search by
# the ownership a posting demands (spec 137). It reads its rows as the
# industry kind does: the same labels, next actions, follow-up and queue. Its
# own score comes from the screening document the track owns, and its
# documents arrive with spec 140.
KIND_RULES["focused"] = replace(
    KIND_RULES["industry"],
    # A senior engineer choosing a smaller scope on purpose (spec 140): its own
    # voices, a header with links and an entity line, an AI-assisted section,
    # a six-paragraph letter with a salutation, and a sidecar naming the
    # posting's skills the truth does not hold.
    tailor_voice=voices.FOCUSED_TAILOR_VOICE,
    letter_voice=voices.FOCUSED_LETTER_VOICE,
    answers_voice=voices.FOCUSED_ANSWERS_VOICE,
    cv_sections=(
        "## PROFILE",
        "## SELECTED ACHIEVEMENTS",
        "## EXPERIENCE",
        AI_CV_SECTION,
        "## TECHNICAL SKILLS",
        "## EDUCATION",
        "## CERTIFICATIONS",
    ),
    cv_template="resume-template-focused.html",
    pages=(1,),
    letter_paragraphs=6,
    letter_max_words=320,
    letter_template="cover-letter-template-focused.html",
    profile_max_sentences=3,
    missing_keywords=True,
    header_links=True,
)


# What a non-default track may run, by operation name (spec 093, moved here by
# spec 094 so the CLI and the API read one list). Operation names are the CLI
# subcommands, plus the two reads the browser needs that the CLI reaches
# through `next` and `review`: `list` (the track's rows) and `counts`.
NON_DEFAULT_OPERATIONS: frozenset[str] = frozenset(
    {
        "add",
        "tracks list",
        "tracks add",
        "tracks archive",
        "next",
        "review",
        "shortlist",
        "track",
        "applied",
        "interviewing",
        "reject",
        "events show",
        "export",
        "list",
        "counts",
        # Spec 097: discovery from the track's own search entry, and
        # reconsideration of its own seen state.
        "discover",
        "reconsider",
        # Spec 099: the track's own resume framing over the shared facts.
        "profile put",
        "profile check",
        # Spec 101: application documents from the track's own framing and
        # application profile, through the same truth and claims gates.
        "tailor",
        "cover-letter",
        "answers",
        # Spec 138: a row moves between an industry and a focused track.
        "move",
        # Spec 149: a description is posting data, whatever track holds the row.
        "description show",
        "description set",
    }
)

# The allowed operations that write a tracker row; an archived track refuses them.
WRITE_OPERATIONS: frozenset[str] = frozenset(
    {
        "add",
        "shortlist",
        "track",
        "applied",
        "interviewing",
        "reject",
        "discover",
        "profile put",
        # Spec 079: a CV that passes its gate moves the row.
        "tailor",
        "move",
    }
)


# Operations a kind may not run yet, each with the spec that opens it
# (spec 137). The allowlist above is per track; this is per kind, and it
# empties as those specs land.
HELD_OPERATIONS: dict[str, dict[str, str]] = {}

# Operations a kind never runs on its own track, each with the sentence that
# says where it runs instead. A focused track's postings are found by the
# industry track's discovery run, which places each one (spec 139).
ELSEWHERE_OPERATIONS: dict[str, dict[str, str]] = {
    "focused": {
        "discover": "the focused track is discovered by the shared run: harrier discover",
    },
}


def held_refusal(kind: str, operation: str) -> str | None:
    """Why a track of this kind may not run this operation, or None: either
    the spec that opens it (spec 137) or where it runs instead (spec 139)."""
    elsewhere = ELSEWHERE_OPERATIONS.get(kind, {}).get(operation)
    if elsewhere is not None:
        return elsewhere
    opened_by = HELD_OPERATIONS.get(kind, {}).get(operation)
    if opened_by is None:
        return None
    return f"{operation} is not available on a {kind} track until {opened_by}"


def operation_refusal(scope: Scope, operation: str) -> str | None:
    """Why this scope may not run this operation, or None when it may.

    One rule for both surfaces: the CLI prints it and exits 2, the API
    answers 409 with it (specs 093, 094).
    """
    if scope.track.id != DEFAULT_TRACK_ID and operation not in NON_DEFAULT_OPERATIONS:
        return f"{operation} is not available on track {scope.track.slug}"
    held = held_refusal(scope.track.kind, operation)
    if held is not None:
        return held
    if scope.track.archived and operation in WRITE_OPERATIONS:
        return f"track {scope.track.slug} is archived"
    return None


def rules_for(kind: str) -> KindRules:
    return KIND_RULES[kind]


def status_label(kind: str, status: str) -> str:
    """The word the operator reads for a status on a track of this kind. The
    stored status is unchanged; this is display text (spec 093)."""
    return KIND_RULES[kind].labels.get(status, status)


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
    # fullmatch: `match` with `$` would accept a trailing newline that the
    # database CHECK refuses (review of PR #180).
    if not slug or len(slug) > SLUG_MAX_LENGTH or _SLUG.fullmatch(slug) is None:
        raise InvalidSlugError(f"invalid track slug {slug!r}; a slug is {SLUG_RULE}")
    return slug


_COLUMNS = "id, slug, kind, label, created_at, archived_at"


def _track_from_row(row: Row | tuple[object, ...]) -> Track:
    values = tuple(row)
    return Track(
        id=int(str(values[0])),
        slug=str(values[1]),
        kind=str(values[2]),
        label=str(values[3]),
        created_at=str(values[4]),
        archived_at=str(values[5] or ""),
    )


def list_tracks(conn: Connection) -> list[Track]:
    """Every track, archived ones included, in id order."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM tracks ORDER BY id").fetchall()
    return [_track_from_row(row) for row in rows]


def resolve_scope(conn: Connection, slug: str | None) -> Scope:
    """The scope a slug names; `None` names the default track."""
    if slug is None:
        return default_scope(conn)
    validate_slug(slug)
    row = conn.execute(f"SELECT {_COLUMNS} FROM tracks WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise UnknownTrackError(f"unknown track {slug!r}; `harrier tracks list` shows them")
    return Scope(track=_track_from_row(row))


def kind_refusal(kind: str) -> str | None:
    """Why a new track of this kind is refused, or None when it may be added.

    `add_track` raises with these words, and the browser's add form shows them
    beside the option it disables, so both surfaces give one reason (spec 094).
    """
    if kind not in TRACK_KINDS:
        return f"unknown track kind {kind!r}; kinds: {', '.join(TRACK_KINDS)}"
    if kind == "industry":
        return (
            "a second industry track is not supported yet: it would share the one "
            "candidate configuration and watchlist with the first"
        )
    return None


def add_track(conn: Connection, slug: str, kind: str, label: str) -> Track:
    """Create a track (spec 093).

    A second industry track is refused: it would share the one candidate
    configuration and the one watchlist, which is the same search twice. That
    refusal lifts once configuration is per track. A second unarchived
    focused track is refused for the same reason (spec 137).
    """
    validate_slug(slug)
    refusal = kind_refusal(kind)
    if refusal is not None:
        raise TrackRefusedError(refusal)
    label = label.strip()
    if not label:
        raise TrackRefusedError("a track needs a label")
    try:
        with conn.transaction():
            if kind == "focused":
                _refuse_a_second_focused_track(conn)
            conn.execute(
                "INSERT INTO tracks (slug, kind, label) VALUES (?, ?, ?)", (slug, kind, label)
            )
    except StoreIntegrityError as error:
        # Two constraints can refuse the insert: the slug's, and the one
        # unarchived focused track's partial index (migration 20), which
        # holds when two adds race past the check above (review of PR #332).
        # Told apart by what the store holds now, never by the message.
        taken = conn.execute("SELECT 1 FROM tracks WHERE slug = ?", (slug,)).fetchone()
        if kind == "focused" and taken is None:
            _refuse_a_second_focused_track(conn)
        raise DuplicateTrackError(f"a track with slug {slug!r} already exists") from error
    return resolve_scope(conn, slug).track


def _refuse_a_second_focused_track(conn: Connection) -> None:
    """One install has one bounded-role search, as it has one industry search
    (spec 137). An archived focused track does not count. Read inside the
    insert's transaction."""
    row = conn.execute(
        "SELECT slug FROM tracks WHERE kind = 'focused' AND archived_at IS NULL ORDER BY id"
    ).fetchone()
    if row is not None:
        raise TrackRefusedError(
            f"a second focused track is not supported: track {row[0]} is the focused track"
        )


def archive_track(conn: Connection, slug: str) -> Track:
    """Archive a track: it still lists and reads, and refuses writes (spec 093).
    The default track cannot be archived, and archiving is one-way."""
    track = resolve_scope(conn, slug).track
    if track.id == DEFAULT_TRACK_ID:
        raise TrackRefusedError("the default track cannot be archived")
    if track.archived:
        raise AlreadyArchivedError(f"track {slug!r} is already archived")
    with conn.transaction():
        conn.execute("UPDATE tracks SET archived_at = ? WHERE id = ?", (store_now(), track.id))
    return resolve_scope(conn, slug).track


def default_scope(conn: Connection) -> Scope:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM tracks WHERE id = ?", (DEFAULT_TRACK_ID,)
    ).fetchone()
    if row is None:
        # Migration 8 seeds it; a database without it was not opened through
        # `harrier.db.connect`, which is the only path that migrates.
        raise UnknownTrackError("the default track is missing; the database was not migrated")
    return Scope(track=_track_from_row(row))
