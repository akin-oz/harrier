"""Operations endpoints: what an operator does to keep the system running
(spec 050, as amended by spec 096 before it was built).

Every write here is a run of the same CLI verb the terminal runs, so there is
one implementation of each report. The routes add two things the terminal
does not need: defaults that change nothing on an empty body, and a refusal
to send the same day's digest twice.

The API runs in a container with no `launchctl` (ADR-010), so the schedule
read reports what the container can see, the cadence and the last-success
records, and says the installed and loaded state is the host's to report.
"""

from __future__ import annotations

import datetime as dt
import io
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from harrier.discovery import SOURCE_ORDER
from harrier.tracks import DEFAULT_TRACK_ID
from harrier.tracks import Scope as TrackScope
from harrier_api.deps import Conn, DatabaseRoute, ErrorOut, scope_for
from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import Manager, RunOut, run_out
from harrier_api.runs import RunParams, backup_hidden_directories

ops_router = APIRouter(route_class=DatabaseRoute)

# The command that reports what this API cannot see (spec 096).
SCHEDULE_HOST_COMMAND = "harrier schedule status"
INSTALLED_STATE = (
    "Whether each job is installed and loaded in launchd is the host's to report; "
    "this server runs where launchd cannot be asked."
)

# The sources as the contract declares them, built from the domain's own
# order so a new source reaches the generated client on the next
# `just contract`. Shown to type checkers as an empty StrEnum, as
# `harrier_api.app` does for the reason codes.
if TYPE_CHECKING:

    class SourceName(StrEnum): ...

else:
    SourceName = StrEnum("SourceName", {name: name for name in SOURCE_ORDER})


REFUSAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorOut, "description": "the request needs its explicit field"},
    **TOKEN_RESPONSES,
}


# --- feed health (spec 025) ---


class PruneIn(BaseModel):
    """Pruning edits stored configuration, so an empty body refuses."""

    confirm: bool = False


