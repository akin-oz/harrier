"""The Settings page's routes, and where every command has its place (spec 096).

Configuration import, the backups list and its verification, what the
container can see about the commands only the host can run, and the three-way
list that places every CLI subcommand: routed, run on the host, or terminal
only. Spec 050 was built first, so the backup itself, the profile list and
the schedule are its routes in `ops_routes.py`, used here rather than repeated.

Every route here requires the token, reads included, for spec 047's reason:
archive names and model metadata describe the operator's own data.

Nothing here runs a host-only command, and nothing here names a host path.
Archives travel by name, a run's output has the backups and data directories
removed from it, and the Gmail token is reported by its presence and age,
never by its name or contents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from harrier.backup import backup_dir, list_archives, listed_archive
from harrier.runoutcome import age_in_days
from harrier.userconfig import ConfigError
from harrier_api.deps import Conn, DatabaseRoute
from harrier_api.localauth import TOKEN_RESPONSES, require_token
from harrier_api.runmodels import Manager, RunOut, run_out
from harrier_api.runs import TERMINAL_STATES, RunManager, RunParams, backup_hidden_directories

settings_router = APIRouter(route_class=DatabaseRoute)

TOKEN = [Depends(require_token)]


class SettingsErrorOut(BaseModel):
    detail: str


# --- where every command has its place ----------------------------------------


@dataclass(frozen=True)
class Routed:
    """A command the browser runs, by the route that runs it."""

    route: str
    note: str | None = None


@dataclass(frozen=True)
class OnHost:
    """A command only the host can run: the exact line to type, and why."""

    shown: str
    reason: str


@dataclass(frozen=True)
class TerminalOnly:
    """A command with no place in the browser, and why."""

    reason: str


Place = Routed | OnHost | TerminalOnly

_MIGRATION = "One-time migrations from the old system."
_CUTOVER = "One irreversible sitting with an attestation (spec 042)."
_PARITY = "Repository upkeep: they compare against the old system."

# Every CLI subcommand, by the name `harrier_cli.main.subcommand_name` gives
# it, in exactly one place. A dictionary, so a command cannot be in two; and
# `tests/test_ui_settings.py::test_every_cli_subcommand_has_exactly_one_place`
# walks the parser, so a command in none fails it.
COMMAND_PLACES: dict[str, Place] = {
    # --- routed (specs 042, 047, 048, 049, 050 as amended, 094, 095, 096) ---
    "shortlist": Routed("POST /tracker/{selector}/status"),
    "track": Routed("POST /tracker/{selector}/status"),
    "interviewing": Routed("POST /tracker/{selector}/status"),
    "applied": Routed("POST /tracker/{selector}/status"),
    "reject": Routed("POST /tracker/{selector}/status"),
    "company-outcome": Routed("POST /tracker/{selector}/outcome"),
    "reevaluate": Routed("POST /tracker/{selector}/rescore"),
    "add": Routed("POST /tracker"),
    "next": Routed("GET /tracker/queue"),
    "review": Routed("GET /tracker/queue"),
    "events show": Routed("GET /tracker/{selector}/events"),
    "events backfill": Routed("POST /ops/events/backfill"),
    "tracks list": Routed("GET /tracks"),
    "tracks add": Routed("POST /tracks"),
    "tracks archive": Routed("POST /tracks/{slug}/archive"),
    "tailor": Routed("POST /apply/{selector}/resume"),
    "cover-letter": Routed("POST /apply/{selector}/cover-letter"),
    "answers": Routed("POST /apply/{selector}/answers"),
    "evaluate": Routed("POST /apply/{selector}/evaluate"),
    "brief show": Routed("GET /apply/{selector}/brief"),
    "brief set": Routed("PUT /apply/{selector}/brief"),
    "find-contacts": Routed("POST /outreach/{selector}/find-contacts"),
    "contacts list": Routed("GET /outreach/contacts"),
    "contacts approve": Routed("POST /outreach/{selector}/candidates/approve"),
    "contacts reject": Routed("POST /outreach/{selector}/candidates/reject"),
    "contacts set-best": Routed("POST /outreach/{selector}/best-contact"),
    "outreach sync": Routed("POST /outreach/sync"),
    "outreach due": Routed("GET /outreach/due"),
    "outreach mark-sent": Routed("POST /outreach/{selector}/sent"),
    "outreach mark-replied": Routed("POST /outreach/{selector}/replied"),
    "outreach snooze": Routed("POST /outreach/{selector}/snooze"),
    "outreach-draft": Routed("POST /outreach/{selector}/draft"),
    "backfill-posters": Routed("POST /outreach/backfill-posters"),
    "gmail-watch": Routed("POST /mail/watch"),
    "discover": Routed(
        "POST /ops/discover",
        note="With its options and uploads; POST /runs with kind discovery starts it with none.",
    ),
    "demo-run": Routed(
        "POST /runs", note="A harness for the run panel, started as a run of kind demo (spec 006)."
    ),
    "check": Routed("GET /ops/check", note="With --link-contacts: POST /ops/check/link-contacts."),
    "evaluate-prospects": Routed("POST /ops/evaluate-prospects"),
    "scoring export": Routed("POST /ops/scoring/export"),
    "reconsider": Routed("POST /ops/reconsider"),
    "digest": Routed("POST /ops/digest"),
    "config check-feeds": Routed("POST /ops/feeds", note="With --prune: POST /ops/feeds/prune."),
    "export": Routed("GET /ops/export/jobs.csv", note="Two downloads: jobs.csv and contacts.csv."),
    "config list": Routed("GET /config"),
    "config get": Routed("GET /config/{kind}"),
    "config set": Routed("PUT /config/{kind}"),
    "config unset": Routed("DELETE /config/{kind}"),
    "config import": Routed("POST /config/import"),
    "profile list": Routed("GET /ops/profile"),
    "backup": Routed("POST /ops/backup"),
    "verify-backup": Routed("POST /settings/backups/{name}/verify"),
    # --- run on the host, shown with the command ---
    "schedule install": OnHost(
        "harrier schedule install",
        "Loads launchd jobs. launchd is on the host, and the container has no launchctl.",
    ),
    "schedule uninstall": OnHost(
        "harrier schedule uninstall", "Unloads launchd jobs, which only the host can reach."
    ),
    "schedule status": OnHost(
        "harrier schedule status",
        "Asks launchd whether each job is installed and loaded, which only the host can answer.",
    ),
    "gmail-oauth": OnHost(
        "harrier gmail-oauth",
        "A browser consent flow that writes the mail token; the container mounts it read-only.",
    ),
    "scoring train": OnHost(
        "harrier scoring train --activate",
        "Needs scikit-learn, which the image does not install (spec 077).",
    ),
    "doctor": OnHost(
        "harrier doctor",
        "Reports who owns the database, which a process inside the container cannot see.",
    ),
    "profile export": OnHost(
        "harrier profile export --to <directory>",
        "Writes the documents into a host directory the container cannot see.",
    ),
    # --- terminal only ---
    "restore": TerminalOnly(
        "Replaces the live database; the case for running it is one where the operator "
        "should be reading carefully (spec 042)."
    ),
    "cutover preflight": TerminalOnly(_CUTOVER),
    "cutover run": TerminalOnly(_CUTOVER),
    "migrate-legacy": TerminalOnly(_MIGRATION),
    "gmail-migrate-state": TerminalOnly(_MIGRATION),
    "profile import": TerminalOnly(_MIGRATION),
    "review-followup": TerminalOnly("Repository upkeep: it reads pull request reviews."),
    "parity checklist": TerminalOnly(_PARITY),
    "parity status": TerminalOnly(_PARITY),
    "parity diff": TerminalOnly(_PARITY),
}

HostFact = Literal["schedule", "gmail_token", "model", "image", "database_owner", "profile"]

# Each fact the host panel shows, beside the commands that would change it
# (spec 096's table). A placeholder in angle brackets stands for anything the
# operator supplies; nothing here is a personal value or a real path, which
# `test_shown_commands_carry_placeholders_only` holds.
HOST_PANEL: tuple[tuple[HostFact, tuple[str, ...]], ...] = (
    ("schedule", ("harrier schedule status", "harrier schedule install")),
    ("gmail_token", ("harrier gmail-oauth",)),
    ("model", ("harrier scoring train --activate",)),
    ("image", ("just container-up",)),
    ("database_owner", ("harrier doctor", "harrier doctor --integrity")),
    ("profile", ("harrier profile export --to <directory>",)),
)


class RoutedCommandOut(BaseModel):
    command: str
    route: str
    note: str | None


class HostCommandOut(BaseModel):
    command: str
    shown: str
    reason: str


class TerminalCommandOut(BaseModel):
    command: str
    reason: str


class HostPanelRowOut(BaseModel):
    fact: HostFact
    commands: list[str]


class CommandPlacesOut(BaseModel):
    routed: list[RoutedCommandOut]
    host: list[HostCommandOut]
    terminal: list[TerminalCommandOut]
    panel: list[HostPanelRowOut]


@settings_router.get(
    "/settings/commands",
    operation_id="listCommandPlaces",
    dependencies=TOKEN,
    responses=TOKEN_RESPONSES,
)
def list_command_places() -> CommandPlacesOut:
    """Every CLI subcommand in its one place, so the page renders the list
    from here rather than from a copy typed into it."""
    routed: list[RoutedCommandOut] = []
    host: list[HostCommandOut] = []
    terminal: list[TerminalCommandOut] = []
    for command in sorted(COMMAND_PLACES):
        place = COMMAND_PLACES[command]
        if isinstance(place, Routed):
            routed.append(RoutedCommandOut(command=command, route=place.route, note=place.note))
        elif isinstance(place, OnHost):
            host.append(HostCommandOut(command=command, shown=place.shown, reason=place.reason))
        else:
            terminal.append(TerminalCommandOut(command=command, reason=place.reason))
    return CommandPlacesOut(
        routed=routed,
        host=host,
        terminal=terminal,
        panel=[HostPanelRowOut(fact=fact, commands=list(lines)) for fact, lines in HOST_PANEL],
    )


# --- configuration import -------------------------------------------------------


class ImportedKindOut(BaseModel):
    kind: str
    count: int
    unit: str


class ConfigImportOut(BaseModel):
    imported: list[ImportedKindOut]
    skipped: list[str]
    # The command's own report, line for line.
    report: list[str]
    total: int


NOTHING_TO_IMPORT = "nothing to import; no configuration files found"


@settings_router.post(
    "/config/import",
    operation_id="importConfig",
    dependencies=TOKEN,
    responses={
        400: {"model": SettingsErrorOut, "description": "a file holds a value the store refuses"},
        409: {"model": SettingsErrorOut, "description": "no configuration file was found"},
        **TOKEN_RESPONSES,
    },
)
def import_configuration(conn: Conn) -> ConfigImportOut:
    """`harrier config import`: the same function, overwriting each stored
    kind that has a file in the checkout."""
    from harrier.userconfig.importer import import_config_files

    try:
        result = import_config_files(conn)
    except ConfigError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not result.imported:
        raise HTTPException(status_code=409, detail=NOTHING_TO_IMPORT)
    return ConfigImportOut(
        imported=[
            ImportedKindOut(kind=entry.kind, count=entry.count, unit=entry.unit)
            for entry in result.imported
        ],
        skipped=list(result.skipped),
        report=list(result.report),
        total=result.total,
    )


class FeedLinesIn(BaseModel):
    urls: list[str]


class UnroutedFeedOut(BaseModel):
    url: str
    message: str


class FeedRoutingOut(BaseModel):
    unrouted: list[UnroutedFeedOut]


@settings_router.post(
    "/settings/feeds/routing",
    operation_id="routeFeeds",
    dependencies=TOKEN,
    responses=TOKEN_RESPONSES,
)
def route_feeds(body: FeedLinesIn) -> FeedRoutingOut:
    """Which watchlist lines no importer handles, in spec 041's words.

    Stores nothing and fetches nothing: it runs the router discovery runs,
    so the editor cannot disagree with it about a URL.
    """
    from harrier.sources.feeds import UNROUTED, route_ats_feeds

    urls = [url.strip() for url in body.urls if url.strip()]
    unrouted = route_ats_feeds(urls)[UNROUTED]
    return FeedRoutingOut(
        unrouted=[
            UnroutedFeedOut(url=url, message=f"no importer handles {url}") for url in unrouted
        ]
    )


# --- backups --------------------------------------------------------------------

VERIFY_KIND = "verify-backup"


class ArchiveOut(BaseModel):
    name: str
    size_bytes: int
    modified_at: str
    # From the most recent verification this server ran, if any.
    verification: Literal["passed", "failed", "running"] | None


class BackupsOut(BaseModel):
    # Absent when the backups directory is not mounted or not created yet.
    directory: Literal["present", "absent"]
    archives: list[ArchiveOut]


BACKUP_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": SettingsErrorOut, "description": "no archive of that name in the listing"},
    **TOKEN_RESPONSES,
}


def _verification(manager: RunManager, name: str) -> Literal["passed", "failed", "running"] | None:
    """The latest verification of this archive the run registry knows of."""
    for run in manager.list_runs():
        if run.kind != VERIFY_KIND or run.target != name:
            continue
        if run.state not in TERMINAL_STATES:
            return "running"
        if run.state == "succeeded":
            return "passed"
        if run.state == "failed":
            return "failed"
        return None
    return None


@settings_router.get(
    "/settings/backups",
    operation_id="listBackups",
    dependencies=TOKEN,
    responses=TOKEN_RESPONSES,
)
def list_backups(manager: Manager) -> BackupsOut:
    """The archives in the backups directory the container mounts (spec 064),
    newest first."""
    directory = backup_dir()
    return BackupsOut(
        directory="present" if directory.is_dir() else "absent",
        archives=[
            ArchiveOut(
                name=item.name,
                size_bytes=item.size_bytes,
                modified_at=item.modified_at,
                verification=_verification(manager, item.name),
            )
            for item in list_archives(directory)
        ],
    )


@settings_router.post(
    "/settings/backups/{name}/verify",
    operation_id="verifyBackup",
    dependencies=TOKEN,
    responses=BACKUP_ERRORS,
)
async def verify_backup(name: str, manager: Manager) -> RunOut:
    """`harrier verify-backup` on an archive from the listing. A name the
    listing does not contain is 404, and a path is never such a name."""
    archive = listed_archive(name)
    if archive is None:
        raise HTTPException(status_code=404, detail=f"no archive named {name} in the backups list")
    # Locked per archive, and recorded under its name so the list can mark
    # the result.
    params = RunParams(archive=archive, hidden=backup_hidden_directories(), target=name)
    return run_out(await manager.start(VERIFY_KIND, params))


# --- what the container can see about host-only commands ------------------------

UNKNOWN = "unknown"
# Read where `harrier.mail.watch.env_config` reads it. Only the presence and
# age of the file it names are reported, never the name.
GMAIL_TOKEN_ENV = "GMAIL_OAUTH_TOKEN_FILE"


class GmailTokenOut(BaseModel):
    state: Literal["present", "absent", "not_configured"]
    age_days: int | None


class ActiveModelOut(BaseModel):
    state: Literal["active", "missing", "invalid"]
    trained_at: str | None
    version: str | None


class HostFactsOut(BaseModel):
    """What the container can read about each host-only command, and no more.

    The schedule is not repeated here: spec 050's `GET /ops/schedule` reads
    the same records and says the installed state is the host's to report.
    The one fact here the container cannot read is typed as the one value
    `unknown`, so the contract itself cannot carry a healthy answer for it.
    """

    gmail_token: GmailTokenOut
    model: ActiveModelOut
    newest_feature_export: str | None
    image_revision: str
    database_owner: Literal["unknown"]


def _gmail_token() -> GmailTokenOut:
    raw = os.environ.get(GMAIL_TOKEN_ENV, "").strip()
    if not raw:
        return GmailTokenOut(state="not_configured", age_days=None)
    try:
        stat = Path(raw).expanduser().stat()
    except OSError:
        return GmailTokenOut(state="absent", age_days=None)
    modified = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
    return GmailTokenOut(state="present", age_days=age_in_days(modified))


def _active_model() -> ActiveModelOut:
    from harrier.scoring.model import MODEL_ACTIVE, MODEL_MISSING, active_model_info

    info = active_model_info()
    if info.state == MODEL_ACTIVE:
        return ActiveModelOut(state="active", trained_at=info.trained_at, version=info.version)
    return ActiveModelOut(
        state="missing" if info.state == MODEL_MISSING else "invalid",
        trained_at=None,
        version=None,
    )


@settings_router.get(
    "/settings/host",
    operation_id="getHostFacts",
    dependencies=TOKEN,
    responses=TOKEN_RESPONSES,
)
def host_facts() -> HostFactsOut:
    """The facts the host panel shows beside each command (spec 096's table),
    apart from the schedule, which is `GET /ops/schedule`. Opens no database,
    so it answers while a host process holds it."""
    from harrier.scoring.export import newest_export_date
    from harrier_api.app import build_revision

    return HostFactsOut(
        gmail_token=_gmail_token(),
        model=_active_model(),
        newest_feature_export=newest_export_date(),
        image_revision=build_revision(),
        database_owner=UNKNOWN,
    )
