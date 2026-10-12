"""Shared FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Annotated, Any, cast

from fastapi import Depends, HTTPException, Query, params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from harrier.connection import Connection
from harrier.db import connect
from harrier.tracks import (
    NON_DEFAULT_OPERATIONS,
    SLUG_MAX_LENGTH,
    WRITE_OPERATIONS,
    Scope,
    UnknownTrackError,
    default_scope,
    operation_refusal,
    resolve_scope,
)
from harrier_api.demo import demo_db_path, is_demo_mode
from harrier_api.hostedauth import (
    AUTH_401_DESCRIPTION,
    BEARER_SCHEME_NAME,
    KEYS_UNAVAILABLE_DESCRIPTION,
    AuthRefusalOut,
    needs_token,
)


def get_conn() -> Iterator[Connection]:
    # same_thread=False: FastAPI runs this dependency and the endpoint on
    # different threadpool threads, so sqlite3's same-thread check fires
    # intermittently under concurrent requests even though this connection
    # only ever serves one request and is closed below.
    conn = connect(demo_db_path() if is_demo_mode() else None, same_thread=False)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[Connection, Depends(get_conn)]


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


CAUSE_JOIN = "; or "

UNKNOWN_TRACK_CAUSE = "the track parameter named no track"
NOT_AVAILABLE_CAUSE = "the track may not run this operation"
ARCHIVED_CAUSE = "the track is archived"


def track_refusal_responses(operation: str) -> dict[int | str, dict[str, Any]]:
    """What `scope_for(operation)` can answer: a 404 for an unknown slug on
    every route, and a 409 only for the causes this operation can meet, so a
    read every track may run claims none (review of PR #239). The rule is
    `operation_refusal`'s; the status route's verb is any string, so it can
    meet both."""
    in_body = operation == VERB_IN_BODY
    causes: list[str] = []
    if in_body or operation not in NON_DEFAULT_OPERATIONS:
        causes.append(NOT_AVAILABLE_CAUSE)
    if in_body or operation in WRITE_OPERATIONS:
        causes.append(ARCHIVED_CAUSE)
    responses: dict[int | str, dict[str, Any]] = {
        404: {"model": ErrorOut, "description": UNKNOWN_TRACK_CAUSE}
    }
    if causes:
        responses[409] = {"model": ErrorOut, "description": CAUSE_JOIN.join(causes)}
    return responses


def merge_responses(
    *sets: Mapping[int | str, Mapping[str, Any]],
) -> dict[int | str, dict[str, Any]]:
    """One route's response declarations, one entry per status. Two causes of
    the same status are both described, joined, rather than the later one
    replacing the earlier: a run conflict and a track refusal are both 409,
    and unpacking one dict over another kept only the last (review of
    PR #224)."""
    merged: dict[int | str, dict[str, Any]] = {}
    for responses in sets:
        for status, entry in responses.items():
            first = merged.get(status)
            if first is None:
                merged[status] = dict(entry)
                continue
            models = {id(m): m for m in (first.get("model"), entry.get("model")) if m is not None}
            if len(models) > 1:
                # One status has one body; two different models cannot both
                # describe it (review of PR #234).
                raise ValueError(f"status {status} is declared with two body models")
            # Compared as whole causes, so one whose text sits inside
            # another's is still kept (review of PR #234).
            causes = [part for part in str(first.get("description", "")).split(CAUSE_JOIN) if part]
            # The new side is split too, so a joined cause list merged again
            # adds nothing it already has (review of PR #239).
            for more in str(entry.get("description", "")).split(CAUSE_JOIN):
                if more and more not in causes:
                    causes.append(more)
            merged[status] = {**first, **entry, "description": CAUSE_JOIN.join(causes)}
    return merged


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


# --- the hosted API's token refusals (spec 104) ---

# Declared on every route the hosted API serves with a token, in both modes,
# so the document stays one document (ADR-005). The local API never sends
# either body.
AUTH_REFUSAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": AuthRefusalOut, "description": AUTH_401_DESCRIPTION}
}
KEYS_UNAVAILABLE_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {"model": AuthRefusalOut, "description": KEYS_UNAVAILABLE_DESCRIPTION}
}
# A route that opens the database can answer 503 for either cause, so its one
# 503 declaration admits both bodies.
EITHER_503_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "model": DatabaseHeldOut | AuthRefusalOut,
        "description": CAUSE_JOIN.join(
            (str(DATABASE_HELD_RESPONSES[503]["description"]), KEYS_UNAVAILABLE_DESCRIPTION)
        ),
    }
}
BEARER_SECURITY: list[dict[str, list[str]]] = [{BEARER_SCHEME_NAME: []}]


def _needs_token(path: str, methods: object) -> bool:
    # APIRoute's own default when a route names no method.
    named = cast(Sequence[str], methods) if methods else ("GET",)
    return any(needs_token(method, path) for method in named)


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
    """A route that declares the 503 itself when it depends on `Conn`, the
    track refusals when it takes its scope from `scope_for`, and the hosted
    token refusals and bearer scheme when the hosted API serves it with a
    token.

    Declared by the route class rather than in each decorator, so a new route
    that opens the database, names a track or needs a token cannot forget
    them, and one that does not never claims them (specs 075, 094, 104). A
    route's own causes for a status are joined with the track's, never
    replaced by them or replacing them.
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        own: dict[int | str, dict[str, Any]] = dict(kwargs.get("responses") or {})
        declared: dict[int | str, dict[str, Any]] = {}
        opens_database = depends_on_conn(path, endpoint, kwargs.get("dependencies"))
        if _needs_token(path, kwargs.get("methods")):
            declared.update(AUTH_REFUSAL_RESPONSES)
            declared.update(EITHER_503_RESPONSES if opens_database else KEYS_UNAVAILABLE_RESPONSES)
            extra: dict[str, Any] = dict(kwargs.get("openapi_extra") or {})
            extra["security"] = BEARER_SECURITY
            kwargs["openapi_extra"] = extra
        elif opens_database:
            declared.update(DATABASE_HELD_RESPONSES)
        operation = _track_operation(get_dependant(path=path, call=endpoint))
        if operation is not None:
            # Joined per status: the route's own causes and the track's are
            # all answers the route can give. Letting the route's text
            # replace the track's hid the track refusal, the 409 on about
            # twenty routes (review of PR #234) and the unknown track's 404
            # on every route that names a job (review of PR #239). A route
            # that named no body model gains `ErrorOut`, since every refusal
            # here is the same `HTTPException` body.
            for status, entry in track_refusal_responses(operation).items():
                mine = own.pop(status, None)
                if mine is None:
                    declared[status] = dict(entry)
                else:
                    declared[status] = merge_responses({status: mine}, {status: entry})[status]
        if declared:
            kwargs["responses"] = {**declared, **own}
        super().__init__(path, endpoint, **kwargs)
