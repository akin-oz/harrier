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
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from harrier.db import data_dir

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

    _configured = True
