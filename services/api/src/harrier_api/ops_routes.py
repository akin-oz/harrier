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
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from harrier.discovery import APIFY_MAX_COUNT, SOURCE_ORDER
from harrier.tracks import DEFAULT_TRACK_ID
from harrier.tracks import Scope as TrackScope
from harrier_api.deps import Conn, DatabaseRoute, ErrorOut, scope_for
from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import RUN_CONFLICT_RESPONSES, Manager, RunOut, run_out
from harrier_api.runs import RunParams, write_run_input

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
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def check_feeds(manager: Manager) -> RunOut:
    """`config check-feeds` as a run: one probe per configured board.

    A run, and a POST, because it starts a process that reaches the network.
    Its results are the run's log, which `GET /runs/{id}/events` serves
    without the token, as spec 050 states for feed results.
    """
    params = RunParams()
    return run_out(await manager.start("check-feeds", params))


@ops_router.post(
    "/ops/feeds/prune",
    operation_id="pruneDeadFeeds",
    dependencies=[Depends(require_token)],
    responses={**REFUSAL_RESPONSES, **RUN_CONFLICT_RESPONSES},
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
    params = RunParams(switches=frozenset({"--prune"}))
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
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
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
        # One per track: a report and a clear of the same seen state never
        # run at once (review of PR #208).
        target=scope.track.slug,
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
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def take_backup(manager: Manager, body: BackupIn = BackupIn()) -> RunOut:  # noqa: B008
    params = RunParams(switches=frozenset() if body.prune else frozenset({"--no-prune"}))
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
    responses={**REFUSAL_RESPONSES, **RUN_CONFLICT_RESPONSES},
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


@ops_router.get("/ops/profile", operation_id="listProfileDocuments")
def list_profile_documents(conn: Conn) -> list[ProfileDocumentOut]:
    """`profile list`: names and formats, never contents."""
    from harrier.profile import list_documents

    return [ProfileDocumentOut.model_validate(doc) for doc in list_documents(conn)]


# --- spec 095: the commands that work on the operator's data ---------------------

# What an upload may weigh. The batch exports are small text files; the cap is
# there so a wrong file cannot fill the disk (spec 095).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024


class InvariantBreachOut(BaseModel):
    job_id: str
    breach: str


class UnresolvedLinkOut(BaseModel):
    contact: str
    breach: str


class DataCheckOut(BaseModel):
    """What `check` reports, in the domain's words. A contact is named by
    its name or profile URL, which is why the route requires the token."""

    breaches: list[InvariantBreachOut]
    unresolved_links: list[UnresolvedLinkOut]


@ops_router.get(
    "/ops/check",
    operation_id="checkData",
    dependencies=[Depends(require_token)],
    responses=TOKEN_RESPONSES,
)
def check_data(
    conn: Conn, scope: Annotated[TrackScope, Depends(scope_for("check"))]
) -> DataCheckOut:
    """`check` without `--link-contacts`: reports, and changes nothing."""
    from harrier.outreach.joblink import unresolved_links
    from harrier.tracker.invariants import check_rows
    from harrier.tracker.store import list_jobs

    return DataCheckOut(
        breaches=[
            InvariantBreachOut(job_id=str(job_id), breach=breach)
            for job_id, breach in check_rows(list_jobs(conn, scope))
        ],
        unresolved_links=[
            UnresolvedLinkOut(contact=who, breach=breach)
            for who, breach in unresolved_links(conn, scope)
        ],
    )


class LinkContactsIn(BaseModel):
    """Linking edits stored contacts, so an empty body refuses."""

    confirm: bool = False


class LinkContactsOut(BaseModel):
    """What the write did, counted when it ran rather than when it was
    previewed."""

    linked: int
    unmatched: int


@ops_router.post(
    "/ops/check/link-contacts",
    operation_id="linkContactIds",
    dependencies=[Depends(require_token)],
    responses=REFUSAL_RESPONSES,
)
def link_contact_ids(
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("check"))],
    body: LinkContactsIn = LinkContactsIn(),  # noqa: B008
) -> LinkContactsOut:
    """`check --link-contacts`: gives existing contact links the job id they
    were written without, and drops nothing (spec 036)."""
    from harrier.outreach.joblink import backfill_job_ids

    if not body.confirm:
        raise HTTPException(
            status_code=409,
            detail="linking writes job ids into stored contacts; send confirm: true to link them",
        )
    linked, unmatched = backfill_job_ids(conn, scope)
    return LinkContactsOut(linked=linked, unmatched=unmatched)


class EventsBackfillIn(BaseModel):
    """A dry run unless the body says otherwise: the counts first."""

    dry_run: bool = True


