"""The hosted store's nightly backup, and the restore that proves it (spec 153).

`.github/workflows/backup.yml` runs three commands in order, and each is a
function here so the logic is tested in Python and not only read from YAML:

1. `harrier store backup-dump` (`dump_hosted`). As the migrator, in one
   REPEATABLE READ transaction, it exports a snapshot, reads the store's
   schema version and each table's row count, and runs `pg_dump` twice at
   that snapshot: the `public` schema (every table with its rows, policies,
   functions and `schema_version`), and the two `auth` tables every owned
   row references, `auth.users` and `auth.identities`. Nothing else from
   `auth` or `storage` is dumped. The counts and the version are the
   manifest, so a dump that lost a row cannot match it.
2. `harrier store backup-check` (`check_restore`). It restores the dumps,
   plain, into an empty Postgres and compares every table's count and the
   schema version with the manifest. The job holds no private key, so this
   runs before anything is encrypted. A difference raises, and the job
   stops with nothing uploaded. A pass leaves a marker holding the digest of
   each file it checked.
3. `harrier store backup-seal` (`seal`). It refuses a directory the check
   did not pass or one changed since, then encrypts the dumps and the
   manifest together to the `age` recipient in `deploy/backup-recipient.txt`.

The restore the check performs is the restore `docs/deploy.md` documents: the
same order, so the check is a rehearsal of the procedure. The `public` dump
restores in two parts around the `auth` dump, because the `auth` dump carries
the trigger on `auth.users` that calls a function in `public`, and `public`'s
foreign keys need `auth.users`' key.

Nothing here prints a URL, a host, an owner or a row's value. A log line names
a table and counts, or a step and an exit status. A tool's own output is
captured and dropped, since `pg_dump` quotes the host in its errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from harrier.connection import Connection, HostedBackupRefused, StoreDatabaseError
from harrier.tracker.schema import SCHEDULE_READER_ROLE, SERVICE_ROLE, SUPABASE_ROLES, TENANT_ROLE

# The server's major version. The dump tool must be at least this: pg_dump
# refuses a server newer than itself.
PG_MAJOR = 17

PUBLIC_DUMP = "public.dump"
AUTH_DUMP = "auth.dump"
MANIFEST = "manifest.json"
CHECKED = "checked.json"
BUNDLE: tuple[str, ...] = (PUBLIC_DUMP, AUTH_DUMP, MANIFEST)

# The `auth` tables every owned row references (spec 153, Decisions taken).
AUTH_TABLES: tuple[str, ...] = ("auth.users", "auth.identities")

# Where a PostgreSQL client may be found beside, or instead of, PATH: the
# variable names a directory first (a test or a machine with the client
# somewhere unusual), then the packages' own.
BIN_VARIABLE = "HARRIER_PG_BIN"
CLIENT_DIRECTORIES: tuple[str, ...] = (
    f"/usr/lib/postgresql/{PG_MAJOR}/bin",
    f"/opt/homebrew/opt/postgresql@{PG_MAJOR}/bin",
    f"/usr/local/opt/postgresql@{PG_MAJOR}/bin",
)

# One tool run may take this long. A dump of one person's search is small; an
# hour is a hang, which the job then reports as a failure.
TOOL_TIMEOUT_SECONDS = 3600

# The recipient file: one `age` public key. The committed placeholder holds
# this word instead and the seal refuses it, so a run before the operator
# makes the key fails loudly (spec 153, Migration).
RECIPIENT_PLACEHOLDER = "REPLACE_WITH_AGE_PUBLIC_KEY"
_RECIPIENT = re.compile(r"age1[02-9ac-hj-np-z]{58}")

MANIFEST_FORMAT = 1

# The lines of `pg_restore --list` that make or describe the `public` schema
# itself. A Supabase project already has that schema, with the grants its
# roles rely on, so a restore leaves these out (`-L`) and fills the schema
# instead of dropping and making it again. The runbook shows the same pattern
# to `grep -v -E`.
PUBLIC_SCHEMA_LINES = " (SCHEMA - public|COMMENT - SCHEMA public|ACL - SCHEMA public) "

PLACEHOLDER_REFUSAL = (
    "the recipient is the placeholder; replace it with the operator's age public key"
)
NOT_EMPTY_REFUSAL = (
    "the restore check runs only in an empty database; this one has an auth schema or a table"
)

Report = Callable[[str], None]


# --- the manifest ---


@dataclass(frozen=True)
class Manifest:
    """What a dump holds: when it started, the store's schema version and
    each table's row count. It names no owner and no value."""

    started_at: str
    schema_version: int
    tables: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(
            {
                "format": MANIFEST_FORMAT,
                "started_at": self.started_at,
                "schema_version": self.schema_version,
                "tables": dict(sorted(self.tables.items())),
            },
            indent=2,
        )

    @classmethod
    def read(cls, path: Path) -> Manifest:
        try:
            document: object = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError
            body = cast("dict[str, object]", document)
            tables = body["tables"]
            if body["format"] != MANIFEST_FORMAT or not isinstance(tables, dict):
                raise ValueError
            counts = cast("dict[str, object]", tables)
            return cls(
                started_at=str(body["started_at"]),
                schema_version=int(str(body["schema_version"])),
                tables={str(name): int(str(count)) for name, count in counts.items()},
            )
        except (OSError, ValueError, KeyError, TypeError):
            raise HostedBackupRefused(
                "the manifest is missing or not one this code reads"
            ) from None


