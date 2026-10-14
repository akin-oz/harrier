"""Each owner's Telegram bot, over HTTP, hosted only (spec 109).

An owner brings a bot made with @BotFather and saves its token and their
chat id. From then on every notification their runs send goes to their chat
through their bot (spec 106 gives a run the pair as `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`, so `harrier.notify` is unchanged).

`PUT` checks the pair before it stores anything: the shapes, the Bot API's
`getMe`, then one fixed confirmation message to the chat
(`harrier.notify.check_telegram`). Only a pair that passed is written,
through spec 116's credential store, replacing any earlier pair. The check
runs in the request with the values the request carried; the API decrypts
no stored credential for it.

`GET` says whether a pair is stored and when it was last saved. `DELETE`
removes it. Neither calls Telegram, and no route returns a value: the bot
token and the chat id are both secret (decision D7). A refusal is a fixed
sentence per reason and never quotes either.

A body the schema refuses gets spec 116's 422, which names the field and
the rule only: FastAPI's own 422 echoes the submitted input.

Locally every route answers 404: Telegram is configured in `.env` on the
local product. The routes are in the one contract all the same (spec 104).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from harrier.credentials import credential_updates, delete_credential, put_credential
from harrier.notify import TelegramReason, check_telegram
from harrier_api.credentials_routes import (
    CredentialProblemOut,
    CredentialRefusalOut,
    credential_refusal,
    request_key,
    request_owner,
)
from harrier_api.deps import Conn, DatabaseRoute, ErrorOut

logger = logging.getLogger(__name__)

LOCAL_TEXT = "Telegram is configured in .env on the local product"
UNREADABLE_BODY = "must be a JSON document UTF-8 can read"
PREFIX = "/notifications"


def hosted_only(request: Request) -> None:
    """404 with the local text on the local API, before anything opens the
    store. First among each route's dependencies."""
    if not getattr(request.app.state, "hosted", False):
        raise HTTPException(status_code=404, detail=LOCAL_TEXT)


notify_router = APIRouter(
    prefix=PREFIX, route_class=DatabaseRoute, dependencies=[Depends(hosted_only)]
)

# --- bodies and answers ---


class TelegramSettingsIn(BaseModel):
    """The owner's bot token and chat id. Both are secret and write-only."""

    # Plain strings to the schema, no pattern or length, so a malformed value
    # reaches the route's own check and is refused with a reason. Both are
    # kept out of the model's repr, so a body that reaches a log record or an
    # error by `repr` carries neither.
    model_config = ConfigDict(extra="forbid")

    bot_token: SecretStr = Field(description="The bot's token from @BotFather. Secret.")
    chat_id: str = Field(
        repr=False,
        description="The chat the bot writes to. Secret (decision D7), like the token.",
        json_schema_extra={"format": "password", "writeOnly": True},
    )


class TelegramSettingsOut(BaseModel):
    """Whether the owner has a pair saved, and when. Never the pair."""

    configured: bool
    updated_at: str | None


class TelegramRefusalOut(BaseModel):
    """Why a save was refused: a fixed sentence per `reason`, never the
    token, the chat id, or Telegram's own description."""

    detail: str
    reason: TelegramReason


# One status and one sentence per reason, from the closed set in
# `harrier.notify`. The first four are the owner's to fix; the last is
# Telegram's being out of reach.
REFUSALS: dict[TelegramReason, tuple[int, str]] = {
    "token_shape": (
        400,
        "The bot token is not of the form Telegram issues: digits, a colon, "
        "then letters, digits, underscores or hyphens.",
    ),
    "chat_id_shape": (
        400,
        "The chat id must be a whole number with an optional leading minus sign. "
        "Channel usernames are not accepted.",
    ),
    "token_rejected": (400, "Telegram did not accept the bot token. Check it with @BotFather."),
    "chat_unreachable": (
        400,
        "Telegram would not deliver a message to that chat. Send your bot a message "
        "first, check the chat id, and save again.",
    ),
    "telegram_unavailable": (
        502,
        "Telegram could not be reached or did not answer in time. Try again later.",
    ),
}

LOCAL_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut, "description": LOCAL_TEXT},
}
PUT_RESPONSES: dict[int | str, dict[str, Any]] = {
    **LOCAL_RESPONSE,
    400: {
        "model": TelegramRefusalOut,
        "description": "the token or chat id is malformed, Telegram refused the token, "
        "or the bot cannot write to the chat; nothing is stored",
    },
    422: {
        "model": CredentialRefusalOut,
        "description": "a request the schema refuses, named by field and rule",
    },
    502: {
        "model": TelegramRefusalOut,
        "description": "Telegram could not be reached or did not answer in time; nothing is stored",
    },
}


