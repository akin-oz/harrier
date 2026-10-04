"""Is the harrier container running, and what has it mounted as its data? (spec 061)

The tracker database is opened in WAL mode, and WAL needs every process using
the file to share one kernel. The container runs under the Docker Desktop VM;
the host CLI runs under macOS. Before the host opens the live database it has
to know whether the container already has it. This module answers that
question and nothing else: `harrier.db` decides what the answer means.

It asks the Docker engine over its unix socket rather than running the
`docker` binary. launchd starts processes with a minimal PATH, so the binary
is not reliably findable, and a socket request needs no subprocess on the
path the API takes for every request inside the container, where no socket is
mounted and the answer is an immediate "unreachable".

Two failures are deliberately different:

- **Unreachable** (no socket, or a socket nobody is listening on): there is no
  engine, so there is no container. The host may open the file.
- **Unknown** (an engine that answers badly, or not within the bound): the
  container may well be running. The host may not open the file.

Reasons are fixed phrases, never an exception's text. An `OSError` carries the
socket path, which contains a home directory, and `harrier doctor` prints the
reason (spec 061, "no absolute path in doctor output").
"""

from __future__ import annotations

import http.client
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CONTAINER_NAME = "harrier"
DATA_MOUNT = "/app/data"

# Long enough for an engine that is merely busy, short enough that a scheduled
# run against an engine resuming from sleep fails in seconds rather than
# hanging until launchd fires the next one.
DEFAULT_TIMEOUT_SECONDS = 3.0

UNREACHABLE = "unreachable"
REACHABLE = "reachable"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class ContainerState:
    """What the engine said about the harrier container.

    `engine` is one of UNREACHABLE, REACHABLE, UNKNOWN. `data_source` is the
    host directory mounted at /app/data, set only when the container is
    running with such a mount. `reason` explains UNKNOWN in a fixed phrase.
    """

    engine: str
    running: bool = False
    data_source: Path | None = None
    reason: str | None = None


def socket_candidates() -> list[Path]:
    """Where the engine's socket may be, in the order they are tried.

    DOCKER_HOST wins when it names a unix socket, as it does for the docker
    CLI. Otherwise the system path, then Docker Desktop's per-user one, which
    is the only one present when the system symlink is turned off.
    """
    configured = os.environ.get("DOCKER_HOST", "").strip()
    if configured.startswith("unix://"):
        return [Path(configured.removeprefix("unix://"))]
    return [Path("/var/run/docker.sock"), Path.home() / ".docker" / "run" / "docker.sock"]


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self._socket_path))
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def detect(timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ContainerState:
    """Ask the engine about the harrier container. Never raises."""
    sockets = [path for path in socket_candidates() if path.exists()]
    if not sockets:
        return ContainerState(engine=UNREACHABLE)
    for path in sockets:
        state = _ask(path, timeout)
        if state.engine != UNREACHABLE:
            return state
    return ContainerState(engine=UNREACHABLE)


def _ask(path: Path, timeout: float) -> ContainerState:
    connection = _UnixConnection(path, timeout)
    try:
        try:
            connection.connect()
        except (FileNotFoundError, ConnectionRefusedError):
            # A socket file with nobody behind it: Docker Desktop quit. No
            # engine, so no container.
            return ContainerState(engine=UNREACHABLE)
        except PermissionError:
            # Still unknown, because an engine may be running behind it, but
            # named so `harrier doctor` says what to fix (review finding on
            # PR #110).
            return ContainerState(
                engine=UNKNOWN, reason="the docker engine socket is not accessible"
            )
        connection.request("GET", f"/containers/{CONTAINER_NAME}/json")
        response = connection.getresponse()
        body = response.read()
    except TimeoutError:
        return ContainerState(engine=UNKNOWN, reason="the docker engine did not answer in time")
    except (OSError, http.client.HTTPException):
        return ContainerState(engine=UNKNOWN, reason="the docker engine connection failed")
    finally:
        connection.close()

    if response.status == 404:
        return ContainerState(engine=REACHABLE)
    if response.status != 200:
        return ContainerState(
            engine=UNKNOWN, reason=f"the docker engine answered HTTP {response.status}"
        )
    try:
        return _parse(json.loads(body))
    except (ValueError, TypeError, KeyError):
        return ContainerState(engine=UNKNOWN, reason="the docker engine answer was not readable")


def _parse(document: object) -> ContainerState:
    data = cast("dict[str, object]", document)
    state = cast("dict[str, object]", data["State"])
    if state.get("Running") is not True:
        return ContainerState(engine=REACHABLE)
    for mount in cast("list[dict[str, object]]", data.get("Mounts") or []):
        if mount.get("Destination") == DATA_MOUNT:
            source = mount.get("Source")
            if isinstance(source, str) and source:
                return ContainerState(engine=REACHABLE, running=True, data_source=Path(source))
    return ContainerState(engine=REACHABLE, running=True)
