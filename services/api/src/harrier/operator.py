"""`harrier operator invite` and `harrier operator erase` (spec 111), and
`harrier operator erase-test-owners` (spec 143).

The operator of a hosted deployment creates a user and removes one. Neither
is reachable from HTTP (ADR-013 decision 3): both run on the operator's
machine, with a Supabase secret key in the shell and, for `erase`, a database
login that can act as `service_role`.

`invite` opens no database connection. It reads one address from standard
input, asks the auth server's admin API for a confirmed user with no
password, and prints the user's id. Harrier sends nothing itself.

`erase` runs six steps in order, each reported on its own line and each a
no-op on what is already gone, so a re-run resumes (see `erase_owner`).

`erase-test-owners` finds the synthetic owners that spec 115's staging check
leaves in `auth.users` by a rule on the `auth` tables alone, and runs `erase`'s
steps for each, one owner at a time (see `run_erase_test_owners`).

Nothing here prints an address, a key, a URL's password or a row's content.
An error names a step and, for the auth server, an HTTP status.

The two Storage steps delete every object under the owner's prefix through
spec 107's seam (`harrier.filestore.SupabaseFileStore.for_operator`), which
calls the Storage API with the operator's secret key: the delete spec 111's
steps 2 and 5 describe, replacing the list-only check of its Amendment
(2026-10-15) (spec 107, Note (2026-10-15)). Nothing here deletes a row from
the Storage objects table with SQL, which leaves the files behind.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO, cast
from urllib.parse import urlsplit

import httpx

from harrier.connection import Connection, StoreDatabaseError, StoreError
from harrier.db import data_dir
from harrier.envfile import SECRET_KEY_VARIABLE
from harrier.filestore import BUCKET as FILES_BUCKET

if TYPE_CHECKING:
    from collections.abc import Sequence

SUPABASE_URL_VARIABLE = "HARRIER_SUPABASE_URL"
DATABASE_URL_VARIABLE = "HARRIER_DATABASE_URL"

# The private bucket of ADR-013 decision 6, as the seam names it.
BUCKET = FILES_BUCKET

# The duration Supabase's updateUserById reference uses as its example: 100
# years, so a banned user signs in no more.
BAN_DURATION = "876000h"

# The six steps, by the name an error and the ledger use.
STEPS: tuple[str, ...] = ("ban", "storage", "rows", "auth", "stragglers", "ledger")

REQUEST_TIMEOUT_SECONDS = 30.0

# --- texts: each names a variable or a step, never a value ---

ADDRESS_EXISTS = "that address already has an account"
NOT_AN_ADDRESS = "that is not an email address; it is read from standard input, one line"
NOT_A_USER_ID = "the user id must be an auth user's UUID; an email address is never accepted"
CONFIRM_MISMATCH = "--confirm must repeat the user id exactly"
NEEDS_SERVICE_ROLE = "harrier operator needs a login that can act as service_role"
NOT_MIGRATED = "the store has no harrier_erase_owner(uuid); run harrier store migrate first"
ROLE_DID_NOT_HOLD = "the erase transaction did not run as service_role"
LATE_ROWS = "rows were written after the erase; run it again after the user's last token expires"
SUPABASE_URL_REFUSAL = (
    f"{SUPABASE_URL_VARIABLE} must be an https:// URL that names a host and nothing else"
)


def needs(variable: str) -> str:
    return f"{variable} is not set"


def secret_key_in_dotenv() -> str:
    return (
        f"{SECRET_KEY_VARIABLE} is defined in .env; the local container reads that file, "
        "so a secret key there would sit in its environment. Remove it and export it in "
        "the shell for this command"
    )


class OperatorError(Exception):
    """A refusal or a failed step. The message is printed after `error: `
    and carries no address, key, password or row content."""

    def __init__(self, message: str, *, step: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.step = step

    def line(self) -> str:
        if self.step is None:
            return f"error: {self.message}"
        return f"error: step {STEPS.index(self.step) + 1} ({self.step}): {self.message}"


# --- the admin client ---


@contextmanager
def quiet_http_logs() -> Generator[None]:
    """No request line from the HTTP client reaches a log while a command
    runs. httpx logs every request at INFO with its URL, which holds a user
    id, and a process that configured logging at INFO would print it. The
    level is put back when the command ends."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    saved = [(logger, logger.level) for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for logger, level in saved:
            logger.setLevel(level)


