"""Hosted runs: their binding, their records, and the machine kept awake
(spec 106).

The run manager holds one `HostedRuns` when it serves hosted. Each method
that touches the store opens a transaction of its own on the API's login,
as the run's owner: `SET LOCAL ROLE harrier_tenant` and the owner's claims,
read back by `assume_identity` (spec 134). The owner is the one the run
manager recorded from a verified `sub`; this is the reach the API already
has (spec 106, The trust model).

- `begin` reads the owner's ciphertexts, binds the run (`runenv.bind_run`)
  and inserts its `run_records` row, in one transaction, before anything is
  spawned. A refused binding writes nothing.
- `record` follows each state change, `WHERE run_id = ? AND api_instance =
  ?`, so a process never writes a row another process started.
- `listed` and `one` read the owner's rows for the run routes, and first
  store a row another API process left `queued` or `running` as
  `interrupted`: that process is gone, so the run was cut off (spec 106,
  "A run cut off by a stop").
- `KeepAwake` sends `GET /health` through Fly's proxy while any run is
  active, so the proxy does not stop the machine under it.

A row holds the run's id, kind, target, state, times and exit code. Never a
parameter, an event or an output.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from harrier.connection import Connection, owner_claims
from harrier.credentials import CredentialKind, CredentialsKey, stored_credentials
from harrier.db import BUSY_TIMEOUT_MS
from harrier_api.runenv import BoundRun, bind_run

if TYPE_CHECKING:
    from harrier.pgstore import RequestPool
    from harrier_api.runs import Run, RunState

logger = logging.getLogger(__name__)

ACTIVE_STATES = ("queued", "running")

# How often a beat is sent while a run is active: the interval spec 115's
# F1 confirmed on a real machine, where the proxy stopped it 80 s to 126 s
# after the last request.
BEAT_INTERVAL_SECONDS = 30.0
# How long one beat may take before it counts as failed.
BEAT_TIMEOUT_SECONDS = 10.0
HEALTH_PATH = "/health"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class HostedRuns:
    """The run manager's hosted half: the key, the API's pool, this
    process's instance id, and the keep-awake beats."""

    def __init__(
        self,
        *,
        pool: RequestPool,
        key: CredentialsKey,
        root: Path,
        server_env: Mapping[str, str],
        keep_awake: KeepAwake,
        api_instance: str | None = None,
    ) -> None:
        self._pool = pool
        self._key = key
        self._root = root
        self._server_env = server_env
        self.keep_awake = keep_awake
        # Drawn once per process and written nowhere but `run_records`
        # (spec 106).
        self.api_instance = api_instance if api_instance is not None else str(uuid.uuid4())

    @contextmanager
    def as_owner(self, owner: str) -> Generator[Connection]:
        """One transaction on the API's login as `owner`, committed when the
        block ends without an exception. Raises `IdentityDidNotHold` when
        the read-back differs, before any statement of the block."""
        driver = self._pool.lease()
        try:
            conn = Connection.postgres(cast(Any, driver), claims=owner_claims(owner))
            conn.busy_timeout_ms = BUSY_TIMEOUT_MS
            with conn.transaction():
                yield conn
        finally:
            self._pool.release(driver)

    def begin(
        self, run: Run, needs: Iterable[CredentialKind], extra: Mapping[str, str] | None = None
    ) -> BoundRun:
        """Bind `run` to its owner and insert its row, in one transaction
        as the owner. Raises `RunCredentialMissing` or
        `CredentialUnreadable` before anything is written."""
        with self.as_owner(run.owner) as conn:
            bound = bind_run(
                server_env=self._server_env,
                owner=run.owner,
                root=self._root,
                stored=stored_credentials(conn),
                key=self._key,
                needs=needs,
                extra=extra,
            )
            conn.execute(
                "INSERT INTO run_records (run_id, kind, target, state, api_instance, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run.id, run.kind, run.target, run.state, self.api_instance, run.created_at),
            )
        return bound

    def record(self, run: Run) -> None:
        """Store `run`'s state, times and exit code, as its owner, on the
        row this process inserted. A row the stop signal stored
        `interrupted` stays so, whatever write was still in flight."""
        with self.as_owner(run.owner) as conn:
            conn.execute(
                "UPDATE run_records SET state = ?, started_at = ?, ended_at = ?, exit_code = ? "
                "WHERE run_id = ? AND api_instance = ? AND state <> 'interrupted'",
                (
                    run.state,
                    run.started_at,
                    run.ended_at,
                    run.exit_code,
                    run.id,
                    self.api_instance,
                ),
            )

    def listed(self, owner: str) -> list[Run]:
        """Every run row of `owner`, newest first, with a row another
        process left active stored and shown `interrupted`."""
        with self.as_owner(owner) as conn:
            self._interrupt_left_behind(conn)
            rows = conn.execute(
                "SELECT run_id, kind, target, state, created_at, started_at, ended_at, "
                "exit_code FROM run_records ORDER BY created_at DESC, run_id"
            ).fetchall()
        return [_run_from(owner, row) for row in rows]

    def one(self, owner: str, run_id: str) -> Run | None:
        """`owner`'s row for `run_id`, or None, as `listed` reads it."""
        with self.as_owner(owner) as conn:
            self._interrupt_left_behind(conn)
            row = conn.execute(
                "SELECT run_id, kind, target, state, created_at, started_at, ended_at, "
                "exit_code FROM run_records WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else _run_from(owner, row)

    def _interrupt_left_behind(self, conn: Connection) -> None:
        # Only the session owner's rows: the policy sees no other.
        conn.execute(
            "UPDATE run_records SET state = 'interrupted', ended_at = ? "
            "WHERE api_instance <> ? AND state IN ('queued', 'running')",
            (_now(), self.api_instance),
        )


def _run_from(owner: str, row: Any) -> Run:
    from harrier_api.runs import Run

    return Run(
        id=str(row[0]),
        kind=str(row[1]),
        command=[],
        target=str(row[2]),
        state=cast("RunState", str(row[3])),
        created_at=str(row[4]),
        started_at=None if row[5] is None else str(row[5]),
        ended_at=None if row[6] is None else str(row[6]),
        exit_code=None if row[7] is None else int(row[7]),
        owner=owner,
    )


# --- keeping the machine awake ---


def get_status(url: str) -> int:
    """`GET url`, and its status. Raises for a refusal or a failure, as
    urllib does."""
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=BEAT_TIMEOUT_SECONDS) as response:
        return int(response.status)


