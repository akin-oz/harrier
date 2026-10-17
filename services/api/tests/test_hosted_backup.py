"""The hosted store's nightly backup, its restore check and its seal (spec 153).

The dump and the check run the real `pg_dump` and `pg_restore` against a
throwaway Postgres: the CI service or the local container
(`HARRIER_TEST_POSTGRES_URL`, `tests/pg_support.py`). They need a PostgreSQL 17
client; `HARRIER_PG_BIN` names a directory that holds one when it is not where
the packages put it. Locally, without a server or a client, those tests skip;
in CI (`CI=true`) they fail, since a gate that skips has not run (spec 039).
The tests of the seal that need `age` skip where it is not installed.

Every owner, company, URL and key here is synthetic. The nightly workflow's own
file is read in `tests/test_backup_workflow.py`.
"""

# Pyright strict cannot resolve psycopg's row types.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import uuid
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from pg_support import (
    MIGRATOR_ROLE,
    database_copy,
    fresh_database,
    new_owner,
    open_owner_store,
    superuser_url,
)

from harrier import hostedbackup
from harrier.connection import HostedBackupRefused
from harrier.demo import repo_root
from harrier.hostedbackup import (
    AUTH_DUMP,
    BIN_VARIABLE,
    BUNDLE,
    CHECKED,
    MANIFEST,
    NOT_EMPTY_REFUSAL,
    PG_MAJOR,
    PLACEHOLDER_REFUSAL,
    PUBLIC_DUMP,
    RECIPIENT_PLACEHOLDER,
    Manifest,
    check_restore,
    digests,
    dump_hosted,
    pg_tool,
    read_recipient,
    seal,
)
from harrier.pgstore import postgres_connect, target_version
from harrier.tracker import add_job
from harrier.tracks import default_scope
from harrier_cli.main import main

ROOT = repo_root()
COMPANY = "Example Co"
# Nothing listens on port 1, so a connection is refused at once. The password
# is synthetic, and the tests show it never reaches an output.
UNREACHABLE_URL = "postgresql://dumper:synthetic-password-0123@127.0.0.1:1/nowhere"
# Syntactically an age recipient, with a checksum no key made: `age` rejects it.
SHAPED_LIKE_A_RECIPIENT = "age1" + "q" * 58


def in_ci() -> bool:
    return os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}


def missing(reason: str) -> None:
    """Fail in CI and skip locally, for a tool the test cannot run without."""
    if in_ci():
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason)


@pytest.fixture()
def pg_client() -> None:
    """A PostgreSQL client at the server's major version, or skip (locally)."""
    try:
        for name in ("pg_dump", "pg_restore"):
            tool = pg_tool(name, os.environ)
        done = subprocess.run([tool, "--version"], capture_output=True, text=True, check=False)
    except (HostedBackupRefused, OSError) as error:
        missing(f"no PostgreSQL {PG_MAJOR} client: {error}")
        return
    words = done.stdout.split()
    major = int(words[-1].split(".")[0]) if words and words[-1][:1].isdigit() else 0
    if major < PG_MAJOR:
        missing(f"the PostgreSQL client is version {major}; the backup needs {PG_MAJOR}")


# --- a synthetic hosted store ---


@dataclass
class Source:
    url: str
    owners: list[str]


def add_identities_table(url: str) -> None:
    """Supabase's `auth.identities`, as far as the backup reads it. The shim
    of `pg_support` has `auth.users` alone."""
    with psycopg.connect(superuser_url(url), autocommit=True) as admin:
        admin.execute(
            b"CREATE TABLE auth.identities (id uuid PRIMARY KEY, "
            b"user_id uuid NOT NULL REFERENCES auth.users (id), identity_data jsonb NOT NULL)"
        )
        admin.execute(f"GRANT ALL ON auth.identities TO {MIGRATOR_ROLE}".encode())