@ops_router.post(
    "/ops/events/backfill",
    operation_id="backfillEvents",
    dependencies=[Depends(require_token)],
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def backfill_events_route(
    manager: Manager,
    scope: Annotated[TrackScope, Depends(scope_for("events backfill"))],
    body: EventsBackfillIn = EventsBackfillIn(),  # noqa: B008
) -> RunOut:
    """`events backfill` as a run: it walks the whole tracker."""
    del scope  # the default track's; any other is refused by the dependency
    params = RunParams(
        switches=frozenset({"--dry-run"}) if body.dry_run else frozenset(),
    )
    return run_out(await manager.start("events-backfill", params))


class EvaluateProspectsIn(BaseModel):
    """Evaluate and report; reject nothing unless `apply`. An absent field
    is the CLI's own default, so each default has one definition."""

    apply: bool = False
    threshold: float | None = Field(default=None, ge=0, le=1)
    limit: int | None = Field(default=None, ge=1)
    refresh: bool = False
    include_borderline: bool = False


@ops_router.post(
    "/ops/evaluate-prospects",
    operation_id="evaluateProspects",
    dependencies=[Depends(require_token)],
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def evaluate_prospects_route(
    manager: Manager,
    scope: Annotated[TrackScope, Depends(scope_for("evaluate-prospects"))],
    body: EvaluateProspectsIn = EvaluateProspectsIn(),  # noqa: B008
) -> RunOut:
    """`evaluate-prospects` as a run: it calls a model per prospect."""
    del scope
    switches = {
        flag
        for flag, wanted in (
            ("--apply", body.apply),
            ("--refresh", body.refresh),
            ("--include-borderline", body.include_borderline),
        )
        if wanted
    }
    params = RunParams(
        switches=frozenset(switches),
        numbers={} if body.limit is None else {"--limit": body.limit},
        fractions={} if body.threshold is None else {"--threshold": body.threshold},
    )
    return run_out(await manager.start("evaluate-prospects", params))


@ops_router.post(
    "/ops/scoring/export",
    operation_id="exportScoringFeatures",
    dependencies=[Depends(require_token)],
    responses={**TOKEN_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def export_scoring_features(
    manager: Manager, scope: Annotated[TrackScope, Depends(scope_for("scoring export"))]
) -> RunOut:
    """`scoring export` as a run. Training reads only the export and needs
    scikit-learn, which the image does not install, so it stays on the host
    (spec 077)."""
    del scope
    return run_out(await manager.start("scoring-export", RunParams()))


DISCOVER_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorOut, "description": "discovery from the browser is the default track's"},
    413: {"model": ErrorOut, "description": "an upload is larger than the cap"},
    **TOKEN_RESPONSES,
}


def _suffix(upload: UploadFile) -> str:
    """The importer reads a `.csv` as CSV and anything else as JSON, so the
    suffix is chosen from that closed set, never taken from the upload."""
    return ".csv" if (upload.filename or "").lower().endswith(".csv") else ".json"


@ops_router.post(
    "/ops/discover",
    operation_id="runDiscover",
    dependencies=[Depends(require_token)],
    responses={**DISCOVER_RESPONSES, **RUN_CONFLICT_RESPONSES},
)
async def run_discover(
    manager: Manager,
    conn: Conn,
    scope: Annotated[TrackScope, Depends(scope_for("discover"))],
    dry_run: Annotated[bool, Form()] = False,
    notify: Annotated[bool, Form()] = True,
    shadow: Annotated[bool, Form()] = False,
    only_source: Annotated[SourceName | None, Form()] = None,
    apify_count: Annotated[int | None, Form(ge=1, le=APIFY_MAX_COUNT)] = None,
    dataset_file: Annotated[UploadFile | None, File()] = None,
    wellfound_file: Annotated[UploadFile | None, File()] = None,
    wttj_file: Annotated[UploadFile | None, File()] = None,
) -> RunOut:
    """`discover` with its options, as a run.

    Each upload is read and checked against the cap before any is written,
    so a refused request writes nothing. Each is then written owner-only to
    the run inputs and passed by path; the run removes them when it ends. No
    browser-supplied path reaches argv. `shadow` is passed as `--shadow`
    alone: the CLI's options make it a dry run, one definition of that rule.
    """
    from harrier.discovery import scheduled_apify_count

    if scope.track.id != DEFAULT_TRACK_ID:
        # `discover` runs on an academic track from its own search entry on
        # the command line (spec 097); from the browser it is the default
        # track's (spec 095).
        raise HTTPException(
            status_code=409, detail=f"discover is not available on track {scope.track.slug}"
        )
    uploads = {
        flag: upload
        for flag, upload in (
            ("--dataset-file", dataset_file),
            ("--wellfound-file", wellfound_file),
            ("--wttj-file", wttj_file),
        )
        if upload is not None
    }
    contents: dict[str, tuple[bytes, str]] = {}
    for flag, upload in uploads.items():
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{flag.removeprefix('--')} is larger than 5 MB; nothing was written",
            )
        contents[flag] = (data, _suffix(upload))
    written: dict[str, Path] = {}
    try:
        for flag, (data, suffix) in contents.items():
            written[flag] = write_run_input(data, suffix)
    except OSError:
        # Every file this attempt wrote, not only the first.
        for path in written.values():
            path.unlink(missing_ok=True)
        raise
    switches = {
        flag
        for flag, wanted in (
            ("--dry-run", dry_run),
            ("--no-notify", not notify),
            ("--shadow", shadow),
        )
        if wanted
    }
    count = apify_count if apify_count is not None else scheduled_apify_count(conn=conn)
    params = RunParams(
        switches=frozenset(switches),
        numbers={"--apify-count": count},
        choices={} if only_source is None else {"--only-source": only_source.value},
        input_files=written,
    )
    return run_out(await manager.start("discovery", params))
