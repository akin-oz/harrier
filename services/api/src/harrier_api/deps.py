"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Any

from fastapi import Depends, params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.routing import APIRoute
from pydantic import BaseModel

from harrier.db import connect
from harrier.tracks import Scope, default_scope
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
    """The track this request works in, resolved once per request (spec 092).

    The default track until a later spec lets a request name one. Every
    route that reads or writes tracker rows takes it, so no route can read
    across tracks by forgetting.
    """
    return default_scope(conn)


ScopeDep = Annotated[Scope, Depends(get_scope)]


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
    """A route that declares the 503 itself when it depends on `Conn`.

    Declared by the route class rather than in each decorator, so a new route
    that opens the database cannot forget it, and one that does not never
    claims it (spec 075).
    """

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        if depends_on_conn(path, endpoint, kwargs.get("dependencies")):
            kwargs["responses"] = {**DATABASE_HELD_RESPONSES, **(kwargs.get("responses") or {})}
        super().__init__(path, endpoint, **kwargs)
