"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from harrier.db import connect
from harrier.tracks import (
    SLUG_MAX_LENGTH,
    Scope,
    UnknownTrackError,
    default_scope,
    operation_refusal,
    resolve_scope,
)
from harrier_api.demo import demo_db_path, is_demo_mode


def get_conn() -> Iterator[sqlite3.Connection]:
    # same_thread=False: FastAPI runs this dependency and the endpoint on
    # different threadpool threads, so sqlite3's same-thread check fires
    # intermittently under concurrent requests even though this connection
    # only ever serves one request and is closed below.
    conn = connect(demo_db_path() if is_demo_mode() else None, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def get_scope(conn: Conn) -> Scope:
    """The default track, resolved once per request (spec 092). Only the
    capture routes use it: a bookmarklet names no track, so they write to the
    default one and take no `track` parameter (spec 094)."""
    return default_scope(conn)


ScopeDep = Annotated[Scope, Depends(get_scope)]

# The status route's operation is the verb in its body, which a dependency
# cannot read; the route checks it with `require_operation` (spec 094).
VERB_IN_BODY = "the verb in the request body"

# A track named in a query string follows the slug rule (spec 091).
SLUG_PATTERN = rf"^[a-z][a-z0-9-]{{0,{SLUG_MAX_LENGTH - 1}}}$"


def require_operation(scope: Scope, operation: str) -> None:
    """409 when this track may not run this operation (specs 093, 094)."""
    refusal = operation_refusal(scope, operation)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


def scope_for(operation: str) -> Callable[..., Scope]:
    """The scope dependency for a route that runs `operation` (spec 094).

    It adds the optional `track` query parameter to the route, resolves the
    track once per request, and refuses the operation on a track that may not
    run it, before the route reads a row. A route that reads or writes
    tracker rows declares its operation through this, and a test walks every
    route to hold that.
    """

    def dependency(
        conn: Conn,
        track: Annotated[
            str | None,
            Query(
                pattern=SLUG_PATTERN,
                description="The search track to work in; the default track when omitted.",
            ),
        ] = None,
    ) -> Scope:
        try:
            scope = resolve_scope(conn, track)
        except UnknownTrackError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if operation != VERB_IN_BODY:
            require_operation(scope, operation)
        return scope

    dependency.__name__ = f"scope_for_{operation.replace(' ', '_').replace('-', '_')}"
    setattr(dependency, "track_operation", operation)  # noqa: B010 - read by the route walk
    return dependency


# --- refusal bodies ---


# FastAPI already sends this body for an `HTTPException`. Declaring it is what
# lets the generated client know a refusal has one, so the browser reads
# `detail` through a generated type rather than a cast (spec 082). The raise
# and this declaration are separate lines, so
# `tests/test_ui_tracker.py::test_every_tracker_refusal_is_the_body_the_contract_declares`
# provokes each refusal and holds the two together. It lives here so the
# route class below can declare the track refusals with it (spec 094).
class ErrorOut(BaseModel):
    """The body of a refusal: the message the domain wrote, verbatim."""

    detail: str


# What `scope_for` can answer on every route that takes `track`: an unknown
# slug, and an operation the track may not run or an archived track's write.
TRACK_REFUSAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut, "description": "the track parameter named no track"},
    409: {
        "model": ErrorOut,
        "description": "the track may not run this operation, or it is archived",
    },
}


def _track_operation(dependant: Dependant) -> str | None:
    operation = getattr(dependant.call, "track_operation", None)
    if isinstance(operation, str):
        return operation
    for child in dependant.dependencies:
        found = _track_operation(child)
        if found is not None:
            return found
    return None


# --- the 503 while a host process holds the database (spec 075) ---


class DatabaseHoldOut(BaseModel):
    """Which host process holds the database, and since when. The subcommand
    name only, never an argument value."""

    subcommand: str
    since: str


class DatabaseHeldOut(BaseModel):
    detail: str
    hold: DatabaseHoldOut


DATABASE_HELD_DETAIL = (
    "A host process holds the tracker database. This request is refused until it ends."
)

DATABASE_HELD_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "model": DatabaseHeldOut,
        "description": "A host process holds the tracker database (spec 075).",
    }
}


def _needs_conn(dependant: Dependant) -> bool:
    if dependant.call is get_conn:
        return True
    return any(_needs_conn(child) for child in dependant.dependencies)


def depends_on_conn(
    path: str, endpoint: Callable[..., Any], dependencies: Sequence[params.Depends] | None
) -> bool:
    """Whether a route opens the database, directly or through a dependency."""
    if _needs_conn(get_dependant(path=path, call=endpoint)):
        return True
    return any(
        dependency.dependency is not None
        and _needs_conn(get_dependant(path=path, call=dependency.dependency))
        for dependency in dependencies or ()
    )


class DatabaseRoute(APIRoute):
    """A route that declares the 503 itself when it depends on `Conn`, and
    the track refusals when it takes its scope from `scope_for`.

    Declared by the route class rather than in each decorator, so a new route
    that opens the database or names a track cannot forget them, and one that
    does not never claims them (specs 075, 094). A route's own declaration of
    a status wins: the tracker writes keep their 404 wording (spec 082).
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        own: dict[int | str, dict[str, Any]] = dict(kwargs.get("responses") or {})
        declared: dict[int | str, dict[str, Any]] = {}
        if depends_on_conn(path, endpoint, kwargs.get("dependencies")):
            declared.update(DATABASE_HELD_RESPONSES)
        if _track_operation(get_dependant(path=path, call=endpoint)) is not None:
            # Merged per status: a route that already declares a 404 keeps
            # its wording, and gains the body model if it named none, since
            # every refusal here is the same `HTTPException` body.
            for status, entry in TRACK_REFUSAL_RESPONSES.items():
                declared[status] = {**entry, **own.pop(status, {})}
        if declared:
            kwargs["responses"] = {**declared, **own}
        super().__init__(path, endpoint, **kwargs)