def http_client() -> httpx.Client:
    """The client the admin calls use. A test replaces this with one over a
    mock transport, so no test reaches the network."""
    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)


def supabase_origin(raw: str) -> str | None:
    """`raw` as `https://host[:port]`, or None when it is anything else:
    another scheme, credentials, a path, a query or a fragment. As the hosted
    API reads the same variable (`harrier_api.hostedauth`), which this
    package may not import."""
    text = raw.strip().removesuffix("/")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    netloc = parts.hostname if port is None else f"{parts.hostname}:{port}"
    origin = f"https://{netloc}"
    return origin if text == origin else None


@dataclass
class AdminClient:
    """The Supabase admin calls this module makes: create, read, ban and
    delete an auth user. Storage is reached through the seam
    (`clear_storage`), on the same client and key.

    The secret key goes in the `apikey` header, which is where Supabase's API
    keys guide says a secret key belongs, and in no URL, body, log or error.
    Every failure is an OperatorError that names the call and the HTTP
    status, never a response body: a body can carry what was sent.
    """

    origin: str
    secret_key: str = field(repr=False)
    http: httpx.Client = field(repr=False)

    def _call(
        self,
        step: str,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        ok: Sequence[int] = (200,),
    ) -> httpx.Response:
        try:
            response = self.http.request(
                method,
                f"{self.origin}{path}",
                headers={"apikey": self.secret_key},
                json=None if body is None else dict(body),
            )
        except httpx.HTTPError as error:
            # The class only: a transport error can quote the URL.
            raise OperatorError(f"the call failed ({type(error).__name__})", step=step) from None
        if response.status_code not in ok:
            raise OperatorError(f"the server answered {response.status_code}", step=step)
        return response

    def create_user(self, address: str) -> str:
        """A confirmed user with no password. Returns the user's id. Raises
        OperatorError(ADDRESS_EXISTS) when the address has a user."""
        try:
            response = self.http.request(
                "POST",
                f"{self.origin}/auth/v1/admin/users",
                headers={"apikey": self.secret_key},
                json={"email": address, "email_confirm": True},
            )
        except httpx.HTTPError as error:
            raise OperatorError(f"the call failed ({type(error).__name__})") from None
        if response.status_code == 422 and _error_code(response) == "email_exists":
            raise OperatorError(ADDRESS_EXISTS)
        if response.status_code != 200:
            raise OperatorError(f"the auth server answered {response.status_code}")
        identifier = _field(response, "id")
        if not isinstance(identifier, str) or not _is_uuid(identifier):
            raise OperatorError("the auth server's answer holds no user id")
        return str(uuid.UUID(identifier))

    def user_exists(self, user_id: str, *, step: str = "ban") -> bool:
        try:
            response = self.http.request(
                "GET",
                f"{self.origin}/auth/v1/admin/users/{user_id}",
                headers={"apikey": self.secret_key},
            )
        except httpx.HTTPError as error:
            raise OperatorError(f"the call failed ({type(error).__name__})", step=step) from None
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise OperatorError(f"the server answered {response.status_code}", step=step)

    def ban_user(self, user_id: str) -> None:
        self._call("ban", "PUT", f"/auth/v1/admin/users/{user_id}", {"ban_duration": BAN_DURATION})

    def delete_user(self, user_id: str) -> None:
        """A hard delete (`should_soft_delete` false). A user already gone
        answers 404, which a re-run counts as done."""
        self._call(
            "auth",
            "DELETE",
            f"/auth/v1/admin/users/{user_id}",
            {"should_soft_delete": False},
            ok=(200, 404),
        )

    def clear_storage(self, user_id: str, *, step: str) -> int:
        """Delete every object under the owner's prefix in the private bucket
        and return how many there were: steps 2 and 5 of spec 111's
        Deletion. Through spec 107's seam, bound to this one owner with the
        operator's secret key, outside any request; it lists the prefix at
        every depth and removes what it found through the Storage API, never
        with SQL. A failure names the step and the HTTP status, as every
        other call here does, and never the owner or an object name."""
        from harrier.filestore import FileStoreError, SupabaseFileStore

        store = SupabaseFileStore.for_operator(
            user_id, origin=self.origin, secret_key=self.secret_key, http=self.http
        )
        try:
            return store.delete_everything()
        except FileStoreError as error:
            if error.status is not None:
                raise OperatorError(f"the server answered {error.status}", step=step) from None
            raise OperatorError(f"the call failed ({type(error).__name__})", step=step) from None


