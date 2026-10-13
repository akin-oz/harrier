"""Each hosted request runs as its user (spec 134).

Spec 104's middleware verifies a request's token and leaves its claims on
the request. This module turns them into the request's database identity,
and is the only way a hosted route reaches the store:

- `open_request_store`, at start: the pool of connections as the login
  role, and the start checks, read once: the login role cannot step past
  the row policy (`harrier.loginrole`), the store defaults to `READ
  COMMITTED`, and the store is at the version this code needs.
- `hosted_conn`, the hosted app's `get_conn`: one transaction per request,
  through the seam, as `harrier_tenant` with the verified claims, read back
  by `assume_identity`, with three timeouts set `LOCAL`.
- `RequestTransactionMiddleware`: commits or rolls the transaction back by
  the response's status before the client sees it, and returns the
  connection to the pool.

The middleware, not the dependency's exit, ends the transaction: FastAPI
runs that exit after the response is sent, and it sees no status, so a 409
returned without raising would commit and a failed commit would follow a
200 (spec 134, Commit and rollback).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from harrier.connection import (
    Connection,
    IdentityDidNotHold,
    StoreConnectionError,
    StoreDatabaseError,
)
from harrier.db import BUSY_TIMEOUT_MS
from harrier.loginrole import login_role_problems
from harrier.pgstore import RequestPool, check_postgres_version, transaction_failed
from harrier.tracker.schema import TENANT_ROLE

if TYPE_CHECKING:
    from harrier.loginrole import LoginRoleProblem

logger = logging.getLogger(__name__)

# --- the chosen values (spec 134 asks each to say why) ---

# How long a request waits for a lock another session holds: the wait the
# domain was written for, SQLite's busy timeout, which the seam already
# gives every Postgres transaction. A request stalled by another session's
# table lock fails after this instead of waiting for that session, so one
# owner's lock cannot hold every other owner for longer.
REQUEST_LOCK_TIMEOUT_MS = BUSY_TIMEOUT_MS
# How long one statement may run, a lock wait included. No harrier
# statement is meant to take long: each reads or writes one owner's rows.
# Six times the lock timeout, so a statement waiting on a lock is stopped by
# the lock timeout, and this bounds only a statement that runs. Chosen, not
# measured: the hosted store's real query times are not known yet.
REQUEST_STATEMENT_TIMEOUT_MS = 30_000
# How long a request's transaction may sit idle between statements. The
# longest wait a route makes while its transaction is open is `POST
# /capture/add` enriching a posting from its URL: three attempts of 30
# seconds, with 2 and 4 seconds between them (`harrier.screening.http`'s
# defaults), 96 seconds. This is above that, so a slow posting is not cut
# off, and it still ends a transaction a bug left open.
REQUEST_IDLE_TIMEOUT_MS = 120_000

# The pool. One connection is opened at start, so the start checks can run,
# and more as requests need them, up to ten. Chosen, not measured: one
# machine serves the first hosted release, and spec 110 sizes it against
# the plan. A request waits for a free connection as long as
# `postgres_connect` waits to connect, the longest harrier waits to reach
# the store, and then fails as any exception does, 500 (spec 134, Open
# decision 1).
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 10
POOL_WAIT_SECONDS = 10.0

# --- texts ---

# The one detail a request gets when it cannot run as its user or its
# transaction cannot be kept. It names nothing of the request.
COULD_NOT_RUN_AS_USER = "the request could not run as its user"
REQUEST_ENDED = "the request's transaction has ended, so no statement runs on it"
START_UNREACHABLE = "the hosted API cannot run its start checks: it could not connect to the store"
ISOLATION_NEEDED = "read committed"

# Where the request's transaction waits for the middleware, in the request's
# state beside spec 104's `claims`.
REQUEST_STATE = "harrier_request_transaction"


class HostedStartRefused(RuntimeError):
    """A start check refused the store or the login role. The text names
    the check, and the role a membership reaches, never the URL or a
    password (spec 134)."""


def login_role_refusal(problems: list[LoginRoleProblem]) -> str:
    return "the hosted API refuses its login role: " + "; ".join(map(str, problems))


def isolation_refusal(found: str) -> str:
    return (
        f"the store's default_transaction_isolation is {found}; "
        f"the hosted API needs {ISOLATION_NEEDED}"
    )


def check_failed(check: str, reason: str) -> str:
    return f"the hosted API start check of the {check} could not run: {reason}"


# --- the store, at start ---


class RequestStore:
    """The pool, once the start checks passed."""

    def __init__(self, pool: RequestPool) -> None:
        self.pool = pool

    def close(self) -> None:
        self.pool.close()


def open_request_store(url: str) -> RequestStore:
    """The pool as the login role in `url`, and the start checks.

    Raises HostedStartRefused when the store cannot be reached or a check
    fails, and StoreVersionError, with `check_postgres_version`'s text, when
    the store is behind or ahead of this code. Closes the pool before either.
    """
    # Every lease and return is logged at INFO by the pool: two lines per
    # request, saying nothing an operator acts on.
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
    pool: RequestPool | None = None
    with suppress(StoreConnectionError):
        pool = RequestPool(
            url, min_size=POOL_MIN_SIZE, max_size=POOL_MAX_SIZE, wait_seconds=POOL_WAIT_SECONDS
        )
    if pool is None:
        # Outside the except block, so nothing of the pool's error is chained.
        raise HostedStartRefused(START_UNREACHABLE)
    try:
        run_start_checks(pool)
    except BaseException:
        pool.close()
        raise
    return RequestStore(pool)


class _CheckedNothingWritten(Exception):
    """Ends the start checks' transaction by rolling it back."""