def add_jobs(url: str, owner: str, titles: list[str]) -> None:
    conn = open_owner_store(url, owner)
    try:
        for title in titles:
            add_job(
                conn,
                {
                    "company": COMPANY,
                    "title": title,
                    "url": f"https://example.test/{uuid.uuid4().hex}",
                    "source": "manual",
                },
                scope=default_scope(conn),
            )
    finally:
        conn.close()


def seed(url: str, *, identities: bool = True) -> Source:
    """Two owners, each with the track the new-owner trigger gives, three jobs
    in all, and an identity for each owner."""
    if identities:
        add_identities_table(url)
    with closing(postgres_connect(url)) as admin:
        owners = [new_owner(admin), new_owner(admin)]
        if identities:
            for owner in owners:
                admin.execute(
                    b"INSERT INTO auth.identities (id, user_id, identity_data) "
                    b"VALUES (%s, %s, '{}')",
                    (str(uuid.uuid4()), owner),
                )
    add_jobs(url, owners[0], ["Role A1", "Role A2"])
    add_jobs(url, owners[1], ["Role B1"])
    return Source(url, owners)


@contextmanager
def seeded_store(template: str, *, identities: bool = True) -> Generator[Source]:
    with database_copy(template) as url:
        yield seed(url, identities=identities)


def count_in(url: str, table: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}".encode()).fetchone()
    assert row is not None
    return int(row[0])


def version_in(url: str) -> int:
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(b"SELECT max(version) FROM public.schema_version").fetchone()
    assert row is not None
    return int(row[0])


@dataclass
class Dumped:
    source: Source
    directory: Path
    manifest: Manifest


@pytest.fixture(scope="module")
def dumped(postgres_template: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Dumped]:
    """One source store and its dump, made once for the tests that only read
    the dump or tamper with a copy of it."""
    try:
        pg_tool("pg_dump", os.environ)
        pg_tool("pg_restore", os.environ)
    except HostedBackupRefused as error:
        missing(f"no PostgreSQL {PG_MAJOR} client: {error}")
    with seeded_store(postgres_template) as source:
        directory = tmp_path_factory.mktemp("dump") / "backup"
        manifest = dump_hosted(source.url, directory, lambda _line: None)
        yield Dumped(source, directory, manifest)


def copy_of(dump: Dumped, tmp_path: Path) -> Path:
    """The dump in a directory of the test's own, free to change."""
    target = tmp_path / "backup"
    shutil.copytree(dump.directory, target)
    return target


# --- the dump and its restore ---


def test_a_hosted_dump_restores_with_its_manifest_counts(
    pg_client: None, postgres_template: str, tmp_path: Path
) -> None:
    """The dump command, run against the Postgres service with the shim and
    synthetic owners, restores into an empty database with every table's count
    and the schema version equal to the manifest (spec 153, acceptance)."""
    with seeded_store(postgres_template) as source, fresh_database(shim=False) as empty:
        lines: list[str] = []
        directory = tmp_path / "backup"
        manifest = dump_hosted(source.url, directory, lines.append)

        assert sorted(path.name for path in directory.iterdir()) == sorted(BUNDLE)
        assert manifest.schema_version == target_version()
        assert manifest.tables["auth.users"] == 2
        assert manifest.tables["auth.identities"] == 2
        assert manifest.tables["public.jobs"] == 3
        # The manifest is what the source holds, read apart from the code under test.
        for table, count in manifest.tables.items():
            assert count == count_in(source.url, table), table
        # It names no owner and no value.
        text = (directory / MANIFEST).read_text(encoding="utf-8")
        assert COMPANY not in text and "Role A1" not in text
        assert all(owner not in text for owner in source.owners)
        assert json.loads(text)["tables"] == manifest.tables
        assert set(json.loads(text)) == {"format", "started_at", "schema_version", "tables"}
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest.started_at)
        assert all(name.startswith(("public.", "auth.")) for name in manifest.tables)

        check_restore(directory, empty, lines.append)

        # The restored database, looked at apart from the check.
        assert version_in(empty) == manifest.schema_version
        for table, count in manifest.tables.items():
            assert count_in(empty, table) == count, table
        with psycopg.connect(empty, autocommit=True) as conn:
            auth_tables = {
                str(row[0])
                for row in conn.execute(
                    b"SELECT table_name FROM information_schema.tables WHERE table_schema = 'auth'"
                ).fetchall()
            }
            trigger = conn.execute(
                b"SELECT count(*) FROM pg_trigger "
                b"WHERE tgrelid = 'auth.users'::regclass AND tgname = 'harrier_new_owner'"
            ).fetchone()
        # Only the two tables, and the trigger that gives a new owner a track.
        assert auth_tables == {"users", "identities"}
        assert trigger == (1,)
        assert (directory / CHECKED).is_file()
        assert lines[-1] == "restore check passed"