def _error_code(response: httpx.Response) -> str | None:
    value = _field(response, "error_code")
    return value if isinstance(value, str) else None


def _field(response: httpx.Response, name: str) -> object:
    try:
        parsed: object = response.json()
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    return cast("dict[str, object]", parsed).get(name)


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


# --- the environment both commands need ---


@dataclass(frozen=True)
class Admin:
    """The project and its secret key, read once, never printed."""

    origin: str
    secret_key: str = field(repr=False)


def read_admin(environ: Mapping[str, str]) -> Admin:
    """The project URL and secret key from the process environment.

    Refused, naming the variable and never its value, when either is unset or
    malformed, or when the secret key is defined in the working directory's
    `.env`: the local container reads that file (spec 111). The loader never
    copies this key into the environment (Amendment (2026-10-15)), so the
    file is read here for the variable's presence, whatever the environment
    holds.
    """
    from harrier.envfile import read_env_file

    if SECRET_KEY_VARIABLE in read_env_file(Path(".env")):
        raise OperatorError(secret_key_in_dotenv())
    raw_url = environ.get(SUPABASE_URL_VARIABLE, "").strip()
    if not raw_url:
        raise OperatorError(needs(SUPABASE_URL_VARIABLE))
    key = environ.get(SECRET_KEY_VARIABLE, "").strip()
    if not key:
        raise OperatorError(needs(SECRET_KEY_VARIABLE))
    origin = supabase_origin(raw_url)
    if origin is None:
        raise OperatorError(SUPABASE_URL_REFUSAL)
    return Admin(origin, key)


# --- invite ---

# One address: no whitespace, one `@`, a dotted domain, and a length SMTP
# allows. Looser than the RFCs on purpose; the auth server judges the rest.
_ADDRESS = re.compile(r"[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+")


