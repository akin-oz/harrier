"""The search tracks over HTTP (spec 094).

The same three functions `harrier tracks list|add|archive` calls, so the
browser and the command line cannot disagree about what a track is or which
verbs it has.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from harrier.tracks import (
    DEFAULT_TRACK_ID,
    KIND_RULES,
    SLUG_MAX_LENGTH,
    TRACK_KINDS,
    AlreadyArchivedError,
    DuplicateTrackError,
    Track,
    TrackRefusedError,
    UnknownTrackError,
    add_track,
    archive_track,
    kind_refusal,
    list_tracks,
)
from harrier_api.deps import SLUG_PATTERN, Conn, DatabaseRoute
from harrier_api.localauth import TOKEN_RESPONSES, require_token

tracks_router = APIRouter(route_class=DatabaseRoute)


class TrackOut(BaseModel):
    id: int
    slug: str
    kind: str
    label: str
    archived: bool
    is_default: bool
    # The kind's word for each of the six statuses, so the browser holds no
    # copy of the labels (spec 093's `KIND_RULES`).
    status_labels: dict[str, str]


class TrackIn(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN, max_length=SLUG_MAX_LENGTH)
    kind: Literal["industry", "academic"]
    label: str = Field(min_length=1, max_length=80)


class TrackKindOut(BaseModel):
    kind: str
    # False when `add_track` refuses a new track of this kind; `reason` is
    # then the domain's own sentence, so the add form never paraphrases it.
    available: bool
    reason: str


TRACK_ERRORS = {
    404: {"description": "no track has that slug"},
    409: {"description": "the rules refuse it"},
    **TOKEN_RESPONSES,
}


def _out(track: Track) -> TrackOut:
    return TrackOut(
        id=track.id,
        slug=track.slug,
        kind=track.kind,
        label=track.label,
        archived=track.archived,
        is_default=track.id == DEFAULT_TRACK_ID,
        status_labels=dict(KIND_RULES[track.kind].labels),
    )


@tracks_router.get("/tracks", operation_id="listTracks")
def get_tracks(conn: Conn) -> list[TrackOut]:
    """Every track, archived ones included, in id order."""
    return [_out(track) for track in list_tracks(conn)]


@tracks_router.get("/tracks/kinds", operation_id="listTrackKinds")
def get_track_kinds() -> list[TrackKindOut]:
    """Each kind a track can have, and whether a new one may be added now."""
    out: list[TrackKindOut] = []
    for kind in TRACK_KINDS:
        refusal = kind_refusal(kind)
        out.append(TrackKindOut(kind=kind, available=refusal is None, reason=refusal or ""))
    return out


@tracks_router.post(
    "/tracks",
    operation_id="addTrack",
    status_code=201,
    dependencies=[Depends(require_token)],
    responses=TRACK_ERRORS,
)
def post_track(body: TrackIn, conn: Conn) -> TrackOut:
    try:
        return _out(add_track(conn, body.slug, body.kind, body.label))
    except (DuplicateTrackError, TrackRefusedError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@tracks_router.post(
    "/tracks/{slug}/archive",
    operation_id="archiveTrack",
    dependencies=[Depends(require_token)],
    responses=TRACK_ERRORS,
)
def post_archive(slug: Annotated[str, Path(pattern=SLUG_PATTERN)], conn: Conn) -> TrackOut:
    try:
        return _out(archive_track(conn, slug))
    except UnknownTrackError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (AlreadyArchivedError, TrackRefusedError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
