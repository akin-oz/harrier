"""Inbox endpoints (spec 049).

Two routes: read what the watch classified, and run the watch.

The read is unusual in this API for carrying no token, and that is
deliberate. `harrier.mail.watch.redact_event` decides what the archive
holds: the sender is reduced to its domain, and the subject and the body
summary are dropped entirely on the grounds that they are the other party's
words. So there is no identifying content here to protect, unlike the
artifacts of spec 047 or the contacts of spec 048, both of which
authenticate. Nothing in this module widens what is stored.

Hosted, both routes answer 501: Gmail watch is not offered on the hosted
service (spec 109). The refusal is each route's first dependency, so
neither reads the events file nor starts a run.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from harrier_api.deps import DatabaseRoute, ErrorOut, merge_responses
from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import RUN_CONFLICT_RESPONSES, Manager, RunOut, run_out
from harrier_api.runs import GMAIL_NOT_HOSTED_DETAIL, GmailNotHosted, RunParams


def gmail_is_local(request: Request) -> None:
    """Hosted, refuse before the route reads a file or starts a run (spec
    109). The app answers `GmailNotHosted` with 501 and its detail."""
    if getattr(request.app.state, "hosted", False):
        raise GmailNotHosted


# Declared on both routes, in both modes, so the contract is one document
# (spec 104). The local API never sends it.
GMAIL_NOT_HOSTED_RESPONSES: dict[int | str, dict[str, Any]] = {
    501: {"model": ErrorOut, "description": f"hosted only: {GMAIL_NOT_HOSTED_DETAIL}"},
}

mail_router = APIRouter(route_class=DatabaseRoute, dependencies=[Depends(gmail_is_local)])


class WatchIn(BaseModel):
    """A dry run classifies and notifies nobody, which is how an operator
    checks the classifier without sending themselves messages."""

    dry_run: bool = False


class MailEventOut(BaseModel):
    """One archived classification.

    A subset of what `redact_event` archives. There is no subject and no
    sender, because the archive has neither.

    `messageId` is archived but deliberately not returned. It is a stable
    identifier for a message in the operator's mailbox, and this route
    answers without a token; the spec's argument for that is that nothing
    identifying is here, and an id that survives across reads is the one
    field that argument does not cover (review finding on PR #51).
    """

    kind: str
    priority: str = ""
    company: str = ""
    role: str = ""
    tracker_row: str = ""
    next_action: str = ""
    timestamp: str = ""
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
        from_domain=text("from_domain"),
        actionable=bool(raw.get("actionable", False)),
        ignore_reason=text("ignore_reason"),
    )


@mail_router.get(
    "/mail/events", operation_id="listMailEvents", responses=GMAIL_NOT_HOSTED_RESPONSES
)
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
    # The run conflict, but not the hosted run refusal: hosted, this route
    # refuses with its own 501 before it reaches the run manager.
    responses=merge_responses(
        TOKEN_RESPONSES, {409: RUN_CONFLICT_RESPONSES[409]}, GMAIL_NOT_HOSTED_RESPONSES
    ),
)
async def run_mail_watch(body: WatchIn, manager: Manager) -> RunOut:
    """The same `gmail-watch` verb the CLI runs, as a run.

    A run rather than a request: it reaches a remote service, its duration
    depends on how much mail is waiting, and its failures are the kind an
    operator needs to read rather than a spinner that stops.
    """
    params = RunParams(switches=frozenset({"--dry-run"}) if body.dry_run else frozenset())
    return run_out(await manager.start("gmail-watch", params))