@ops_router.post(
    "/ops/feeds",
    operation_id="checkFeeds",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def check_feeds(manager: Manager) -> RunOut:
    """`config check-feeds` as a run: one probe per configured board.

    A run, and a POST, because it starts a process that reaches the network.
    Its results are the run's log, which `GET /runs/{id}/events` serves
    without the token, as spec 050 states for feed results.
    """
    params = RunParams(target="check")
    return run_out(await manager.start("check-feeds", params))


@ops_router.post(
    "/ops/feeds/prune",
    operation_id="pruneDeadFeeds",
    dependencies=[Depends(require_token)],
    responses=REFUSAL_RESPONSES,
)
async def prune_dead_feeds(manager: Manager, body: PruneIn = PruneIn()) -> RunOut:  # noqa: B008
    """`config check-feeds --prune`: probes again, then removes the boards
    that answered as dead and prints each removed URL. Never part of a check."""
    if not body.confirm:
        raise HTTPException(
            status_code=409,
            detail=(
                "pruning removes boards from the stored watchlist; "
                "send confirm: true to prune the boards a fresh check finds dead"
            ),
        )
    params = RunParams(switches=frozenset({"--prune"}), target="prune")
    return run_out(await manager.start("check-feeds", params))


# --- reconsideration (spec 031) ---


class ReconsiderIn(BaseModel):
    """Report by default; `apply` clears, as a second, separate request."""

    apply: bool = False
    source: SourceName | None = None


@ops_router.post(
    "/ops/reconsider",
    operation_id="reconsider",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def reconsider(
    manager: Manager,
    scope: Annotated[TrackScope, Depends(scope_for("reconsider"))],
    body: ReconsiderIn = ReconsiderIn(),  # noqa: B008
) -> RunOut:
    """`reconsider`, on the track the request names, as the CLI's `--track`."""
    track = None if scope.track.id == DEFAULT_TRACK_ID else scope.track.slug
    params = RunParams(
        switches=frozenset({"--apply"}) if body.apply else frozenset(),
        choices={} if body.source is None else {"--source": body.source.value},
        track=track,
        target=f"{scope.track.slug}:{'apply' if body.apply else 'report'}",
    )
    return run_out(await manager.start("reconsider", params))


# --- backup (spec 030) ---


class BackupIn(BaseModel):
    """An empty body takes an archive and deletes nothing. `prune` applies
    the CLI's own retention, which is the CLI's decision, not a copy here."""

    prune: bool = False


@ops_router.post(
    "/ops/backup",
    operation_id="takeBackup",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
async def take_backup(manager: Manager, body: BackupIn = BackupIn()) -> RunOut:  # noqa: B008
    """`backup` as a run, into the directory the Settings page lists, and
    reported by the archive's name, never its path (spec 096)."""
    from harrier.backup import backup_dir

    params = RunParams(
        switches=frozenset() if body.prune else frozenset({"--no-prune"}),
        destination=backup_dir(),
        hidden=backup_hidden_directories(),
    )
    return run_out(await manager.start("backup", params))


# --- the digest (spec 019) ---


class DigestIn(BaseModel):
    """A dry run unless the body says otherwise: a dropped body, or a client
    that forgot the field, must not send a message to a real person."""

    dry_run: bool = True
    date: dt.date | None = None
    # A second send of a day already delivered is refused; this is the
    # explicit request that sends it again.
    resend: bool = False


@ops_router.post(
    "/ops/digest",
    operation_id="runDigest",
    dependencies=[Depends(require_token)],
    responses=REFUSAL_RESPONSES,
)
async def run_digest_route(
    conn: Conn,
    manager: Manager,
    scope: Annotated[TrackScope, Depends(scope_for("digest"))],
    body: DigestIn = DigestIn(),  # noqa: B008
) -> RunOut:
    """`digest` as a run. The send is single-flight per day: a second request
    while one runs joins it, and a day already delivered is refused with the
    time it went (spec 050)."""
    from harrier.digest import delivered_at, parse_target_date

    del scope  # the default track's; any other is refused by the dependency
    target = body.date if body.date is not None else parse_target_date(None)
    if not body.dry_run and not body.resend:
        sent = delivered_at(conn, target)
        if sent is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the digest for {target.isoformat()} was already sent at {sent}; "
                    "send it again with resend: true"
                ),
            )
    params = RunParams(
        switches=frozenset({"--dry-run"}) if body.dry_run else frozenset(),
        dates={"--date": target},
        target=f"{target.isoformat()}:{'dry' if body.dry_run else 'send'}",
    )
    return run_out(await manager.start("digest", params))


# --- the schedule, as the container can see it (spec 096's amendment 2) ---


class SuccessRecordOut(BaseModel):
    """One last-success record. `overdue` is the domain's rule: no success,
    an unreadable one, or one older than twice the job's longest gap."""

    key: str
    last_success_at: str | None
    summary: str
    overdue: bool


class ScheduledJobOut(BaseModel):
    name: str
    cadence: str
    records: list[SuccessRecordOut]


class ScheduleOut(BaseModel):
    """What the container can read about the schedule, and what it cannot.

    There is no `installed` or `loaded` field on purpose. Only launchctl on
    the host can answer either, and a field this server filled would be a
    guess shown as a fact.
    """

    jobs: list[ScheduledJobOut]
    # The definition loader's words when the schedule cannot be read, and
    # `jobs` is then empty. Required, so a client cannot leave it unread.
    error: str | None
    installed_state: str
    host_command: str


@ops_router.get("/ops/schedule", operation_id="getSchedule")
def get_schedule(conn: Conn) -> ScheduleOut:
    """The cadence of each scheduled job and when it last succeeded.

    No token: job names, cadences and times describe the installation, not
    the operator's search, and spec 050 lists this read as tokenless.
    """
    from harrier.paths import repo_root
    from harrier.schedule import SCHEDULE_CONFIG_PATH, ScheduleConfigError, job_health

    try:
        health = job_health(conn, config_path=repo_root() / SCHEDULE_CONFIG_PATH)
    except ScheduleConfigError as error:
        return ScheduleOut(
            jobs=[],
            error=str(error),
            installed_state=INSTALLED_STATE,
            host_command=SCHEDULE_HOST_COMMAND,
        )
    return ScheduleOut(
        jobs=[
            ScheduledJobOut(
                name=job.name,
                cadence=job.cadence,
                records=[
                    SuccessRecordOut(
                        key=record.key,
                        last_success_at=record.last_success_at,
                        summary=record.summary,
                        overdue=record.overdue,
                    )
                    for record in job.records
                ],
            )
            for job in health
        ],
        error=None,
        installed_state=INSTALLED_STATE,
        host_command=SCHEDULE_HOST_COMMAND,
    )


# --- profile documents (spec 004) ---


class ProfileDocumentOut(BaseModel):
    kind: str
    name: str
    format: str
    updated_at: str


@ops_router.get(
    "/ops/profile",
    operation_id="listProfileDocuments",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def list_profile_documents(conn: Conn) -> list[ProfileDocumentOut]:
    """`profile list`: names and formats, never contents.

    Requires the token although it is a read (spec 096, Akin's decision of
    2026-10-10): the document names describe the operator's own data, the
    reason spec 047 gave for the artifact index.
    """
    from harrier.profile import list_documents

    return [ProfileDocumentOut.model_validate(doc) for doc in list_documents(conn)]


# --- the export, as two downloads (spec 096's amendment 4) ---

CSV_MEDIA_TYPE = "text/csv; charset=utf-8"
CONTACTS_DEFAULT_ONLY = (
    "contacts are the person's, not a track's: download them on the default track"
)

EXPORT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/csv": {}}, "description": "the CSV, as `harrier export` writes it"},
    **TOKEN_RESPONSES,
}


def _download(text: str, filename: str) -> Response:
    # Nothing between here and the browser may keep a copy: contacts.csv
    # holds contact identities, and jobs.csv the operator's own decisions.
    return Response(
        content=text,
        media_type=CSV_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@ops_router.get(
    "/ops/export/jobs.csv",
    operation_id="downloadJobsCsv",
    dependencies=[Depends(require_token)],
    response_class=Response,
    responses=EXPORT_RESPONSES,
)
def download_jobs(
    conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("export"))]
) -> Response:
    """The selected track's `jobs.csv`, in the columns `harrier export`
    writes, with formula cells neutralized. Nothing is written on the server."""
    from harrier.tracker.export import write_jobs

    buffer = io.StringIO(newline="")
    write_jobs(buffer, conn, scope, neutralize=True)
    return _download(buffer.getvalue(), "jobs.csv")


@ops_router.get(
    "/ops/export/contacts.csv",
    operation_id="downloadContactsCsv",
    dependencies=[Depends(require_token)],
    response_class=Response,
    responses=EXPORT_RESPONSES,
)
def download_contacts(
    conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("export"))]
) -> Response:
    """`contacts.csv`, on the default track only, as on the command line."""
    from harrier.tracker.export import write_contacts

    if scope.track.id != DEFAULT_TRACK_ID:
        raise HTTPException(status_code=409, detail=CONTACTS_DEFAULT_ONLY)
    buffer = io.StringIO(newline="")
    write_contacts(buffer, conn, neutralize=True)
    return _download(buffer.getvalue(), "contacts.csv")
