"""Run a host command inside the container that owns the database (spec 074).

While the harrier container runs, spec 061 refuses the host the tracker
database. That is safe and, for the launchd schedule, a dead end: launchd
fires on the host and the container is up nearly all the time. This module
runs the same command inside the container instead, so one kernel still opens
the file and the host still gets the result.

It runs `docker exec`, not the engine's exec API over the socket. The engine
API multiplexes stdout and stderr into one framed stream and needs its own
stdin plumbing; the binary already does both and passes the exit status
through. Its path is resolved without relying on PATH, which launchd keeps
minimal: `HARRIER_DOCKER_BIN` first, then PATH, then where Docker Desktop and
Homebrew install it.

The argument vector is passed as a vector, never joined into a shell string,
and the same vector the host was given: the container's `harrier` is the same
CLI, so no translation happens and none can go wrong.

This process writes nothing to `data/logs/harrier.log`. It may not open the
database, so it cannot load the redaction values, and an unredacted line in
the log the container also writes is the failure spec 045 exists to prevent.
What it says goes to stderr as fixed text: the subcommand, never an argument.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from harrier import container
from harrier.paths import repo_root

DOCKER_BIN_ENV = "HARRIER_DOCKER_BIN"

# Where the binary lives when PATH does not say: Docker Desktop's symlink,
# Homebrew on Apple silicon, and the app bundle itself.
_DOCKER_FALLBACKS = (
    Path("/usr/local/bin/docker"),
    Path("/opt/homebrew/bin/docker"),
    Path("/Applications/Docker.app/Contents/Resources/bin/docker"),
)

# Replaced by tests with a fake that records the call.
Runner = Callable[[Sequence[str]], int]


def _run(command: Sequence[str]) -> int:
    # stdin, stdout and stderr are inherited, so `config set` reading stdin and
    # a long run printing as it goes both behave as they do on the host.
    return subprocess.run(list(command), check=False).returncode


run_process: Runner = _run


class DelegationError(Exception):
    """The command could not be handed to the container at all."""


def docker_binary() -> str:
    configured = os.environ.get(DOCKER_BIN_ENV, "").strip()
    if configured:
        return configured
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _DOCKER_FALLBACKS:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise DelegationError(f"the docker binary was not found; set {DOCKER_BIN_ENV} to its path")


def working_tree_revision() -> str | None:
    """This checkout's revision, in the form `just container-up` stamps.

    `<short sha>` plus `-dirty` when there are uncommitted changes, so it is
    comparable with the container's HARRIER_REVISION. None when git cannot
    say, and then no comparison is made rather than a false one.
    """
    root = repo_root()
    try:
        sha = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not sha:
        return None
    return f"{sha}-dirty" if status.strip() else sha


def delegate(argv: Sequence[str], subcommand: str) -> int:
    """Run `harrier <argv>` inside the container. Returns its exit status."""
    try:
        docker = docker_binary()
    except DelegationError as error:
        print(f"harrier {subcommand}: cannot run inside the container: {error}", file=sys.stderr)
        return 1

    state = container.detect()
    here = working_tree_revision()
    if state.revision and here and state.revision != here:
        # First, so it is the line a reader of launchd's log sees first.
        print(
            f"harrier {subcommand}: the {container.CONTAINER_NAME} container runs revision "
            f"{state.revision}, this checkout is {here}; `just container-up` rebuilds it",
            file=sys.stderr,
        )
    print(
        f"harrier {subcommand}: running inside the {container.CONTAINER_NAME} container "
        f"(revision {state.revision or 'unknown'})",
        file=sys.stderr,
    )

    # -i keeps stdin attached (`config set` reads it); no -t, so no TTY is
    # requested and the call behaves the same under launchd as in a terminal.
    try:
        code = run_process([docker, "exec", "-i", container.CONTAINER_NAME, "harrier", *argv])
    except OSError as error:
        # A binary that cannot start (a wrong HARRIER_DOCKER_BIN) used to
        # escape as a traceback (review finding on PR #112). `strerror`, not
        # the exception's text, which carries the configured path.
        print(
            f"harrier {subcommand}: cannot run inside the container: "
            f"the docker binary could not start ({error.strerror or type(error).__name__})",
            file=sys.stderr,
        )
        return 1
    if code != 0 and _container_stopped():
        # Not retried on the host: the container may be coming back, and a
        # host run now would be the two-kernel access this exists to prevent.
        print(
            f"harrier {subcommand}: delegated run interrupted: container stopped",
            file=sys.stderr,
        )
    return code


def _container_stopped() -> bool:
    """Whether the container is known to have stopped.

    Known when the engine answers that it is not running, or when there is no
    engine, which takes the container with it. An engine that did not answer
    says nothing about the container, and a delegated command can fail for
    its own reasons (review finding on PR #112).
    """
    after = container.detect()
    if after.engine == container.UNREACHABLE:
        return True
    return after.engine == container.REACHABLE and not after.running