def run_start_checks(pool: RequestPool) -> None:
    """Spec 134's start checks, in one transaction that is rolled back.

    As the login role: `login_role_problems`, then the store's default
    isolation. Then `SET LOCAL ROLE harrier_tenant`, which is also the check
    that the login role can set it, and the version, read as the tenant
    role: the login role holds no grant of its own, and the tenant may read
    `schema_version` (spec 105). Requests do not read the version again.
    """
    driver = pool.lease()
    check = "login role"
    failed = ""
    try:
        conn = Connection.postgres(cast(Any, driver))
        with conn.transaction():
            row = conn.execute("SELECT current_user::text").fetchone()
            problems = login_role_problems(conn, str(row[0]) if row else "")
            if problems:
                raise HostedStartRefused(login_role_refusal(problems))
            check = "default isolation"
            row = conn.execute("SELECT current_setting('default_transaction_isolation')").fetchone()
            isolation = str(row[0]) if row else ""
            if isolation != ISOLATION_NEEDED:
                raise HostedStartRefused(isolation_refusal(isolation))
            check = "store version"
            conn.execute(f"SET LOCAL ROLE {TENANT_ROLE}")
            check_postgres_version(_store_version(conn))
            raise _CheckedNothingWritten
    except _CheckedNothingWritten:
        pass
    except StoreDatabaseError as error:
        failed = str(error)
    finally:
        pool.release(driver)
    if failed:
        # Raised after the except block, so the driver's error is not
        # chained to it, and a traceback prints none.
        raise HostedStartRefused(check_failed(check, failed))