def run_invite(
    environ: Mapping[str, str],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """`harrier operator invite`: exit 0 and the new user's id on standard
    output, or exit 1 and one line on standard error. Opens no database
    connection, and prints neither the address nor a link."""
    stdin = stdin if stdin is not None else cast("TextIO", sys.stdin)
    stdout = stdout if stdout is not None else cast("TextIO", sys.stdout)
    stderr = stderr if stderr is not None else cast("TextIO", sys.stderr)
    try:
        admin = read_admin(environ)
        address = stdin.readline().strip()
        if len(address) > 254 or _ADDRESS.fullmatch(address) is None:
            raise OperatorError(NOT_AN_ADDRESS)
        with quiet_http_logs(), http_client() as http:
            created = AdminClient(admin.origin, admin.secret_key, http).create_user(address)
    except OperatorError as error:
        print(error.line(), file=stderr)
        return 1
    print(created, file=stdout)
    return 0


# --- erase ---


def ledger_path() -> Path:
    """`data/hosted/erasures.jsonl`, under the data directory, which is
    never in git (spec 111, Backups)."""
    return data_dir() / "hosted" / "erasures.jsonl"


def append_ledger(path: Path, user_id: str, completed: Sequence[str], *, complete: bool) -> None:
    """One line: the user id, the time in UTC, which steps completed and
    whether the erase did. No address, no row content. A restore of a backup
    older than the line re-runs `harrier operator erase` for its id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "user_id": user_id,
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "completed": list(completed),
            "complete": complete,
        },
        separators=(",", ":"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def preflight(conn: Connection) -> None:
    """Before any call: the login can act as `service_role`, and the store
    has the erase function. Reads only the catalog, which any login may."""
    from harrier.loginrole import can_set_role
    from harrier.tracker.schema import ERASE_FUNCTION, SERVICE_ROLE

    try:
        who = conn.execute("SELECT current_user::text").fetchone()
        if who is None or not can_set_role(conn, str(who[0]), SERVICE_ROLE):
            raise OperatorError(NEEDS_SERVICE_ROLE)
        found = conn.execute(
            "SELECT to_regprocedure(?) IS NOT NULL", (f"public.{ERASE_FUNCTION}",)
        ).fetchone()
    except StoreDatabaseError:
        raise OperatorError(NEEDS_SERVICE_ROLE) from None
    if found is None or not found[0]:
        raise OperatorError(NOT_MIGRATED)


def erase_rows(conn: Connection, user_id: str) -> list[tuple[str, int]]:
    """Step 3: one transaction as `service_role`, with the role read back
    before the function runs (refusing otherwise, as spec 104's request
    transaction does), under the connection's lock timeout, which is
    `BUSY_TIMEOUT_MS`. Returns each table and how many rows it lost."""
    from harrier.tracker.schema import SERVICE_ROLE

    with conn.transaction(service_role=True):
        row = conn.execute("SELECT current_user::text").fetchone()
        if row is None or row[0] != SERVICE_ROLE:
            raise OperatorError(ROLE_DID_NOT_HOLD, step="rows")
        counts = conn.execute(
            "SELECT table_name, erased FROM harrier_erase_owner(?::uuid)", (user_id,)
        ).fetchall()
    return [(str(name), int(count)) for name, count in counts]


UNKNOWN_TO_AUTH = "the auth server does not know this user"
AUTH_ROW_UNREADABLE = (
    "the login cannot read auth.users, so a user the auth server does not know "
    "cannot be told from one already erased"
)


def auth_row_exists(conn: Connection, user_id: str) -> bool:
    """Whether `auth.users` holds the id, read as the operator's login. Asked
    only when the auth server answers 404: a row it cannot see (one inserted
    by SQL, as spec 115's tests do) still owns rows, so the database decides
    whether there is anything to erase (spec 143, Failure modes)."""
    try:
        row = conn.execute("SELECT 1 FROM auth.users WHERE id = ?::uuid", (user_id,)).fetchone()
    except StoreDatabaseError:
        raise OperatorError(AUTH_ROW_UNREADABLE, step="ban") from None
    return row is not None


def delete_auth_row(conn: Connection, user_id: str) -> None:
    """Step 4 for a user the auth server does not know: its admin API cannot
    delete what it cannot see, so the row is deleted with SQL, as the
    operator's login."""
    try:
        with conn.transaction():
            conn.execute("DELETE FROM auth.users WHERE id = ?::uuid", (user_id,))
    except StoreError as error:
        raise OperatorError(
            f"auth.users refused the delete ({type(error).__name__})", step="auth"
        ) from None


def erase_owner(
    user_id: str,
    admin: AdminClient,
    conn: Connection,
    ledger: Path,
    out: TextIO,
    err: TextIO,
    row_counts: list[tuple[str, int]] | None = None,
) -> int:
    """The six steps of spec 111, "Deletion", in order. Returns the exit
    status: 0 when every step completed, 1 naming the step otherwise.

    1. Ban, so the user signs in no more.
    2. Storage: every object under the owner's prefix is deleted through
       spec 107's seam.
    3. Rows: `harrier_erase_owner`, one transaction as `service_role`.
    4. The auth user, hard deleted.
    5. Storage again, for objects written since step 2.
    6. The ledger line.

    A user that exists neither for the auth server nor in `auth.users`
    skips steps 1, 3 and 4, so a re-run after a partial erase finishes the
    rest. A user the auth server answers 404 for while `auth.users` still
    holds it skips the ban, has its rows erased, and has its `auth.users`
    row deleted with SQL (spec 143, Failure modes). A line is appended to the ledger when
    the run fails after step 3 committed, too: those rows are gone from the
    live copy, and a restore of an older backup must erase them again.

    `row_counts`, when given, receives each table and the rows step 3 deleted
    from it, for a caller that totals them (spec 143).
    """
    completed: list[str] = []
    rows_gone = False

    def say(text: str) -> None:
        print(text, file=out)

    try:
        known = admin.user_exists(user_id)
        exists = known or auth_row_exists(conn, user_id)
        if known:
            admin.ban_user(user_id)
            say("step 1 ban: banned")
        elif exists:
            say(f"step 1 ban: skipped, {UNKNOWN_TO_AUTH}")
        else:
            say("step 1 ban: skipped, no auth user with that id")
        completed.append("ban")

        deleted = admin.clear_storage(user_id, step="storage")
        say(f"step 2 storage: {deleted} objects deleted")
        completed.append("storage")

        if exists:
            try:
                counts = erase_rows(conn, user_id)
            except StoreError as error:
                raise OperatorError(str(error), step="rows") from None
            rows_gone = True
            if row_counts is not None:
                row_counts.extend(counts)
            for table, erased in counts:
                say(f"step 3 rows: {table} {erased}")
        else:
            rows_gone = True
            say("step 3 rows: skipped, no auth user with that id")
        completed.append("rows")

        if known:
            _delete_auth_user(admin, conn, user_id)
            say("step 4 auth user: deleted")
        elif exists:
            delete_auth_row(conn, user_id)
            say(f"step 4 auth user: deleted from auth.users, as {UNKNOWN_TO_AUTH}")
        else:
            say("step 4 auth user: skipped, no auth user with that id")
        completed.append("auth")

        stragglers = admin.clear_storage(user_id, step="stragglers")
        say(f"step 5 stragglers: {stragglers} objects deleted")
        completed.append("stragglers")

        try:
            append_ledger(ledger, user_id, [*completed, "ledger"], complete=True)
        except OSError:
            raise OperatorError("the ledger line could not be written", step="ledger") from None
        say("step 6 ledger: appended")
    except OperatorError as error:
        print(error.line(), file=err)
        if rows_gone and error.step != "ledger":
            try:
                append_ledger(ledger, user_id, completed, complete=False)
            except OSError:
                print("error: the ledger line could not be written", file=err)
        return 1
    return 0


def _delete_auth_user(admin: AdminClient, conn: Connection, user_id: str) -> None:
    """Step 4. A refused delete is read: if rows were written after step 3
    (a token issued before the ban still works), the foreign keys refuse
    the delete, and erasing again finds them. The command then says so;
    any other refusal is reported as the server's."""
    try:
        admin.delete_user(user_id)
    except OperatorError as refused:
        try:
            late = erase_rows(conn, user_id)
        except (StoreError, OperatorError):
            raise refused from None
        if any(erased for _, erased in late):
            raise OperatorError(LATE_ROWS, step="auth") from None
        raise


@dataclass
class OperatorSession:
    """What a command that erases holds: the project and its key, and the
    operator's login, already checked by `preflight`."""

    admin: Admin
    conn: Connection


@contextmanager
def operator_session(environ: Mapping[str, str]) -> Generator[OperatorSession]:
    """The environment `erase` and `erase-test-owners` share (spec 111, spec
    143): the project and secret key, the operator's login in
    `HARRIER_DATABASE_URL`, and the checks of `preflight`. Every refusal is
    an OperatorError that names a variable or a rule and never a value. The
    connection is closed when the block ends."""
    from harrier.connection import StoreUrlError, store_target
    from harrier.pgstore import postgres_connect

    admin = read_admin(environ)
    if not environ.get(DATABASE_URL_VARIABLE, "").strip():
        raise OperatorError(needs(DATABASE_URL_VARIABLE))
    try:
        target = store_target(environ)
    except StoreUrlError as error:
        raise OperatorError(str(error)) from None
    if not target.is_postgres:
        raise OperatorError(f"{DATABASE_URL_VARIABLE} must name a Postgres store")
    try:
        conn = Connection.postgres(postgres_connect(target.url))
    except StoreError as error:
        raise OperatorError(str(error)) from None
    try:
        preflight(conn)
        yield OperatorSession(admin, conn)
    finally:
        conn.close()


def run_erase(
    environ: Mapping[str, str],
    user_id: str,
    confirm: str,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """`harrier operator erase <user id> --confirm <user id>`.

    Refused before any call, with a text that names a variable or a rule and
    never a value: a `--confirm` that is not the id repeated, an id that is
    not a UUID (an address is never accepted, and never echoed), a missing
    or malformed variable, the secret key in `.env`, a login that cannot act
    as `service_role`, and a store without the erase function.
    """
    out = out if out is not None else cast("TextIO", sys.stdout)
    err = err if err is not None else cast("TextIO", sys.stderr)
    try:
        if confirm != user_id:
            raise OperatorError(CONFIRM_MISMATCH)
        if not _is_uuid(user_id) or str(uuid.UUID(user_id)) != user_id.lower():
            raise OperatorError(NOT_A_USER_ID)
        owner = str(uuid.UUID(user_id))
        with operator_session(environ) as session, quiet_http_logs(), http_client() as http:
            client = AdminClient(session.admin.origin, session.admin.secret_key, http)
            return erase_owner(owner, client, session.conn, ledger_path(), out, err)
    except OperatorError as error:
        print(error.line(), file=err)
        return 1


# --- erase-test-owners (spec 143) ---

# The rule of spec 143, "Which owners are synthetic", as a condition on a row
# of `auth.users` aliased `u`. It reads only that row and `auth.identities`;
# it reads no row of any owned table. `is_anonymous IS FALSE` and not `NOT
# is_anonymous`: a null does not match, so the rule errs toward keeping a user.
SYNTHETIC_OWNER_RULE = """
    u.email IS NULL AND u.phone IS NULL
    AND u.is_anonymous IS FALSE
    AND u.last_sign_in_at IS NULL
    AND NOT EXISTS (SELECT 1 FROM auth.identities i WHERE i.user_id = u.id)
"""

# One progress line per this many owners (spec 143).
PROGRESS_EVERY = 10

RULE_UNREADABLE = (
    "the login cannot read the columns of auth.users and auth.identities that "
    "the synthetic owner rule uses"
)


def find_synthetic_owners(conn: Connection) -> list[str]:
    """The id of every synthetic owner, in `auth.users` id order. Read as the
    operator's login, which can read `auth.users` (the migrator's login on a
    Supabase project does)."""
    try:
        rows = conn.execute(
            f"SELECT u.id::text FROM auth.users u WHERE {SYNTHETIC_OWNER_RULE} ORDER BY u.id"
        ).fetchall()
    except StoreDatabaseError:
        raise OperatorError(RULE_UNREADABLE) from None
    return [str(row[0]) for row in rows]


def is_synthetic_owner(conn: Connection, user_id: str) -> bool:
    """The rule again, for one id, just before it is erased."""
    try:
        row = conn.execute(
            f"SELECT 1 FROM auth.users u WHERE u.id = ?::uuid AND {SYNTHETIC_OWNER_RULE}",
            (user_id,),
        ).fetchone()
    except StoreDatabaseError:
        raise OperatorError(RULE_UNREADABLE) from None
    return row is not None


def count_owned_rows(conn: Connection) -> list[tuple[str, int]]:
    """Each owned table and the rows the synthetic owners hold in it. Counts
    only: no id and no row content is read."""
    from harrier.tracker.schema import OWNED_TABLES

    counts: list[tuple[str, int]] = []
    try:
        for table in OWNED_TABLES:
            row = conn.execute(
                f"SELECT count(*) FROM {table} WHERE owner_id IN "
                f"(SELECT u.id FROM auth.users u WHERE {SYNTHETIC_OWNER_RULE})"
            ).fetchone()
            counts.append((table, int(row[0]) if row is not None else 0))
    except StoreDatabaseError:
        raise OperatorError(RULE_UNREADABLE) from None
    return counts


def count_mismatch(confirmed: int, found: int) -> str:
    return (
        f"--confirm {confirmed} does not match the {found} synthetic owners found now; "
        "nothing was erased"
    )


def run_erase_test_owners(
    environ: Mapping[str, str],
    confirm: int | None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """`harrier operator erase-test-owners [--confirm <n>]` (spec 143).

    Without `--confirm`: the count, the rows they hold per owned table, and
    `nothing erased`. Exit 0, nothing written, no user id printed.

    With `--confirm <n>`: refused with exit 1, before any erase, unless `n`
    is the count found now. Then each synthetic owner, in id order, is
    checked against the rule again and erased by `erase_owner`, spec 111's
    six steps unchanged. The first failure stops the run with exit 1 and the
    failed step named; a re-run finds the owners that remain.
    """
    out = out if out is not None else cast("TextIO", sys.stdout)
    err = err if err is not None else cast("TextIO", sys.stderr)
    try:
        with operator_session(environ) as session:
            conn = session.conn
            owners = find_synthetic_owners(conn)
            if confirm is None:
                print(f"synthetic test owners: {len(owners)}", file=out)
                for table, held in count_owned_rows(conn):
                    print(f"rows held: {table} {held}", file=out)
                print(f"nothing erased; repeat with --confirm {len(owners)}", file=out)
                return 0
            if confirm != len(owners):
                raise OperatorError(count_mismatch(confirm, len(owners)))
            with quiet_http_logs(), http_client() as http:
                client = AdminClient(session.admin.origin, session.admin.secret_key, http)
                return _erase_each(owners, client, conn, out, err)
    except OperatorError as error:
        print(error.line(), file=err)
        return 1


def _erase_each(
    owners: Sequence[str], client: AdminClient, conn: Connection, out: TextIO, err: TextIO
) -> int:
    """The confirmed run: one `erase_owner` per owner still matching the rule.
    Its per-step lines are dropped; the progress and final lines replace them
    (spec 143)."""
    from harrier.tracker.schema import OWNED_TABLES

    erased = 0
    skipped = 0
    totals = dict.fromkeys(OWNED_TABLES, 0)
    ledger = ledger_path()
    for position, owner in enumerate(owners, start=1):
        if not is_synthetic_owner(conn, owner):
            skipped += 1
        else:
            counts: list[tuple[str, int]] = []
            if erase_owner(owner, client, conn, ledger, io.StringIO(), err, counts) != 0:
                print(
                    f"stopped after {erased} erased and {skipped} skipped; "
                    "run the command again to continue",
                    file=err,
                )
                return 1
            erased += 1
            for table, rows in counts:
                totals[table] = totals.get(table, 0) + rows
        if position % PROGRESS_EVERY == 0:
            print(f"{position} of {len(owners)} owners done", file=out)
    held = ", ".join(f"{table} {rows}" for table, rows in totals.items())
    print(f"erased {erased}, skipped {skipped}; rows deleted: {held}", file=out)
    return 0