def beat_failure(error: BaseException) -> str:
    """How a failed beat is logged: its status class, or the exception's
    class. Never the URL (spec 106)."""
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code // 100}xx"
    return type(error).__name__


class KeepAwake:
    """Beats to the API's own public host while a run is active (spec 106,
    "Keeping the machine awake").

    `wake` is called when a run becomes active. One beat is sent then, and
    one at each interval while `active()` holds; the loop ends at the first
    interval after the last run ends. A failed beat is logged and fails
    nothing. `send` and `sleep` are injected by the tests, which reach no
    network and wait for no clock.
    """

    def __init__(
        self,
        host: str,
        *,
        send: Callable[[str], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interval: float = BEAT_INTERVAL_SECONDS,
    ) -> None:
        self.url = f"https://{host}{HEALTH_PATH}"
        # Looked up when built, not when the module loads, so the app's
        # beats can be pointed elsewhere by replacing `get_status`.
        self._send = send if send is not None else get_status
        self._sleep = sleep
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    def wake(self, active: Callable[[], bool]) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._beat_while(active))

    async def _beat_while(self, active: Callable[[], bool]) -> None:
        while active():
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._send, self.url), BEAT_TIMEOUT_SECONDS
                )
            except Exception as error:
                logger.warning("a keep-awake beat failed: %s", beat_failure(error))
            await self._sleep(self._interval)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