# --- the tools ---


def pg_tool(name: str, environ: Mapping[str, str]) -> str:
    """The path of a PostgreSQL client program. `HARRIER_PG_BIN`, when set,
    is the only place looked: a directory that lacks the program is refused,
    never quietly replaced by another client. Unset, the packages'
    directories for the server's major version come first, then PATH."""
    override = environ.get(BIN_VARIABLE, "").strip()
    if override:
        candidate = Path(override) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise HostedBackupRefused(f"{BIN_VARIABLE} names a directory with no {name}")
    for directory in CLIENT_DIRECTORIES:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which(name)
    if found is None:
        raise HostedBackupRefused(f"{name} was not found; install the PostgreSQL {PG_MAJOR} client")
    return found


def run_tool(step: str, argv: list[str]) -> None:
    """Run a tool and drop its output. A tool's text can quote the host or
    the user, so the failure names the step and the exit status only."""
    try:
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise HostedBackupRefused(f"{step} did not finish in time") from None
    except OSError:
        raise HostedBackupRefused(f"{step} could not be started") from None
    if done.returncode != 0:
        raise HostedBackupRefused(f"{step} failed (exit {done.returncode})")


def public_list(pg_restore: str, dump: Path) -> str:
    """The dump's table of contents without the `public` schema's own lines,
    for `pg_restore --use-list`."""
    try:
        done = subprocess.run(
            [pg_restore, "--list", str(dump)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=TOOL_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError):
        raise HostedBackupRefused("reading the public dump's contents failed") from None
    if done.returncode != 0:
        raise HostedBackupRefused(
            f"reading the public dump's contents failed (exit {done.returncode})"
        )
    kept = [
        line
        for line in done.stdout.decode("utf-8", errors="replace").splitlines()
        if re.search(PUBLIC_SCHEMA_LINES, line) is None
    ]
    return "\n".join(kept) + "\n"


# --- the store's counts, read the same way before and after ---


def quote(schema: str, table: str) -> str:
    return '"' + schema.replace('"', '""') + '"."' + table.replace('"', '""') + '"'


def dumped_tables(conn: Connection) -> list[tuple[str, str]]:
    """Every table the backup holds: the `public` schema's ordinary and
    partitioned tables, then the two `auth` tables. A missing `auth` table is
    a refusal in the dump and a difference in the check."""
    rows = conn.execute(
        "SELECT n.nspname::text, c.relname::text FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
        "ORDER BY c.relname"
    ).fetchall()
    tables = [(str(row[0]), str(row[1])) for row in rows]
    for qualified in AUTH_TABLES:
        schema, _, table = qualified.partition(".")
        found = conn.execute("SELECT to_regclass(?) IS NOT NULL", (qualified,)).fetchone()
        if found is not None and found[0]:
            tables.append((schema, table))
    return tables


def schema_version(conn: Connection) -> int:
    """The store's recorded version; 0 when it has no `schema_version`."""
    exists = conn.execute("SELECT to_regclass('public.schema_version') IS NOT NULL").fetchone()
    if exists is None or not exists[0]:
        return 0
    row = conn.execute("SELECT MAX(version) FROM public.schema_version").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def read_counts(conn: Connection) -> tuple[int, dict[str, int]]:
    """The schema version and each dumped table's count, as `schema.table`."""
    version = schema_version(conn)
    counts: dict[str, int] = {}
    for schema, table in dumped_tables(conn):
        row = conn.execute(f"SELECT count(*) FROM {quote(schema, table)}").fetchone()
        counts[f"{schema}.{table}"] = int(row[0]) if row is not None else 0
    return version, counts


def _connect(url: str) -> Connection:
    from harrier.pgstore import postgres_connect

    return Connection.postgres(cast("Any", postgres_connect(url)))


# --- step 1: the dump ---


def dump_hosted(
    url: str, out: Path, report: Report, environ: Mapping[str, str] | None = None
) -> Manifest:
    """Write `public.dump`, `auth.dump` and `manifest.json` into `out`.

    `url` is the migrator's. The directory must not exist or must be empty.
    A failure removes what was written and raises HostedBackupRefused.
    """
    environment = os.environ if environ is None else environ
    pg_dump = pg_tool("pg_dump", environment)
    pg_restore = pg_tool("pg_restore", environment)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise HostedBackupRefused("the output directory is not empty")
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    conn = _connect(url)
    try:
        # One REPEATABLE READ transaction holds the snapshot open while both
        # dumps import it, and reads the manifest's figures from the same
        # snapshot, so the dumps and the counts are one moment of the store.
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            started = conn.execute(
                "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', "
                '\'YYYY-MM-DD"T"HH24:MI:SS"Z"\')'
            ).fetchone()
            snapshot = conn.execute("SELECT pg_export_snapshot()").fetchone()
            if started is None or snapshot is None:
                raise HostedBackupRefused("the store did not answer the snapshot request")
            version, counts = read_counts(conn)
            missing = [name for name in AUTH_TABLES if name not in counts]
            if missing:
                raise HostedBackupRefused(
                    f"the store has no {missing[0]}; is this a Supabase project?"
                )
            report(f"schema version: {version}")
            for name, count in counts.items():
                report(f"{name}: {count}")
            common = ["--format=custom", "--no-password", f"--snapshot={snapshot[0]}"]
            run_tool(
                "pg_dump of the public schema",
                [
                    pg_dump,
                    *common,
                    "--schema=public",
                    f"--file={out / PUBLIC_DUMP}",
                    f"--dbname={url}",
                ],
            )
            run_tool(
                "pg_dump of the auth tables",
                [
                    pg_dump,
                    *common,
                    *(f"--table={name}" for name in AUTH_TABLES),
                    f"--file={out / AUTH_DUMP}",
                    f"--dbname={url}",
                ],
            )
        for name in (PUBLIC_DUMP, AUTH_DUMP):
            run_tool(f"reading {name}", [pg_restore, "--list", str(out / name)])
        manifest = Manifest(str(started[0]), version, counts)
        (out / MANIFEST).write_text(manifest.to_json() + "\n", encoding="utf-8")
    except StoreDatabaseError as error:
        _clean(out)
        raise HostedBackupRefused(f"the dump: {error}") from None
    except BaseException:
        _clean(out)
        raise
    finally:
        conn.close()
    report("dumped")
    return manifest


def _clean(out: Path) -> None:
    """Remove a failed dump's files, so nothing half-written is sealed."""
    for name in (*BUNDLE, CHECKED):
        (out / name).unlink(missing_ok=True)


# --- step 2: the restore check ---


# The roles the dump's policies are granted to, which a Supabase project has
# before harrier's tables arrive. `tests/test_hosted_backup.py` reads the
# dump's policies and fails when one names a role that is not here.
SCAFFOLD_ROLES: tuple[str, ...] = tuple(
    dict.fromkeys((TENANT_ROLE, SCHEDULE_READER_ROLE, SERVICE_ROLE, *SUPABASE_ROLES))
)


def scaffold(conn: Connection, roles: tuple[str, ...] = SCAFFOLD_ROLES) -> None:
    """What a Supabase project has before harrier's tables arrive: the roles
    its policies name, and the `auth` schema. Created only here, in an empty
    database, since a restore into an existing project is the operator's.
    A role that exists is left as it is: roles belong to the whole server."""
    for role in roles:
        conn.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                    CREATE ROLE {role} NOLOGIN;
                END IF;
            EXCEPTION WHEN duplicate_object OR unique_violation THEN
                NULL;
            END
            $$
            """
        )
    conn.execute("CREATE SCHEMA auth")


def _refuse_unless_empty(conn: Connection) -> None:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth') "
        "OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))"
    ).fetchone()
    if row is None or row[0]:
        raise HostedBackupRefused(NOT_EMPTY_REFUSAL)


def restore_arguments(into: str) -> list[str]:
    """The options every restore step shares: stop at the first error, and
    keep neither the dump's owners nor its grants, which name roles of the
    source project."""
    return ["--exit-on-error", "--no-owner", "--no-privileges", "--no-password", f"--dbname={into}"]


def restore_steps(
    directory: Path, into: str, pg_restore: str, public_contents: Path
) -> list[tuple[str, list[str]]]:
    """The restore, in order, as the check runs it and `docs/deploy.md`
    states it. `public_contents` is `public_list`'s output in a file."""
    options = restore_arguments(into)
    public = str(directory / PUBLIC_DUMP)
    use_list = f"--use-list={public_contents}"
    return [
        (
            "restoring the public schema",
            [pg_restore, *options, use_list, "--section=pre-data", public],
        ),
        ("restoring the auth tables", [pg_restore, *options, str(directory / AUTH_DUMP)]),
        (
            "restoring the public data",
            [pg_restore, *options, use_list, "--section=data", "--section=post-data", public],
        ),
    ]


def digests(directory: Path) -> dict[str, str]:
    """The SHA-256 of each file of the bundle."""
    found: dict[str, str] = {}
    for name in BUNDLE:
        try:
            found[name] = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        except OSError:
            raise HostedBackupRefused(f"{name} is missing or cannot be read") from None
    return found


def compare(manifest: Manifest, version: int, counts: Mapping[str, int]) -> list[str]:
    """Each difference between the manifest and what the restore holds, as a
    line that names a table or the schema version, with counts."""
    problems: list[str] = []
    if version != manifest.schema_version:
        problems.append(f"schema version: dumped {manifest.schema_version}, restored {version}")
    for name in sorted(set(manifest.tables) | set(counts)):
        dumped = manifest.tables.get(name)
        restored = counts.get(name)
        if dumped != restored:
            problems.append(
                f"{name}: dumped {'none' if dumped is None else dumped}, "
                f"restored {'none' if restored is None else restored}"
            )
    return problems


def check_restore(
    directory: Path, into: str, report: Report, environ: Mapping[str, str] | None = None
) -> None:
    """Restore the dumps into the empty database `into` names and compare
    with the manifest. Raises HostedBackupRefused on any difference."""
    environment = os.environ if environ is None else environ
    (directory / CHECKED).unlink(missing_ok=True)
    manifest = Manifest.read(directory / MANIFEST)
    before = digests(directory)
    pg_restore = pg_tool("pg_restore", environment)
    conn = _connect(into)
    try:
        _refuse_unless_empty(conn)
        scaffold(conn)
        with tempfile.TemporaryDirectory() as scratch:
            contents = Path(scratch) / "public.list"
            contents.write_text(public_list(pg_restore, directory / PUBLIC_DUMP), encoding="utf-8")
            for step, argv in restore_steps(directory, into, pg_restore, contents):
                run_tool(step, argv)
        version, counts = read_counts(conn)
    except StoreDatabaseError as error:
        raise HostedBackupRefused(f"the restore check: {error}") from None
    finally:
        conn.close()
    problems = compare(manifest, version, counts)
    if problems:
        for line in problems:
            report(f"differs: {line}")
        raise HostedBackupRefused("the restore check found a difference from the manifest")
    report(f"schema version: {version}")
    for name, count in counts.items():
        report(f"{name}: {count}")
    (directory / CHECKED).write_text(json.dumps(before, indent=2) + "\n", encoding="utf-8")
    report("restore check passed")


# --- step 3: the seal ---


def read_recipient(path: Path) -> str:
    """The one `age` public key in `path`, or HostedBackupRefused. Blank lines
    and `#` comments are allowed; the placeholder is refused in words."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise HostedBackupRefused("the recipient file is missing or cannot be read") from None
    lines = [line.strip() for line in text.splitlines()]
    keys = [line for line in lines if line and not line.startswith("#")]
    if keys == [RECIPIENT_PLACEHOLDER]:
        raise HostedBackupRefused(PLACEHOLDER_REFUSAL)
    if len(keys) != 1 or _RECIPIENT.fullmatch(keys[0]) is None:
        raise HostedBackupRefused("the recipient file must hold one age public key (age1...)")
    return keys[0]


def _check_passed(directory: Path) -> None:
    """The restore check passed for exactly these files."""
    try:
        marker: object = json.loads((directory / CHECKED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HostedBackupRefused("the restore check has not passed for this directory") from None
    if marker != digests(directory):
        raise HostedBackupRefused("a file changed after the restore check passed")


def _plain(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """No owner, no group, one mode: the archive carries nothing of the runner."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o600
    return info


def _tar_into(directory: Path, stream: IO[bytes]) -> None:
    with tarfile.open(fileobj=stream, mode="w|") as archive:
        for name in BUNDLE:
            archive.add(directory / name, arcname=name, filter=_plain)


def seal(directory: Path, recipient_file: Path, out: Path) -> None:
    """Encrypt the bundle, as one tar, to the recipient. Refuses a recipient
    that is missing, malformed or the placeholder; a directory the check did
    not pass; and an `out` that exists. Writes nothing unencrypted."""
    recipient = read_recipient(recipient_file)
    _check_passed(directory)
    if out.exists():
        raise HostedBackupRefused("the output file already exists")
    age = shutil.which("age")
    if age is None:
        raise HostedBackupRefused("age was not found; install it (apt-get install age)")
    try:
        process = subprocess.Popen(
            [age, "--recipient", recipient, "--output", str(out)],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        raise HostedBackupRefused("age could not be started") from None
    try:
        assert process.stdin is not None
        _tar_into(directory, process.stdin)
        process.stdin.close()
        status = process.wait(timeout=TOOL_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait()
        out.unlink(missing_ok=True)
        raise HostedBackupRefused("the encryption did not finish") from None
    if status != 0:
        out.unlink(missing_ok=True)
        raise HostedBackupRefused(f"the encryption failed (exit {status})")