def test_the_dump_is_one_moment_of_the_store(
    pg_client: None, postgres_template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job added after the snapshot is in neither dump nor manifest, so a
    store written to while the dump runs still passes its own check."""
    original = hostedbackup.run_tool
    wrote: list[bool] = []

    def writing_first(step: str, argv: list[str]) -> None:
        if step.startswith("pg_dump") and not wrote:
            add_jobs(source.url, source.owners[0], ["Role added during the dump"])
            wrote.append(True)
        original(step, argv)

    monkeypatch.setattr(hostedbackup, "run_tool", writing_first)
    with seeded_store(postgres_template) as source, fresh_database(shim=False) as empty:
        directory = tmp_path / "backup"
        manifest = dump_hosted(source.url, directory, lambda _line: None)
        assert wrote == [True]
        assert count_in(source.url, "public.jobs") == 4
        assert manifest.tables["public.jobs"] == 3
        check_restore(directory, empty, lambda _line: None)
        assert count_in(empty, "public.jobs") == 3


# --- the check refuses what it should ---


@pytest.mark.parametrize("change", ["count", "version", "absent", "extra"])
def test_a_restore_check_that_finds_a_difference_fails_and_seals_nothing(
    change: str,
    pg_client: None,
    dumped: Dumped,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The check, run as the command, exits 1 on a count, a version or a table
    that differs from the manifest. It names the table and the two counts,
    leaves no marker, and the seal then refuses (spec 153, acceptance)."""
    directory = copy_of(dumped, tmp_path)
    document = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    expected = {
        "count": "differs: public.jobs: dumped 4, restored 3",
        "version": f"differs: schema version: dumped {target_version() + 1}, "
        f"restored {target_version()}",
        "absent": "differs: public.jobs: dumped none, restored 3",
        "extra": "differs: public.not_a_table: dumped 5, restored none",
    }[change]
    if change == "count":
        document["tables"]["public.jobs"] += 1
    elif change == "version":
        document["schema_version"] += 1
    elif change == "absent":
        del document["tables"]["public.jobs"]
    else:
        document["tables"]["public.not_a_table"] = 5
    (directory / MANIFEST).write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    with fresh_database(shim=False) as empty:
        code = main(["store", "backup-check", "--dir", str(directory), "--into", empty])
    captured = capsys.readouterr()

    assert code == 1
    assert expected in captured.out.splitlines()
    assert captured.err == "error: the restore check found a difference from the manifest\n"
    assert not (directory / CHECKED).exists()
    recipient = tmp_path / "recipient.txt"
    recipient.write_text(SHAPED_LIKE_A_RECIPIENT + "\n", encoding="utf-8")
    with pytest.raises(HostedBackupRefused, match="has not passed"):
        seal(directory, recipient, tmp_path / "out.age")
    assert not (tmp_path / "out.age").exists()


def test_the_check_refuses_a_database_that_is_not_empty(
    pg_client: None, dumped: Dumped, tmp_path: Path
) -> None:
    """Pointed at the store the dump came from, or at any database with a table
    or an auth schema, the check restores nothing and changes nothing."""
    directory = copy_of(dumped, tmp_path)
    before = {table: count_in(dumped.source.url, table) for table in dumped.manifest.tables}

    with pytest.raises(HostedBackupRefused) as refused:
        check_restore(directory, dumped.source.url, lambda _line: None)
    assert str(refused.value) == NOT_EMPTY_REFUSAL

    with fresh_database(shim=False) as other:
        with psycopg.connect(other, autocommit=True) as conn:
            conn.execute(b"CREATE TABLE public.something (id integer)")
        with pytest.raises(HostedBackupRefused) as refused:
            check_restore(directory, other, lambda _line: None)
        assert str(refused.value) == NOT_EMPTY_REFUSAL
        assert count_in(other, "public.something") == 0
    assert before == {table: count_in(dumped.source.url, table) for table in before}
    assert not (directory / CHECKED).exists()


def test_a_missing_or_unreadable_manifest_or_dump_stops_the_check_before_it_connects(
    tmp_path: Path,
) -> None:
    """Nothing is restored from a directory that is not a whole dump."""
    directory = tmp_path / "backup"
    directory.mkdir()
    with pytest.raises(HostedBackupRefused, match="manifest is missing"):
        check_restore(directory, UNREACHABLE_URL, lambda _line: None)
    (directory / MANIFEST).write_text("not json", encoding="utf-8")
    with pytest.raises(HostedBackupRefused, match="manifest is missing"):
        check_restore(directory, UNREACHABLE_URL, lambda _line: None)
    (directory / MANIFEST).write_text(
        Manifest("2026-10-17T03:30:00Z", 1, {}).to_json(), encoding="utf-8"
    )
    with pytest.raises(HostedBackupRefused, match=f"{PUBLIC_DUMP} is missing"):
        check_restore(directory, UNREACHABLE_URL, lambda _line: None)


# --- the dump refuses what it should ---


def fake_pg_tools(directory: Path, pg_dump_script: str) -> Path:
    """A directory with a `pg_dump` that is the script and the real
    `pg_restore`, for `HARRIER_PG_BIN`."""
    directory.mkdir(parents=True, exist_ok=True)
    dump = directory / "pg_dump"
    dump.write_text(pg_dump_script, encoding="utf-8")
    dump.chmod(0o755)
    (directory / "pg_restore").symlink_to(pg_tool("pg_restore", os.environ))
    return directory


def test_a_failed_pg_dump_leaves_nothing_to_seal_and_names_no_host(
    pg_client: None, dumped: Dumped, tmp_path: Path
) -> None:
    """A `pg_dump` that is cut off after writing part of a file, and quotes the
    connection in its error, fails the dump with the step and the exit status
    alone, and removes what it left."""
    quoting = (
        '#!/bin/sh\nfor a in "$@"; do case "$a" in --file=*) echo partial > "${a#--file=}";; '
        'esac; done\necho "pg_dump: error: connection to $*" >&2\nexit 3\n'
    )
    tools = fake_pg_tools(tmp_path / "bin", quoting)
    out = tmp_path / "backup"

    with pytest.raises(HostedBackupRefused) as refused:
        dump_hosted(dumped.source.url, out, lambda _line: None, {BIN_VARIABLE: str(tools)})

    assert str(refused.value) == "pg_dump of the public schema failed (exit 3)"
    assert list(out.iterdir()) == []
    assert "harrier_migrator" not in str(refused.value)


def test_a_store_without_auth_identities_is_refused_before_any_dump(
    pg_client: None, postgres_template: str, tmp_path: Path
) -> None:
    """The shim holds `auth.users` alone: a store that is not a Supabase
    project is refused by name, and pg_dump is never run."""
    tools = fake_pg_tools(tmp_path / "bin", f"#!/bin/sh\necho ran > {tmp_path}/ran.txt\nexit 0\n")
    with (
        seeded_store(postgres_template, identities=False) as source,
        pytest.raises(HostedBackupRefused) as refused,
    ):
        dump_hosted(source.url, tmp_path / "backup", lambda _line: None, {BIN_VARIABLE: str(tools)})
    assert str(refused.value) == "the store has no auth.identities; is this a Supabase project?"
    assert list((tmp_path / "backup").iterdir()) == []
    assert not (tmp_path / "ran.txt").exists()


def test_the_dump_refuses_a_directory_that_holds_files(pg_client: None, tmp_path: Path) -> None:
    out = tmp_path / "backup"
    out.mkdir()
    (out / "something").write_text("kept", encoding="utf-8")
    with pytest.raises(HostedBackupRefused, match="output directory is not empty"):
        dump_hosted(UNREACHABLE_URL, out, lambda _line: None)
    assert (out / "something").read_text(encoding="utf-8") == "kept"


def stub_tools(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("pg_dump", "pg_restore"):
        tool = directory / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    return directory


def test_the_dump_command_names_no_host_when_the_store_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A public log shows the failure of a run. The line says the store could
    not be reached and names neither the host, the user nor the password."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(BIN_VARIABLE, str(stub_tools(tmp_path / "bin")))
    monkeypatch.setenv("HARRIER_DATABASE_URL", UNREACHABLE_URL)

    code = main(["store", "backup-dump", "--out", str(tmp_path / "backup")])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err == (
        "error: harrier store backup-dump could not connect to the store; "
        "check the migrator's URL\n"
    )


def test_the_dump_command_needs_a_postgres_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["store", "backup-dump", "--out", str(tmp_path / "backup")])
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == "error: harrier store backup-dump needs a Postgres store\n"
    assert not (tmp_path / "backup").exists()


def test_the_dump_command_prints_table_names_and_counts_only(
    pg_client: None,
    postgres_template: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The log of a run names a table, a count and the schema version. It
    holds no owner, company, title, host, user or password."""
    monkeypatch.chdir(tmp_path)
    with seeded_store(postgres_template) as source:
        monkeypatch.setenv("HARRIER_DATABASE_URL", source.url)
        code = main(["store", "backup-dump", "--out", str(tmp_path / "backup")])
    captured = capsys.readouterr()

    assert (code, captured.err) == (0, "")
    lines = captured.out.splitlines()
    assert lines[0] == f"schema version: {target_version()}"
    assert lines[-1] == "dumped"
    assert "public.jobs: 3" in lines
    assert "auth.users: 2" in lines
    for line in lines[1:-1]:
        name, _, count = line.partition(": ")
        assert count.isdigit() and name.startswith(("public.", "auth.")), line
    for secret in (
        *source.owners,
        COMPANY,
        "Role A1",
        "harrier_migrator",
        "127.0.0.1",
        "localhost",
    ):
        assert secret not in captured.out


# --- the tools ---


def test_pg_tool_prefers_the_named_directory_and_names_a_missing_tool(tmp_path: Path) -> None:
    tools = stub_tools(tmp_path / "bin")
    assert pg_tool("pg_dump", {BIN_VARIABLE: str(tools)}) == str(tools / "pg_dump")
    with pytest.raises(HostedBackupRefused) as refused:
        pg_tool("pg_basebackup", {BIN_VARIABLE: str(tools)})
    assert str(refused.value) == f"{BIN_VARIABLE} names a directory with no pg_basebackup"


# --- the recipient and the seal ---


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_recipient_file_is_read_in_words(tmp_path: Path) -> None:
    key = SHAPED_LIKE_A_RECIPIENT
    assert read_recipient(write(tmp_path / "a", f"# a comment\n\n{key}\n")) == key
    with pytest.raises(HostedBackupRefused) as refused:
        read_recipient(write(tmp_path / "b", f"# a comment\n{RECIPIENT_PLACEHOLDER}\n"))
    assert str(refused.value) == PLACEHOLDER_REFUSAL
    assert PLACEHOLDER_REFUSAL == (
        "the recipient is the placeholder; replace it with the operator's age public key"
    )
    for text, message in (
        ("", "must hold one age public key"),
        (f"{key}\n{key}\n", "must hold one age public key"),
        ("age1tooshort\n", "must hold one age public key"),
        ("ssh-ed25519 AAAAC3Nza\n", "must hold one age public key"),
        (f"{key}x\n", "must hold one age public key"),
    ):
        with pytest.raises(HostedBackupRefused, match=message):
            read_recipient(write(tmp_path / "c", text))
    with pytest.raises(HostedBackupRefused, match="missing"):
        read_recipient(tmp_path / "absent.txt")


def test_the_committed_recipient_is_an_age_public_key() -> None:
    """The file the workflow seals to holds one `age` public key, and is not
    the placeholder the seal refuses. Its comment may name the placeholder
    word without making the file one."""
    found = read_recipient(ROOT / "deploy" / "backup-recipient.txt")
    assert found.startswith("age1") and len(found) == 62


def checked_bundle(tmp_path: Path) -> Path:
    """A directory shaped like a checked dump, with synthetic contents."""
    directory = tmp_path / "backup"
    directory.mkdir()
    (directory / PUBLIC_DUMP).write_bytes(b"PGDMP synthetic public " + os.urandom(64))
    (directory / AUTH_DUMP).write_bytes(b"PGDMP synthetic auth " + os.urandom(64))
    (directory / MANIFEST).write_text('{"synthetic": true}\n', encoding="utf-8")
    (directory / CHECKED).write_text(json.dumps(digests(directory)), encoding="utf-8")
    return directory


def test_the_seal_refuses_a_directory_the_check_did_not_pass_or_one_changed_since(
    tmp_path: Path,
) -> None:
    recipient = write(tmp_path / "recipient.txt", SHAPED_LIKE_A_RECIPIENT + "\n")
    out = tmp_path / "out.age"
    directory = checked_bundle(tmp_path)

    (directory / CHECKED).unlink()
    with pytest.raises(HostedBackupRefused, match="has not passed"):
        seal(directory, recipient, out)

    (directory / CHECKED).write_text(json.dumps(digests(directory)), encoding="utf-8")
    (directory / PUBLIC_DUMP).write_bytes(b"PGDMP changed after the check")
    with pytest.raises(HostedBackupRefused, match="changed after the restore check"):
        seal(directory, recipient, out)

    (directory / PUBLIC_DUMP).unlink()
    with pytest.raises(HostedBackupRefused, match=r"changed after the restore check|missing"):
        seal(directory, recipient, out)
    assert not out.exists()


def test_the_seal_refuses_the_placeholder_before_it_touches_the_directory(tmp_path: Path) -> None:
    """A run before the operator makes the key fails loudly, in these words."""
    recipient = write(tmp_path / "recipient.txt", f"# note\n{RECIPIENT_PLACEHOLDER}\n")
    directory = checked_bundle(tmp_path)
    (directory / CHECKED).unlink()  # a directory the seal would refuse for its own reason
    out = tmp_path / "out.age"
    with pytest.raises(HostedBackupRefused) as refused:
        seal(directory, recipient, out)
    assert str(refused.value) == PLACEHOLDER_REFUSAL
    assert not out.exists()


def test_the_seal_refuses_to_replace_a_file(tmp_path: Path) -> None:
    recipient = write(tmp_path / "recipient.txt", SHAPED_LIKE_A_RECIPIENT + "\n")
    directory = checked_bundle(tmp_path)
    out = write(tmp_path / "out.age", "earlier")
    with pytest.raises(HostedBackupRefused, match="already exists"):
        seal(directory, recipient, out)
    assert out.read_text(encoding="utf-8") == "earlier"


def age_tools() -> tuple[str, str]:
    age, keygen = shutil.which("age"), shutil.which("age-keygen")
    if age is None or keygen is None:
        pytest.skip("age is not installed; the encryption tests need age and age-keygen")
    return age, keygen


def make_key(directory: Path, name: str) -> tuple[Path, str]:
    """A key pair `age-keygen` makes: the identity file and its public key."""
    _, keygen = age_tools()
    identity = directory / f"{name}.txt"
    done = subprocess.run([keygen, "-o", str(identity)], capture_output=True, check=False)
    assert done.returncode == 0
    public = next(
        line.split(": ", 1)[1]
        for line in identity.read_text(encoding="utf-8").splitlines()
        if line.startswith("# public key: ")
    )
    return identity, public


def test_the_sealed_file_opens_only_with_the_private_key(tmp_path: Path) -> None:
    """Dump and manifest are encrypted together to the recipient: the file
    holds none of their bytes, the key opens it into the three files, and
    another key does not."""
    age, _ = age_tools()
    identity, public = make_key(tmp_path, "operator")
    stranger, _ = make_key(tmp_path, "stranger")
    recipient = write(tmp_path / "recipient.txt", f"# comment\n{public}\n")
    directory = checked_bundle(tmp_path)
    plain = {name: (directory / name).read_bytes() for name in BUNDLE}
    out = tmp_path / "sealed.tar.age"

    seal(directory, recipient, out)

    sealed = out.read_bytes()
    assert sealed.startswith(b"age-encryption.org/")
    assert all(content not in sealed for content in plain.values())
    assert b"PGDMP" not in sealed and b"synthetic" not in sealed

    opened = subprocess.run(
        [age, "--decrypt", "-i", str(identity), str(out)], capture_output=True, check=False
    )
    assert opened.returncode == 0
    with tarfile.open(fileobj=io.BytesIO(opened.stdout)) as archive:
        members = archive.getmembers()
        assert sorted(member.name for member in members) == sorted(BUNDLE)
        for member in members:
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == plain[member.name]
            assert (member.uid, member.gid, member.mode) == (0, 0, 0o600)
    refused = subprocess.run(
        [age, "--decrypt", "-i", str(stranger), str(out)], capture_output=True, check=False
    )
    assert refused.returncode != 0


def test_a_failed_encryption_leaves_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `age` that writes part of its output and then fails: the seal fails
    with the exit status and removes what was written, so no partial file is
    left to upload."""
    fake = tmp_path / "bin" / "age"
    fake.parent.mkdir()
    fake.write_text(
        '#!/bin/sh\nout=""\nwhile [ $# -gt 0 ]; do\n'
        '  case "$1" in --output) out="$2"; shift;; esac\n'
        '  shift\ndone\ncat > /dev/null\necho partial > "$out"\nexit 2\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)

    def fake_which(_name: str) -> str:
        return str(fake)

    monkeypatch.setattr(hostedbackup.shutil, "which", fake_which)
    recipient = write(tmp_path / "recipient.txt", SHAPED_LIKE_A_RECIPIENT + "\n")
    directory = checked_bundle(tmp_path)
    out = tmp_path / "sealed.tar.age"
    with pytest.raises(HostedBackupRefused) as refused:
        seal(directory, recipient, out)
    assert str(refused.value) == "the encryption failed (exit 2)"
    assert not out.exists()


def test_a_recipient_age_rejects_fails_the_seal(tmp_path: Path) -> None:
    """The shape passes this code's check and the checksum fails `age`'s: the
    seal fails and leaves no file."""
    age_tools()
    recipient = write(tmp_path / "recipient.txt", SHAPED_LIKE_A_RECIPIENT + "\n")
    directory = checked_bundle(tmp_path)
    out = tmp_path / "sealed.tar.age"
    with pytest.raises(HostedBackupRefused, match="the encryption failed"):
        seal(directory, recipient, out)
    assert not out.exists()


def test_the_seal_command_exits_1_in_words_for_the_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    directory = checked_bundle(tmp_path)
    recipient = write(tmp_path / "recipient.txt", f"{RECIPIENT_PLACEHOLDER}\n")
    code = main(
        [
            "store",
            "backup-seal",
            "--dir",
            str(directory),
            "--recipient",
            str(recipient),
            "--out",
            str(tmp_path / "out.age"),
        ]
    )
    captured = capsys.readouterr()
    assert (code, captured.out) == (1, "")
    assert captured.err == f"error: {PLACEHOLDER_REFUSAL}\n"


# --- the commands' start ---

PARSER_PROBE = """
import sys
from harrier_cli.main import build_parser
parser = build_parser()
owner = "00000000-0000-4000-8000-000000000000"
for argv in (
    ["store", "backup-dump", "--out", "x"],
    ["store", "backup-check", "--dir", "x", "--into", "postgresql://h/d"],
    ["store", "backup-seal", "--dir", "x", "--recipient", "r", "--out", "o"],
    ["store", "backup-files", "--owner", owner],
):
    parser.parse_args(argv)
print("httpx" in sys.modules, "psycopg" in sys.modules)
"""


def test_the_backup_commands_import_no_httpx_or_driver_until_they_run() -> None:
    done = subprocess.run(
        [sys.executable, "-c", PARSER_PROBE], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"]


def test_a_dump_directory_is_private_to_the_operator(
    pg_client: None, dumped: Dumped, tmp_path: Path
) -> None:
    """The dump holds a person's whole search: the directory is 0700."""
    mode = stat.S_IMODE(dumped.directory.stat().st_mode)
    assert mode == 0o700


def test_the_public_list_leaves_out_the_schemas_own_lines_and_nothing_else(
    pg_client: None, dumped: Dumped
) -> None:
    """A Supabase project already has its `public` schema, with the grants its
    roles rely on, so a restore fills it and neither makes nor drops it. The
    list that says so removes the schema's own three lines and keeps every
    table, function and policy, and `docs/deploy.md` filters with the same
    pattern."""
    pg_restore = pg_tool("pg_restore", os.environ)
    full = subprocess.run(
        [pg_restore, "--list", str(dumped.directory / PUBLIC_DUMP)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    kept = hostedbackup.public_list(pg_restore, dumped.directory / PUBLIC_DUMP).splitlines()
    removed = [line for line in full if line not in kept]
    assert [line.split(" ", 3)[3].rsplit(" ", 1)[0] for line in removed] == [
        "SCHEMA - public",
        "COMMENT - SCHEMA public",
        "ACL - SCHEMA public",
    ]
    assert any(" TABLE public jobs " in line for line in kept)
    assert any(" FUNCTION public harrier_refuse" in line for line in kept)
    assert any(" POLICY public jobs owner_only" in line for line in kept)


POLICY_ROLES = re.compile(
    r"CREATE POLICY \S+ ON \S+ (?:AS \w+ )?(?:FOR \w+ )?TO ([\w, ]+?)(?: USING|;)"
)


def test_the_scaffold_makes_every_role_a_policy_of_the_dump_names(
    pg_client: None, dumped: Dumped
) -> None:
    """A database that never saw a migration has none of the roles the
    policies name. The scaffold's list covers every role the dump's policies
    are granted to, so a new policy for a new role fails here and not in a
    night's restore."""
    sql = subprocess.run(
        [
            pg_tool("pg_restore", os.environ),
            "--no-owner",
            "--no-privileges",
            "-f",
            "-",
            str(dumped.directory / PUBLIC_DUMP),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    named = {
        role.strip()
        for roles in POLICY_ROLES.findall(sql)
        for role in roles.split(",")
        if role.strip() != "PUBLIC"
    }
    assert "harrier_tenant" in named
    assert named <= set(hostedbackup.SCAFFOLD_ROLES), named - set(hostedbackup.SCAFFOLD_ROLES)


def test_the_scaffold_creates_an_absent_role_and_leaves_an_existing_one(
    pg_client: None,
) -> None:
    """Roles belong to the whole server: an absent one is made, a present one
    is left, and the `auth` schema is made once."""
    from harrier.connection import Connection, StoreDatabaseError

    absent = f"backup_probe_{uuid.uuid4().hex[:10]}"
    with fresh_database(shim=False) as empty:
        with closing(postgres_connect(empty)) as driver:
            conn = Connection.postgres(driver)  # pyright: ignore[reportArgumentType]
            hostedbackup.scaffold(conn, (absent, "anon"))
            found = conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ?", (absent,))
            assert found.fetchone() is not None
            with pytest.raises(StoreDatabaseError):
                hostedbackup.scaffold(conn, (absent,))  # the schema exists now
        with psycopg.connect(superuser_url(empty), autocommit=True) as admin:
            admin.execute(f"DROP ROLE {absent}".encode())
