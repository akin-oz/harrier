"""Tenant credentials over HTTP, hosted only (spec 116).

An owner lists, sets and deletes their credentials. No route reads one
back: `GET /credentials` says which kinds are set and when, and never
selects a ciphertext. Only spec 106's run start decrypts.

Each kind with a body of its own has its own `PUT`, so the contract names
each body. `PUT /credentials/{kind}` answers 404 for every other kind:
Telegram is set by spec 109's route, after it checks the bot, and Gmail is
not a kind.

A refused body is answered 422 with the field and the rule only
(`credential_refusal`): FastAPI's own 422 echoes the submitted input, and
the input here is a secret. Nothing here logs a request body.

Locally every route answers 404: credentials are read from `.env` on the
local API. The routes are in the one contract all the same (spec 104).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field

from harrier.credentials import (
    FIELD_MAX_BYTES,
    FIELDS,
    KIND_FIELDS,
    KINDS,
    AiProvider,
    CredentialKind,
    CredentialsKey,
    credential_updates,
    delete_credential,
    field_problem,
    put_credential,
    unknown_field_problem,
)
from harrier_api.deps import Conn, DatabaseRoute, ErrorOut

LOCAL_TEXT = "credentials are read from .env on the local API"
TELEGRAM_ELSEWHERE = "the telegram credential is set by its own route (spec 109)"
UNKNOWN_KIND = "no credential kind has that name"
PREFIX = "/credentials"


def hosted_only(request: Request) -> None:
    """404 with the local text on the local API, before anything opens the
    store. First among each route's dependencies."""
    if not getattr(request.app.state, "hosted", False):
        raise HTTPException(status_code=404, detail=LOCAL_TEXT)


credentials_router = APIRouter(
    prefix=PREFIX, route_class=DatabaseRoute, dependencies=[Depends(hosted_only)]
)

# --- bodies ---


def _stripped(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _required(value: str) -> str:
    problem = field_problem(value)
    if problem is not None:
        raise ValueError(problem)
    return value.strip()


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    problem = field_problem(value, optional=True)
    if problem is not None:
        raise ValueError(problem)
    return value.strip() or None


_RULE = (
    f"Stripped of surrounding whitespace; refused when empty, not text UTF-8 can "
    f"encode, longer than {FIELD_MAX_BYTES} bytes in UTF-8, or holding a control character."
)
# Kept out of the model's repr, so a body that reaches a log record or an
# error by `repr` carries no secret (local review of PR #310).
Secret = Annotated[str, AfterValidator(_required), Field(description=_RULE, repr=False)]
Provider = Annotated[AiProvider, BeforeValidator(_stripped)]


class AiCredentialIn(BaseModel):
    """The one AI credential: its provider decides which key a run gets."""

    model_config = ConfigDict(extra="forbid")

    provider: Provider
    key: Secret
    model: Annotated[
        str | None,
        AfterValidator(_optional),
        Field(description="Optional; empty is unset. " + _RULE),
    ] = None


class ApifyCredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Secret


class HunterCredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Secret


# --- answers ---


class CredentialStatusOut(BaseModel):
    """Whether one kind is set, and when it was last written. Never a value."""

    kind: CredentialKind
    set: bool
    updated_at: str | None


class CredentialsOut(BaseModel):
    """One item per kind, in the kinds table's order."""

    credentials: list[CredentialStatusOut]


class CredentialProblemOut(BaseModel):
    field: str
    problem: str


class CredentialRefusalOut(BaseModel):
    """A refused body: the field and the rule, never the submitted value."""

    detail: list[CredentialProblemOut]


LOCAL_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut, "description": LOCAL_TEXT},
}
# A path parameter is any string, which nothing refuses, so these routes
# never send a 422. FastAPI declares one on every route with a parameter, and
# the body it would carry here is the credentials routes' own.
PATH_REFUSAL: dict[int | str, dict[str, Any]] = {
    422: {
        "model": CredentialRefusalOut,
        "description": "a request the schema refuses, named by field and rule",
    },
}
KIND_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorOut,
        "description": f"{LOCAL_TEXT}; or {UNKNOWN_KIND}",
    },
    **PATH_REFUSAL,
}
PUT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_RESPONSE,
    422: {
        "model": CredentialRefusalOut,
        "description": "a field is empty, too long, or holds a control character",
    },
}
ELSEWHERE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorOut,
        "description": f"{LOCAL_TEXT}; or {TELEGRAM_ELSEWHERE}; or {UNKNOWN_KIND}",
    },
    **PATH_REFUSAL,
}


