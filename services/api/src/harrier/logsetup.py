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
the privacy plan claimed existed and which did not.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
from pathlib import Path

from harrier.db import connect, data_dir

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "harrier.log"
LEVEL_ENV = "HARRIER_LOG_LEVEL"

# Bounded so an unattended machine cannot fill its disk with a repeating
# failure, which is the failure mode most likely to be logging heavily.
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

_configured = False


def log_path() -> Path:
    return data_dir() / LOG_DIR_NAME / LOG_FILE_NAME


def configure_logging(*, force: bool = False) -> None:
    """Set up the root logger once.

    The level comes from the environment so a quiet default can be turned up
    without a code change, which matters because the interesting lines here
    are the ones that explain why a run did less than expected.
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
    root.addHandler(stream)

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
        root.addHandler(rotating)
    except OSError:
        root.warning("file logging is unavailable; logging to stderr only")

    _install_identity_redaction(root)

    _configured = True


def _install_identity_redaction(root: logging.Logger) -> None:
    """Load the identity values once and filter every handler (spec 045).

    Best effort for the same reason the file handler is: there is no database
    on a fresh clone, and refusing to log because the profile store is not
    there yet would make the tool unusable before it is configured. A failure
    here means no redaction, so it says so rather than passing silently.
    """
    from harrier.logredact import IdentityRedactionFilter, forget_all, identity_values, register

    try:
        with connect() as conn:
            values = identity_values(conn)
    except (sqlite3.Error, OSError):
        root.warning("identity redaction is unavailable; logs are not redacted")
        return

    redaction = IdentityRedactionFilter(values)
    for handler in root.handlers:
        handler.addFilter(redaction)
    # Registered so the tracker write path can refresh it when a contact is
    # added, rather than leaving that contact unredacted until restart.
    forget_all()
    register(redaction)
