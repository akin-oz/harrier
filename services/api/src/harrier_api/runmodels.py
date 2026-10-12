"""The run response, and the dependency that reaches the run manager.

Extracted from `app.py` when the outreach routes needed both and could not
import them without a cycle (spec 048). `RunOut` keeps its name, so the
generated OpenAPI component and the type the web app imports are unchanged.

Every run route reaches the manager through `Manager`, which is the manager
as the request's owner sees it (spec 132): hosted, a route lists, reads,
cancels and streams only the caller's runs, and a run it starts records the
caller. Locally the owner is empty and every run is everyone's.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Annotated, Any, cast

from fastapi import Depends, Request
from pydantic import BaseModel

from harrier_api.deps import ErrorOut
from harrier_api.runs import (
    RUNS_NOT_HOSTED_DETAIL,
    Run,
    RunEvent,
    RunManager,
    RunParams,
    RunState,
    format_sse,
    runs_hosted,
)


class RunOut(BaseModel):
    id: str
    kind: str
    state: RunState
    created_at: str
    started_at: str | None
    ended_at: str | None
    exit_code: int | None


def run_out(run: Run) -> RunOut:
    return RunOut(
        id=run.id,
        kind=run.kind,
        state=run.state,
        created_at=run.created_at,
        started_at=run.started_at,
        ended_at=run.ended_at,
        exit_code=run.exit_code,
    )


# What every route that starts a run can answer when a run of the same kind
# and target is active with other options (`RunConflictError`, review of
# PR #208), and, hosted, when no run may start (`RunsNotHosted`, spec 132).
# Declared so the generated client reads the words by type. Every route that
# calls `start` declares this, and only those. The local API never sends the
# 501; its document is the hosted one.
RUN_CONFLICT_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorOut,
        "description": "refused: a run of this kind is active with other options, "
        "or the request lacks its explicit field",
    },
    501: {
        "model": ErrorOut,
        "description": f"hosted only: {RUNS_NOT_HOSTED_DETAIL}",
    },
}

# The stream's last line when the token that opened it expires: an SSE
# comment, which the browser ignores, naming why it ended and carrying no
# claim (spec 132, Open decision 1, the recommended answer).
TOKEN_EXPIRED_LINE = ": token expired\n\n"


def get_manager(request: Request) -> RunManager:
    return cast(RunManager, request.app.state.run_manager)


@dataclass(frozen=True)
class OwnedRuns:
    """The run manager as one request's owner sees it (spec 132).

    `hosted` is false locally, where every run is listed and every route
    answers as it always has. Hosted, `owner` is the request's verified
    `sub`, a run is visible only when its owner is exactly that, and a run
    with an empty owner is visible to no one. Another owner's run is answered
    as an id that does not exist, so a response does not say it exists.
    """

    manager: RunManager
    owner: str
    hosted: bool
    # The `exp` of the token that opened the request, hosted; None locally.
    expires_at: float | None

    def sees(self, run: Run) -> bool:
        return not self.hosted or (run.owner != "" and run.owner == self.owner)

    def get(self, run_id: str) -> Run | None:
        run = self.manager.get(run_id)
        return run if run is not None and self.sees(run) else None

    def list_runs(self) -> list[Run]:
        return [run for run in self.manager.list_runs() if self.sees(run)]

    async def cancel(self, run_id: str) -> Run | None:
        # Another owner's run is not cancelled: it is not found.
        if self.get(run_id) is None:
            return None
        return await self.manager.cancel(run_id)

    async def start(self, kind: str, params: RunParams | None = None) -> Run:
        # The one place a route's run gets its owner, so no route passes
        # another (spec 132).
        return await self.manager.start(kind, params, owner=self.owner)

    def stream(self, run_id: str, last_event_id: int = 0) -> AsyncIterator[str]:
        """The run's SSE messages after `last_event_id`. Hosted, the stream
        also ends at the token's `exp`, so it does not outlive the authority
        that opened it; the browser reconnects with a fresh token and
        `Last-Event-ID` and loses nothing."""
        return sse_until(self.manager.stream(run_id, last_event_id), self.expires_at)


async def sse_until(
    events: AsyncIterator[RunEvent],
    expires_at: float | None,
    clock: Callable[[], float] = time.time,
) -> AsyncIterator[str]:
    """Each event as an SSE message, until the events end or `expires_at`
    passes, whichever is first. At expiry the last line is
    `TOKEN_EXPIRED_LINE`, also while no event is pending."""
    async with aclosing(cast(Any, events)) as source:
        while True:
            remaining: float | None = None
            if expires_at is not None:
                remaining = expires_at - clock()
                if remaining <= 0:
                    yield TOKEN_EXPIRED_LINE
                    return
            try:
                event = await asyncio.wait_for(anext(source), remaining)
            except StopAsyncIteration:
                return
            except TimeoutError:
                yield TOKEN_EXPIRED_LINE
                return
            yield format_sse(cast(RunEvent, event))


def _expiry(claims: dict[str, object]) -> float:
    """The verified `exp`. A hosted request without one has no authority to
    keep a stream open, so its stream ends at once."""
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return 0.0
    return float(exp)


def get_runs(request: Request, manager: Annotated[RunManager, Depends(get_manager)]) -> OwnedRuns:
    if not runs_hosted():
        return OwnedRuns(manager=manager, owner="", hosted=False, expires_at=None)
    # The claims spec 104's middleware verified and attached. Without them,
    # or without a string `sub`, the owner is empty: no run is visible and
    # none may start.
    found: object = getattr(request.state, "claims", None)
    claims = cast(dict[str, object], found) if isinstance(found, dict) else {}
    sub = claims.get("sub")
    return OwnedRuns(
        manager=manager,
        owner=sub if isinstance(sub, str) else "",
        hosted=True,
        expires_at=_expiry(claims),
    )


Manager = Annotated[OwnedRuns, Depends(get_runs)]
