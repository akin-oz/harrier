"""Inbox endpoints (spec 049).

Two routes: read what the watch classified, and run the watch.

The read is unusual in this API for carrying no token, and that is
deliberate. `harrier.mail.watch.redact_event` decides what the archive
holds: the sender is reduced to its domain, and the subject and the body
summary are dropped entirely on the grounds that they are the other party's
words. So there is no identifying content here to protect, unlike the
artifacts of spec 047 or the contacts of spec 048, both of which
authenticate. Nothing in this module widens what is stored.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import Manager, RunOut, run_out
from harrier_api.runs import RunParams

mail_router = APIRouter()


class WatchIn(BaseModel):
    """A dry run classifies and notifies nobody, which is how an operator
    checks the classifier without sending themselves messages."""

    dry_run: bool = False


class MailEventOut(BaseModel):
    """One archived classification.

    Every field here is one `redact_event` archives. There is no subject and
    no sender, because the archive has neither.
    """

    kind: str
    priority: str = ""
    company: str = ""
    role: str = ""
    tracker_row: str = ""
    next_action: str = ""
    timestamp: str = ""
    message_id: str = ""
    from_domain: str = ""
    actionable: bool = False
    ignore_reason: str = ""


class MailEventsOut(BaseModel):
    """The window, and what it cannot tell you.

    `has_run` is not a nicety: a watch that has never run and one that ran
    and classified nothing produce the same empty list and mean entirely
    different things.
    """

    events: list[MailEventOut]
    has_run: bool
    at_cap: bool


def _as_event(raw: dict[str, object]) -> MailEventOut:
    def text(key: str) -> str:
        value = raw.get(key)
        return "" if value is None else str(value)

    return MailEventOut(
        kind=text("kind"),
        priority=text("priority"),
        company=text("company"),
        role=text("role"),
        tracker_row=text("tracker_row"),
        next_action=text("next_action"),
        timestamp=text("timestamp"),
        message_id=text("messageId"),
        from_domain=text("from_domain"),
        actionable=bool(raw.get("actionable", False)),
        ignore_reason=text("ignore_reason"),
    )


@mail_router.get("/mail/events", operation_id="listMailEvents")
def list_mail_events(limit: Annotated[int | None, Query(ge=1, le=500)] = None) -> MailEventsOut:
    """The archived events, newest first.

    No token: this is a read of a store that was redacted on the way in
    (spec 049). The asymmetry with spec 047's artifact reads is the
    redaction, not an oversight.
    """
    from harrier.mail import read_events

    window = read_events(limit)
    return MailEventsOut(
        events=[_as_event(event) for event in window.events],
        has_run=window.has_run,
        at_cap=window.at_cap,
    )


@mail_router.post(
    "/mail/watch",
    operation_id="runMailWatch",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def run_mail_watch(body: WatchIn, manager: Manager) -> RunOut:
    """The same `gmail-watch` verb the CLI runs, as a run.

    A run rather than a request: it reaches a remote service, its duration
    depends on how much mail is waiting, and its failures are the kind an
    operator needs to read rather than a spinner that stops.
    """
    params = RunParams(switches=frozenset({"--dry-run"}) if body.dry_run else frozenset())
    return run_out(await manager.start("gmail-watch", params))
