"""One logging configuration, applied at every entry point (spec 029).

Before this, no module called `basicConfig` or `dictConfig`. Every module
built a logger and nothing ever configured the root, so `logger.info` was
discarded outright and `logger.warning` went through logging's last-resort
handler: bare message, no timestamp, no level, no rotation, appended forever
to whatever file the scheduler redirected stderr into.

The practical cost was not tidiness. The Apify cost gate logged its skip at
INFO, so a paid source deciding not to run was indistinguishable from demo
mode and from a crash, in a system whose founding failure was a job that
stopped running without saying so.

Called once per process, by the CLI and by the API. Idempotent, because a
second call adding a second handler is how a line gets logged twice and a
log gets read as two runs.

That sentence was false until spec 045: only the CLI called it, so the process
serving the browser had none of this. `create_app` calls it now, and
`test_logging.py::test_the_api_configures_logging_when_the_app_is_created`
fails if the call is removed again.

Handlers also carry the identity redaction filter (`harrier.logredact`), which
the privacy plan claimed existed and which did not, and before it the secret
scrub, which keeps the store password out of the log (spec 133) and a driver
error's text with it (spec 135).

Hosted, logging opens nothing: no database, so no identity values and no
identity filter, and no file (spec 135). The identity values hosted are the
owners' rows on Postgres, which the API's login role may not read at start.
A hosted run is not the hosted API: it reads its owner's identity values
from its own store (spec 106).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from harrier.connection import (
    RUN_OWNER_VARIABLE,
    StoreDatabaseError,
    StoreError,
    in_a_hosted_run,
    open_store,
)
from harrier.db import DatabaseOwnedByContainer, DatabaseOwnershipError, connect, data_dir
from harrier.logredact import ACCESS_QUERY, SECRET_SCRUB, forget_all

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "harrier.log"
LEVEL_ENV = "HARRIER_LOG_LEVEL"

# Bounded so an unattended machine cannot fill its disk with a repeating
# failure, which is the failure mode most likely to be logging heavily.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

HOSTED_NOTE = (
    "hosted: identity values are not loaded, "
    "and driver errors are logged without their text (spec 135)"
)

_configured = False

# uvicorn's own loggers, which do not propagate to the root, so a filter on
# the root handlers never sees their records. `uvicorn.error` propagates to
# `uvicorn` (uvicorn/config.py, LOGGING_CONFIG).
UVICORN_LOGGERS = ("uvicorn", "uvicorn.access")
ACCESS_LOGGER = "uvicorn.access"


def log_path() -> Path:
    return data_dir() / LOG_DIR_NAME / LOG_FILE_NAME


def configure_logging(*, force: bool = False, hosted: bool = False) -> None:
    """Set up the root logger once.

    The level comes from the environment so a quiet default can be turned up
    without a code change, which matters because the interesting lines here
    are the ones that explain why a run did less than expected.

    `hosted` is the hosted API's: stderr only, which the platform collects,
    and nothing opened under the data directory (spec 135).
    """
    global _configured
    if _configured and not force:
        return

    level_name = os.environ.get(LEVEL_ENV, "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(FORMAT)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    # Before anything else is installed, and on every path below, the two
    # that install no identity filter included (spec 133).
    stream.addFilter(SECRET_SCRUB)
    root.addHandler(stream)

    if hosted:
        # No local tracker exists hosted, and opening one created an empty
        # harrier.db under the data directory at every start. Nothing else
        # is installed: a filter registered by an earlier local call would
        # read identity values through the request's connection.
        forget_all()
        root.info(HOSTED_NOTE)
        _configured = True
        return

    # The redaction values come from the database, so they are loaded before
    # the log file is opened. When the container owns the database this
    # process may not read them, and a line it wrote to harrier.log would be
    # unredacted in the one file the container also writes. So it writes no
    # file at all and keeps stderr. The refusal itself is not swallowed: a
    # command that opens the database is refused at its own open and exits
    # 75 (spec 061, amended: the first text let the refusal out of here,
    # which refused every host command, `harrier doctor` included).
    try:
        values = _load_identity_values()
    except DatabaseOwnershipError as error:
        # Named by cause: "the container owns it" when ownership is only
        # unknown sends the operator after the wrong problem (review finding
        # on PR #110).
        cause = (
            "the harrier container owns the database"
            if isinstance(error, DatabaseOwnedByContainer)
            else "cannot tell who owns the database"
        )
        root.warning("%s; logging to stderr only, without identity redaction", cause)
        _configured = True
        return

    # A file handler is best effort. A read-only or missing data directory
    # must not stop the program from running: losing the log is bad, refusing
    # to work because the log cannot be opened is worse.
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        rotating.addFilter(SECRET_SCRUB)
        root.addHandler(rotating)
    except OSError:
        root.warning("file logging is unavailable; logging to stderr only")

    _install_identity_redaction(root, values)

    _configured = True


def scrub_last_resort() -> None:
    """Install the secret scrub on logging's last-resort handler.

    A process that never configures logging writes a warning through
    `logging.lastResort`, unfiltered. `harrier store` and `harrier doctor`
    skip `configure_logging` on purpose (it would open the database), and
    `harrier store` is the one command that runs on Postgres, where the
    driver logs its own warnings (spec 133). The CLI calls this before any
    command runs.
    """
    if logging.lastResort is not None:
        logging.lastResort.addFilter(SECRET_SCRUB)


def scrub_uvicorn_handlers() -> None:
    """Install the secret scrub on every handler of uvicorn's loggers.

    Called by the API when the app is built, which uvicorn does after it
    configures its logging, so its handlers exist by then (spec 133). An
    unhandled exception is logged by uvicorn, not by the app.
    """
    for name in UVICORN_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(SECRET_SCRUB)


def drop_access_queries() -> None:
    """Install the query filter on uvicorn's access handlers.

    Called by the hosted API when the app is built, as
    `scrub_uvicorn_handlers` is, so the handlers uvicorn configured before
    it built the app carry it (spec 135). First on each handler, before
    the secret scrub, as the spec says. For uvicorn's access record the
    order shows in no output: the scrub masks each argument in place and
    keeps all five, so the query filter finds the path either way.
    """
    for handler in logging.getLogger(ACCESS_LOGGER).handlers:
        if ACCESS_QUERY not in handler.filters:
            handler.filters.insert(0, ACCESS_QUERY)


def _load_identity_values() -> set[str] | None:
    """The identity values, or None when the database cannot be read.

    Best effort for the same reason the file handler is: there is no database
    on a fresh clone, and refusing to log because the profile store is not
    there yet would make the tool unusable before it is configured. Raises
    `DatabaseOwnershipError`, which is neither error caught here.
    """
    from harrier.logredact import identity_values

    # closing(), not the connection as its own context manager: that one
    # commits but does not close, and an unclosed connection lives until gc
    # and checkpoints the WAL whenever that happens (spec 076).
    # A read through the connection fails as StoreDatabaseError (spec 112);
    # opening the file still raises the driver's own classes, since
    # `harrier.db` works on the SQLite file itself.
    if in_a_hosted_run():
        # The run's own Postgres store as its owner, so the values are the
        # owner's and no SQLite file is created (spec 106). Any refusal of
        # the open or the read means no redaction, which the caller says.
        try:
            with closing(open_store(owner=os.environ[RUN_OWNER_VARIABLE])) as owned:
                return identity_values(owned)
        except StoreError:
            return None
    try:
        with closing(connect()) as conn:
            return identity_values(conn)
    except (StoreDatabaseError, sqlite3.Error, OSError):
        return None


def _install_identity_redaction(root: logging.Logger, values: set[str] | None) -> None:
    """Filter every handler with the identity values (spec 045).

    A failure to load them means no redaction, so it says so rather than
    passing silently.
    """
    from harrier.logredact import IdentityRedactionFilter, register

    if values is None:
        root.warning("identity redaction is unavailable; logs are not redacted")
        return

    redaction = IdentityRedactionFilter(values)
    for handler in root.handlers:
        handler.addFilter(redaction)
    # Registered so the tracker write path can refresh it when a contact is
    # added, rather than leaving that contact unredacted until restart.
    forget_all()
    register(redaction)