def credential_refusal(error: RequestValidationError, path: str) -> CredentialRefusalOut:
    """The 422 body for a refused credential: each error's field and its
    rule. Pydantic keeps the submitted value in `input`, which is dropped,
    and never in `msg`. Three things a caller wrote are not repeated either
    (local review of PR #310): a field name the kind does not take, which
    is reported as the kind's own fields; the character offset of a body
    that is not JSON, reported as the body; and nothing else of `loc` but
    the declared field names."""
    kind = path.rstrip("/").rpartition("/")[2]
    problems: list[CredentialProblemOut] = []
    for entry in error.errors():
        location = list(entry.get("loc", ()))
        if location[:1] == ["body"]:
            location = location[1:]
        if entry.get("type") == "extra_forbidden" and kind in KIND_FIELDS:
            field, problem = FIELDS, unknown_field_problem(kind)
        elif not location or not all(isinstance(part, str) for part in location):
            field, problem = "body", str(entry.get("msg", ""))
        else:
            field = ".".join(str(part) for part in location)
            problem = str(entry.get("msg", "")).removeprefix("Value error, ")
        problems.append(CredentialProblemOut(field=field, problem=problem))
    return CredentialRefusalOut(detail=problems)


def is_credentials_route(scope: Mapping[str, Any]) -> bool:
    """Whether the request matched one of this module's routes. Read from
    the matched route, not the path, so a root path or a prefix changes
    nothing (local review of PR #310)."""
    route = scope.get("route")
    return route is not None and any(route is own for own in credentials_router.routes)


def credentials_refusal_response(request: Request, error: RequestValidationError) -> JSONResponse:
    """A refused request on a credentials route. Locally the local 404
    wins: FastAPI reads a body that is not JSON before any dependency runs,
    so `hosted_only` never saw it (local review of PR #310)."""
    if not getattr(request.app.state, "hosted", False):
        return JSONResponse(status_code=404, content=ErrorOut(detail=LOCAL_TEXT).model_dump())
    body = credential_refusal(error, cast(str, request.scope["path"]))
    return JSONResponse(status_code=422, content=body.model_dump())


# --- routes ---


def request_owner(request: Request) -> str:
    """The verified `sub` of a hosted request. Spec 109's Telegram route
    reads it too."""
    state = cast(dict[str, object], request.scope.get("state") or {})
    claims = state.get("claims")
    sub = cast(dict[str, object], claims).get("sub") if isinstance(claims, dict) else None
    if not isinstance(sub, str):
        # The request transaction refuses a request without claims before
        # this runs; a route reached some other way writes nothing.
        raise HTTPException(status_code=500, detail="the request carries no owner")
    return sub


def request_key(request: Request) -> CredentialsKey:
    """The key the hosted app encrypts with. Spec 109's Telegram route uses
    it too."""
    key = getattr(request.app.state, "credentials_key", None)
    if not isinstance(key, CredentialsKey):
        raise HTTPException(status_code=500, detail="the hosted API holds no credentials key")
    return key


def _status(kind: CredentialKind, updated_at: str | None) -> CredentialStatusOut:
    return CredentialStatusOut(kind=kind, set=updated_at is not None, updated_at=updated_at)


@credentials_router.get("", responses=LOCAL_RESPONSE)
def list_credentials(conn: Conn) -> CredentialsOut:
    updates = credential_updates(conn)
    return CredentialsOut(credentials=[_status(kind, updates.get(kind)) for kind in KINDS])


def _put(
    request: Request, conn: Conn, kind: CredentialKind, fields: dict[str, str | None]
) -> CredentialStatusOut:
    written = put_credential(conn, request_key(request), request_owner(request), kind, fields)
    return _status(kind, written)


@credentials_router.put("/ai", responses=PUT_RESPONSES)
def put_ai_credential(request: Request, body: AiCredentialIn, conn: Conn) -> CredentialStatusOut:
    return _put(
        request, conn, "ai", {"provider": body.provider, "key": body.key, "model": body.model}
    )


@credentials_router.put("/apify", responses=PUT_RESPONSES)
def put_apify_credential(
    request: Request, body: ApifyCredentialIn, conn: Conn
) -> CredentialStatusOut:
    return _put(request, conn, "apify", {"token": body.token})


@credentials_router.put("/hunter", responses=PUT_RESPONSES)
def put_hunter_credential(
    request: Request, body: HunterCredentialIn, conn: Conn
) -> CredentialStatusOut:
    return _put(request, conn, "hunter", {"key": body.key})


# Its only answer is the 404, so that is its declared status: the contract
# claims no success it never sends.
@credentials_router.put(
    "/{kind}", status_code=404, response_model=ErrorOut, responses=ELSEWHERE_RESPONSES
)
def put_other_credential(kind: str) -> ErrorOut:
    """Every kind this module does not set. Takes no body and opens nothing."""
    raise HTTPException(
        status_code=404, detail=TELEGRAM_ELSEWHERE if kind == "telegram" else UNKNOWN_KIND
    )


@credentials_router.delete("/{kind}", responses=KIND_RESPONSES)
def delete_a_credential(kind: str, conn: Conn) -> CredentialStatusOut:
    if kind not in KINDS:
        raise HTTPException(status_code=404, detail=UNKNOWN_KIND)
    delete_credential(conn, kind)
    return _status(kind, None)