def _store_version(conn: Connection) -> int:
    """`harrier.pgstore.postgres_version`, through the seam."""
    exists = conn.execute("SELECT to_regclass('public.schema_version') IS NOT NULL").fetchone()
    if exists is None or not exists[0]:
        return 0
    row = conn.execute("SELECT MAX(version) FROM public.schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


# --- the request transaction ---


class _Lease:
    """The pooled connection as the request's `Connection` sees it, until
    the middleware ends the transaction. After that every use is refused,
    so a body read after the headers fails rather than running outside the
    transaction on a connection another request may hold."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        self._ended = False

    def end(self) -> None:
        self._ended = True

    def __getattr__(self, name: str) -> Any:
        if self._ended:
            raise StoreDatabaseError(REQUEST_ENDED)
        return getattr(self._driver, name)


class _RolledBack(Exception):
    """Thrown into the request's open `transaction()` to roll it back."""


@dataclass
class RequestTransaction:
    """One request's connection and its open transaction."""

    pool: RequestPool | None = None
    driver: Any = None
    lease: _Lease | None = None
    block: AbstractContextManager[None] | None = None

    @property
    def is_open(self) -> bool:
        return self.block is not None

    def begin(self, pool: RequestPool, claims: Mapping[str, object]) -> Connection:
        """Lease a connection and open the request's transaction on it:
        `BEGIN`, `lock_timeout`, the role, the claims and the read-back
        (the seam), then the other two timeouts. Raises IdentityDidNotHold
        when the read-back differs; the seam has rolled back."""
        if self.driver is not None:
            raise RuntimeError("the request's transaction is already open")
        self.pool = pool
        self.driver = pool.lease()
        self.lease = _Lease(self.driver)
        conn = Connection.postgres(cast(Any, self.lease), claims=claims)
        conn.busy_timeout_ms = REQUEST_LOCK_TIMEOUT_MS
        block = conn.transaction()
        block.__enter__()
        self.block = block
        conn.execute(f"SET LOCAL statement_timeout = '{REQUEST_STATEMENT_TIMEOUT_MS}ms'")
        conn.execute(
            f"SET LOCAL idle_in_transaction_session_timeout = '{REQUEST_IDLE_TIMEOUT_MS}ms'"
        )
        return conn

    def end(self, *, commit: bool) -> bool:
        """Commit or roll back, and refuse the `Connection` every later
        statement. False when a commit was asked for and the store did not
        keep the work: the commit raised, or a statement had failed in the
        transaction, where `COMMIT` rolls back without an error."""
        block, lease = self.block, self.lease
        if block is None or lease is None:
            return True
        self.block = None
        try:
            if commit and not transaction_failed(self.driver):
                try:
                    block.__exit__(None, None, None)
                except StoreDatabaseError:
                    return False
                return True
            block.__exit__(_RolledBack, _RolledBack(), None)
            return not commit
        finally:
            lease.end()

    def finish(self) -> None:
        """Roll back whatever is still open, and return the connection."""
        try:
            self.end(commit=False)
        finally:
            driver, pool = self.driver, self.pool
            self.driver = None
            if driver is not None and pool is not None:
                pool.release(driver)


def _shown(path: str) -> str:
    # Percent-decoded, so a control character is escaped rather than
    # written into the record as a new line (as spec 104's refusal log).
    return path.encode("unicode_escape").decode("ascii")


def hosted_conn(request: Request) -> Iterator[Connection]:
    """`get_conn` on the hosted app (spec 134, The request transaction).

    Refuses with 500, and takes nothing from the pool, when the request
    carries no verified claims or did not pass the transaction middleware.
    Answers 500 with the generic detail when the read-back differs, and
    logs which value differed with the method and path, nothing else.
    """
    state = cast(dict[str, Any], request.scope.get("state") or {})
    claims = state.get("claims")
    transaction = state.get(REQUEST_STATE)
    store = getattr(request.app.state, "request_store", None)
    method, path = request.method, _shown(request.url.path)
    if (
        not isinstance(claims, dict)
        or not isinstance(transaction, RequestTransaction)
        or not isinstance(store, RequestStore)
    ):
        logger.error("%s %s reached the store without verified claims", method, path)
        raise HTTPException(status_code=500, detail=COULD_NOT_RUN_AS_USER)
    conn: Connection | None = None
    try:
        conn = transaction.begin(store.pool, cast(dict[str, object], claims))
    except IdentityDidNotHold as error:
        logger.warning(
            "%s %s could not run as its user: %s differed", method, path, ", ".join(error.differed)
        )
    if conn is None:
        # Outside the except block, so the store error is not chained.
        raise HTTPException(status_code=500, detail=COULD_NOT_RUN_AS_USER)
    yield conn


class RequestTransactionMiddleware:
    """Ends each request's transaction by its response's status (spec 134).

    Installed hosted only, inside spec 104's authentication middleware. When
    the response's start passes, a request that opened a transaction
    commits below 400 and rolls back otherwise, and only then is the start
    forwarded; a commit the store did not keep is answered 500 with the
    generic detail instead. A request that ends with an exception and no
    response rolls back. Either way the connection goes back to the pool,
    which discards it unless it is open and idle.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        transaction = RequestTransaction()
        state = cast(dict[str, Any], scope.get("state") or {})
        scope = {**scope, "state": {**state, REQUEST_STATE: transaction}}
        replaced = False

        async def ending(message: Message) -> None:
            nonlocal replaced
            if replaced:
                # The route's own body, after its start was replaced.
                return
            if message["type"] == "http.response.start" and transaction.is_open:
                commit = int(message["status"]) < 400
                if not await run_in_threadpool(transaction.end, commit=commit):
                    replaced = True
                    logger.warning(
                        "%s %s: the store did not keep the request's work",
                        scope["method"],
                        _shown(cast(str, scope["path"])),
                    )
                    response = JSONResponse(
                        status_code=500, content={"detail": COULD_NOT_RUN_AS_USER}
                    )
                    await response(scope, receive, send)
                    return
            await send(message)

        try:
            await self.app(scope, receive, ending)
        finally:
            await run_in_threadpool(transaction.finish)