def telegram_refusal(reason: TelegramReason) -> JSONResponse:
    """The answer to a refused save. Logged by its reason alone."""
    status, detail = REFUSALS[reason]
    logger.info("telegram settings refused: %s", reason)
    body = TelegramRefusalOut(detail=detail, reason=reason)
    return JSONResponse(status_code=status, content=body.model_dump())


# --- a body the schema refuses ---


def is_notify_route(scope: Mapping[str, Any]) -> bool:
    """Whether the request matched one of this module's routes. Read from
    the matched route, not the path, so a root path or a prefix changes
    nothing (as spec 116's `is_credentials_route`)."""
    route = scope.get("route")
    return route is not None and any(route is own for own in notify_router.routes)


def notify_refusal_response(request: Request, error: RequestValidationError) -> JSONResponse:
    """A refused request on a Telegram route. Locally the local 404 wins:
    FastAPI reads a body that is not JSON before any dependency runs, so
    `hosted_only` never saw it. Hosted, the 422 is spec 116's: the field and
    the rule, never the input, a field name the route does not take, or the
    offset of a body that is not JSON."""
    if not getattr(request.app.state, "hosted", False):
        return JSONResponse(status_code=404, content=ErrorOut(detail=LOCAL_TEXT).model_dump())
    body = credential_refusal(error, cast(str, request.scope["path"]))
    return JSONResponse(status_code=422, content=body.model_dump())


def notify_unreadable_body_response(request: Request) -> JSONResponse:
    """A body FastAPI could not read at all, such as one that is not UTF-8:
    it answers that with its own 400 before any validation, a body this
    route's 400 does not declare. Answered as a body that is not JSON is:
    the local 404, or hosted the 422 naming the body (contract review of
    PR #325)."""
    if not getattr(request.app.state, "hosted", False):
        return JSONResponse(status_code=404, content=ErrorOut(detail=LOCAL_TEXT).model_dump())
    body = CredentialRefusalOut(
        detail=[CredentialProblemOut(field="body", problem=UNREADABLE_BODY)]
    )
    return JSONResponse(status_code=422, content=body.model_dump())


# --- the check, before the request's transaction ---


@dataclass(frozen=True)
class CheckedPair:
    """The stripped pair and what `check_telegram` made of it. Kept out of
    the repr, so it carries neither value into a log or an error."""

    token: str = field(repr=False)
    chat_id: str = field(repr=False)
    reason: TelegramReason | None


def check_pair(request: Request, body: TelegramSettingsIn) -> CheckedPair:
    """Check the pair against the Bot API as a dependency declared before
    `Conn`, so the request's transaction opens after both calls end. Run
    inside it, the check held a pooled connection for as long as Telegram
    took, and ten slow saves would have held the pool (data integrity review
    of PR #325).

    The key and the owner are read first: a request that cannot be written
    must not send the owner a confirmation. Stripped as the store strips
    them, so the pair that is checked is the pair that is stored."""
    request_key(request)
    request_owner(request)
    token = body.bot_token.get_secret_value().strip()
    chat_id = body.chat_id.strip()
    return CheckedPair(token, chat_id, check_telegram(token, chat_id))


# --- routes ---


@notify_router.get("/telegram", operation_id="getTelegramSettings", responses=LOCAL_RESPONSE)
def get_telegram(conn: Conn) -> TelegramSettingsOut:
    # `kind` and `updated_at` only: the ciphertext is never selected. Both
    # values live in the one row, so a row means both are stored.
    written = credential_updates(conn).get("telegram")
    return TelegramSettingsOut(configured=written is not None, updated_at=written)


@notify_router.put(
    "/telegram",
    operation_id="saveTelegramSettings",
    response_model=TelegramSettingsOut,
    responses=PUT_RESPONSES,
)
def put_telegram(
    request: Request, checked: Annotated[CheckedPair, Depends(check_pair)], conn: Conn
) -> TelegramSettingsOut | JSONResponse:
    # `checked` is declared before `conn`: the check has ended when the
    # transaction opens.
    if checked.reason is not None:
        return telegram_refusal(checked.reason)
    # Only a pair that passed the check is written. `check_telegram` holds
    # every value to the store's field rule, so the store does not refuse it.
    written = put_credential(
        conn,
        request_key(request),
        request_owner(request),
        "telegram",
        {"bot_token": checked.token, "chat_id": checked.chat_id},
    )
    return TelegramSettingsOut(configured=True, updated_at=written)


@notify_router.delete("/telegram", operation_id="clearTelegramSettings", responses=LOCAL_RESPONSE)
def delete_telegram(conn: Conn) -> TelegramSettingsOut:
    delete_credential(conn, "telegram")
    return TelegramSettingsOut(configured=False, updated_at=None)
