"""The run response, and the dependency that reaches the run manager.

Extracted from `app.py` when the outreach routes needed both and could not
import them without a cycle (spec 048). Nothing here is new: `RunOut` keeps
its name, so the generated OpenAPI component and the type the web app
imports are unchanged.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import Depends, Request
from pydantic import BaseModel

from harrier_api.deps import ErrorOut
from harrier_api.runs import Run, RunManager, RunState


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
# PR #208). Declared so the generated client reads the words by type.
RUN_CONFLICT_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorOut,
        "description": "refused: a run of this kind is active with other options, "
        "or the request lacks its explicit field",
    }
}


def get_manager(request: Request) -> RunManager:
    return cast(RunManager, request.app.state.run_manager)


Manager = Annotated[RunManager, Depends(get_manager)]
