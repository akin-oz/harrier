"""A local store's artifacts import into one hosted account (spec 146).

Each test builds an account on its own copy of the migrated Postgres template
(`postgres_template`, spec 126), fills it with `harrier store import-local`
from a synthetic local store, and drives `harrier store import-artifacts`
through `main` against the fake Storage server (`storage_support`, spec 107).
The files are synthetic and sit in the test's own `HARRIER_DATA_DIR`; no test
reads the real `data/`. Local job ids and hosted job ids differ on purpose,
since an import that kept an id in a name would otherwise pass. The tests
that need Postgres skip locally without a test server and fail in CI (spec
039).
"""

# Pyright strict cannot resolve starlette's TestClient request and response
# members, nor psycopg's row types, which is why the API tests carry these.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import pytest
from pg_support import open_owner_store
from resume_support import store_example
from storage_support import BUCKET, FakeStorage
from test_hosted_requests import Server, copied_server, run_login_for, serving
from test_import_local import hosted_query

from harrier import container, db
from harrier.container import ContainerState
from harrier.db import connect, data_dir, default_db_path
from harrier.filestore import CONTENT_TYPES, SIZE_LIMIT
from harrier.hostlease import LEASE_DIRNAME
from harrier.tracker import add_job
from harrier.tracker.importlocal import IMPORT_URL_VARIABLE
from harrier.tracks import default_scope
from harrier_cli.main import main

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)

PROJECT_HOST = "synthetic-project.supabase.test"
PROJECT_URL = f"https://{PROJECT_HOST}"
SECRET_KEY = "sb_secret_synthetic_value_0123456789"
URL_NAME = "HARRIER_SUPABASE_URL"
KEY_NAME = "HARRIER_SUPABASE_SECRET_KEY"

# The synthetic candidate in config/resume-facts.example.json, as the resume
# slug writes the name, and the one company and role the files are for.
CANDIDATE_SLUG = "deniz-ornek"
PAIR = "example-co-frontend-developer"
RESUME_STEM = f"{CANDIDATE_SLUG}-{PAIR}"
ROLE_TITLE = "Frontend Developer"
OTHER_TITLE = "Backend Developer"

# Every name an output may not carry: the candidate, the company and the role.
IDENTITY_FRAGMENTS = ("deniz", "ornek", "Örnek", "example-co", "Example Co", "frontend")

PDF = b"%PDF-1.4 synthetic\n"

# family -> file name -> bytes: the six artifacts of one job.
GOOD_FILES: dict[str, dict[str, bytes]] = {
    "resumes": {
        f"{RESUME_STEM}.pdf": PDF + b"resume\n",
        f"{RESUME_STEM}.md": "# Resume ü\n".encode(),
        f"{RESUME_STEM}.metadata.json": b'{"synthetic": true}',
    },
    "cover-letters": {
        f"{PAIR}.pdf": PDF + b"letter\n",
        f"{PAIR}.md": "# Letter ü\n".encode(),
    },
    "answers": {f"{PAIR}.md": "# Answers ü\n".encode()},
}


@pytest.fixture(name="server")
def server_fixture(postgres_template: str) -> Iterator[Server]:
    yield from copied_server(postgres_template)


@dataclass
class Account:
    """A hosted account: its owner and the run login's URL. `jobs` is the
    hosted id of each job by role title, once its rows are imported."""

    server: Server
    login: str
    owner: str
    jobs: dict[str, int] = field(default_factory=dict[str, int])


@pytest.fixture()
def account(server: Server, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Account]:
    """An empty account, and the environment an operator's shell holds: the
    Storage project and its secret key, a working directory with no `.env`,
    and a local data directory no container holds."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(URL_NAME, PROJECT_URL)
    monkeypatch.setenv(KEY_NAME, SECRET_KEY)
    monkeypatch.setattr(db, "live_data_root", lambda: default_db_path().parent)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    with run_login_for(server.url) as login:
        monkeypatch.setenv(IMPORT_URL_VARIABLE, login)
        yield Account(server, login, server.owner())
    db.release_host_lease()


def build_local_store() -> None:
    """The local store: the resume documents and two jobs on the default
    track, with local ids 1 and 2."""
    conn = connect(default_db_path())
    try:
        store_example(conn)
        for title in (ROLE_TITLE, OTHER_TITLE):
            add_job(
                conn,
                {
                    "company": "Example Co",
                    "title": title,
                    "url": f"https://example.test/{uuid.uuid4().hex}",
                    "source": "manual",
                },
                scope=default_scope(conn),
            )
    finally:
        conn.close()
    db.release_host_lease()


def import_rows(
    account: Account, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`harrier store import-local`, after other owners' rows have taken the
    first hosted ids, so no hosted id equals its local one."""
    for number in range(3):
        stranger = open_owner_store(account.server.url, account.server.owner())
        try:
            stranger.execute(
                "INSERT INTO jobs (company, url) VALUES (?, ?)",
                [f"Filler {number}", f"https://example.test/filler-{number}"],
            )
        finally:
            stranger.close()
    build_local_store()
    monkeypatch.setenv(IMPORT_URL_VARIABLE, account.login)
    assert main(["store", "import-local", "--owner", account.owner]) == 0
    capsys.readouterr()
    rows = hosted_query(
        account.server, "SELECT id, title FROM jobs WHERE owner_id = %s", (account.owner,)
    )
    account.jobs = {str(row["title"]): int(row["id"]) for row in rows}
    assert sorted(account.jobs) == sorted((ROLE_TITLE, OTHER_TITLE))
    assert set(account.jobs.values()).isdisjoint({1, 2}), "hosted ids differ from local ids"


@pytest.fixture()
def imported(
    account: Account, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Account:
    import_rows(account, capsys, monkeypatch)
    return account


# --- the local files ---


def put_local(family: str, name: str, data: bytes) -> Path:
    path = data_dir() / family / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def put_good_files() -> dict[str, bytes]:
    """The six artifacts, by key."""
    keys: dict[str, bytes] = {}
    for family, named in GOOD_FILES.items():
        for name, data in named.items():
            put_local(family, name, data)
            keys[f"{family}/{name}"] = data
    return keys


def object_name(owner: str, key: str) -> str:
    """Where Storage holds `key`: spec 107's object name for a hashed
    family, built here from the spec's words and not from the store."""
    family, _, last = key.rpartition("/")
    return (
        f"{owner}/{family}/{hashlib.sha256(key.encode()).hexdigest()}{PurePosixPath(last).suffix}"
    )


@dataclass
class Result:
    code: int
    out: str
    err: str


def run(capsys: pytest.CaptureFixture[str], account: Account) -> Result:
    code = main(["store", "import-artifacts", "--owner", account.owner])
    out, err = capsys.readouterr()
    return Result(code, out, err)


def uploads(storage: FakeStorage) -> list[str]:
    return [
        name
        for operation, name in map(storage.operation, storage.requests)
        if operation == "upload"
    ]


# --- what the command read, to show what it did not ---


@dataclass
class Reads:
    """Every local path the command listed, stat-ed or read."""

    paths: list[Path] = field(default_factory=list[Path])

    def under(self, root: Path) -> list[Path]:
        """The paths strictly inside `root`, less the host's own bookkeeping that every
        host command does before it runs: the lease directory (spec 075) and
        the database file's name, which the ownership check is asked about
        and never opens."""
        machinery = {LEASE_DIRNAME, "tracker.db"}
        return [
            path
            for path in self.paths
            if root in path.parents and not machinery & set(path.relative_to(root).parts)
        ]


@pytest.fixture()
def reads(monkeypatch: pytest.MonkeyPatch) -> Reads:
    seen = Reads()
    for name in ("iterdir", "lstat", "read_bytes", "stat", "open", "read_text"):
        original: Callable[..., Any] = getattr(Path, name)

        def recording(
            self: Path, *args: Any, _original: Callable[..., Any] = original, **kwargs: Any
        ) -> Any:
            seen.paths.append(self)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, recording)
    return seen


# --- the nine behaviours of the spec's acceptance criteria ---


def test_import_artifacts_copies_the_three_families(
    imported: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = put_good_files()

    result = run(capsys, imported)

    assert result.code == 0, result.err
    assert result.out.splitlines() == [
        "resumes: 3 copied, 0 already there",
        "cover-letters: 2 copied, 0 already there",
        "answers: 1 copied, 0 already there",
        "not copied, over the size limit: 0",
        "not copied, other type or name: 0",
        "not copied, differs from the hosted object: 0",
        "attached: 6",
        "unattached: 0",
        "imported",
    ]
    assert result.err == ""
    assert storage.names() == sorted(object_name(imported.owner, key) for key in keys)
    for key, data in keys.items():
        found = storage.objects[object_name(imported.owner, key)]
        assert found.data == data
        assert found.content_type == CONTENT_TYPES[PurePosixPath(key).suffix]

    # The artifact routes find them, as the browser does, and serve the bytes.
    with serving(monkeypatch, imported.server.login_role().url) as hosted:
        client = hosted.client()
        token = hosted.headers(imported.owner)
        job = imported.jobs[ROLE_TITLE]
        listed = client.get(f"/apply/{job}/artifacts", headers=token)
        assert listed.status_code == 200, listed.text
        rows = {row["kind"]: row for row in listed.json()}
        assert {kind for kind, row in rows.items() if row["exists"]} == {
            "resume-pdf",
            "resume-markdown",
            "cover-letter-pdf",
            "cover-letter-markdown",
            "answers",
        }
        expected = {
            "resume-pdf": (f"resumes/{RESUME_STEM}.pdf", "application/pdf"),
            "resume-markdown": (f"resumes/{RESUME_STEM}.md", "text/markdown; charset=utf-8"),
            "cover-letter-pdf": (f"cover-letters/{PAIR}.pdf", "application/pdf"),
            "cover-letter-markdown": (f"cover-letters/{PAIR}.md", "text/markdown; charset=utf-8"),
            "answers": (f"answers/{PAIR}.md", "text/markdown; charset=utf-8"),
        }
        for kind, (key, media_type) in expected.items():
            served = client.get(f"/apply/{job}/artifacts/{kind}", headers=token)
            assert served.status_code == 200, (kind, served.text)
            assert served.content == keys[key]
            assert served.headers["content-type"] == media_type


def test_import_artifacts_stay_attached_to_the_hosted_job(
    imported: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put_good_files()
    # A file no job of the account names: copied, and counted unattached.
    put_local("cover-letters", "example-co-research-fellow.md", b"# For no job\n")

    result = run(capsys, imported)

    assert result.code == 0, result.err
    assert "attached: 6" in result.out.splitlines()
    assert "unattached: 1" in result.out.splitlines()
    with serving(monkeypatch, imported.server.login_role().url) as hosted:
        client = hosted.client()
        token = hosted.headers(imported.owner)
        holder = imported.jobs[ROLE_TITLE]
        other = imported.jobs[OTHER_TITLE]
        assert holder not in (1, 2) and other not in (1, 2)
        mine = {
            row["kind"]: row["exists"]
            for row in client.get(f"/apply/{holder}/artifacts", headers=token).json()
        }
        assert mine["resume-pdf"] and mine["cover-letter-markdown"] and mine["answers"]
        others = {
            row["kind"]: row["exists"]
            for row in client.get(f"/apply/{other}/artifacts", headers=token).json()
        }
        assert not any(others.values())
        assert client.get(f"/apply/{other}/artifacts/answers", headers=token).status_code == 404
    # No file name carries a job id, so nothing was renamed or rewritten.
    names = [name for named in GOOD_FILES.values() for name in named]
    assert not any(str(job) in name for name in names for job in imported.jobs.values())
    assert len(storage.objects) == 7


def test_import_artifacts_is_idempotent(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_good_files()
    first = run(capsys, imported)
    assert first.code == 0, first.err
    before = len(storage.requests)
    stored = {name: found.data for name, found in storage.objects.items()}

    second = run(capsys, imported)

    assert second.code == 0, second.err
    assert second.out.splitlines() == [
        "resumes: 0 copied, 3 already there",
        "cover-letters: 0 copied, 2 already there",
        "answers: 0 copied, 1 already there",
        "not copied, over the size limit: 0",
        "not copied, other type or name: 0",
        "not copied, differs from the hosted object: 0",
        "attached: 6",
        "unattached: 0",
        "imported",
    ]
    assert [op for op in storage.operations()[before:] if op == "upload"] == []
    assert {name: found.data for name, found in storage.objects.items()} == stored


def test_import_artifacts_never_overwrites_a_different_object(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = put_good_files()
    newer = f"resumes/{RESUME_STEM}.pdf"
    held = object_name(imported.owner, newer)
    storage.put(held, b"%PDF-1.4 a newer hosted run\n", "application/pdf")
    before = storage.objects[held]

    result = run(capsys, imported)

    assert result.code == 1
    assert result.out.splitlines() == [
        "resumes: 2 copied, 0 already there",
        "cover-letters: 2 copied, 0 already there",
        "answers: 1 copied, 0 already there",
        "not copied, over the size limit: 0",
        "not copied, other type or name: 0",
        "not copied, differs from the hosted object: 1",
        "attached: 5",
        "unattached: 0",
        "incomplete",
    ]
    assert storage.objects[held] is before
    assert storage.objects[held].data == b"%PDF-1.4 a newer hosted run\n"
    assert held not in uploads(storage)
    # Everything else went up whole.
    for key, data in keys.items():
        if key != newer:
            assert storage.objects[object_name(imported.owner, key)].data == data


def test_import_artifacts_resumes_after_an_interruption(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = put_good_files()
    seen: list[str] = []

    def lose_the_fourth(request: httpx.Request) -> None:
        seen.append(request.url.path)
        if len(seen) == 4:
            storage.raising["upload"] = httpx.ConnectError("synthetic")

    storage.before["upload"] = lose_the_fourth

    stopped = run(capsys, imported)

    # Three uploads finished, each whole, and the fourth never arrived.
    assert stopped.code == 1
    assert stopped.out.splitlines() == ["resumes: 3 copied, 0 already there"]
    assert stopped.err.count("\n") == 1
    assert stopped.err.startswith("error: ")
    assert "cover-letters" in stopped.err
    assert len(storage.objects) == 3
    for name, found in storage.objects.items():
        assert found.data in keys.values()
        assert name.startswith(f"{imported.owner}/")
    storage.raising.clear()
    storage.before.clear()
    stored = set(storage.names())
    before = len(storage.requests)

    resumed = run(capsys, imported)

    assert resumed.code == 0, resumed.err
    assert resumed.out.splitlines()[:3] == [
        "resumes: 0 copied, 3 already there",
        "cover-letters: 2 copied, 0 already there",
        "answers: 1 copied, 0 already there",
    ]
    sent = [
        name
        for operation, name in map(storage.operation, storage.requests[before:])
        if operation == "upload"
    ]
    assert len(sent) == 3
    assert stored.isdisjoint(sent)
    assert storage.names() == sorted(object_name(imported.owner, key) for key in keys)


def test_import_artifacts_skips_what_storage_would_refuse(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_local("answers", f"{PAIR}.md", b"# Answers\n")
    # Over the seam's limit, by one byte.
    put_local("resumes", f"{RESUME_STEM}.pdf", b"\0" * (SIZE_LIMIT + 1))
    # An extension outside the bucket's five.
    put_local("resumes", f"{RESUME_STEM}.txt", b"synthetic")
    # A name outside the key grammar: a space, and a letter outside ASCII.
    put_local("answers", "two words.md", b"synthetic")
    put_local("answers", "ünicode.md", b"synthetic")
    # A file of no bytes.
    put_local("cover-letters", f"{PAIR}.md", b"")

    result = run(capsys, imported)

    assert result.code == 1
    assert result.out.splitlines() == [
        "resumes: 0 copied, 0 already there",
        "cover-letters: 0 copied, 0 already there",
        "answers: 1 copied, 0 already there",
        "not copied, over the size limit: 1",
        "not copied, other type or name: 4",
        "not copied, differs from the hosted object: 0",
        "attached: 1",
        "unattached: 0",
        "incomplete",
    ]
    # The skipped files cost no request, and the sixth was sent.
    assert uploads(storage) == [object_name(imported.owner, f"answers/{PAIR}.md")]
    assert [op for op in storage.operations() if op in ("download",)] == []
    assert len(storage.requests) == 3  # stat, upload, stat


def test_import_artifacts_leaves_other_directories_alone(
    imported: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    reads: Reads,
    tmp_path: Path,
) -> None:
    put_good_files()
    root = data_dir()
    # A dot file (the PDF gate's temporary name), a subdirectory and a
    # symbolic link to a file elsewhere, inside a directory the command does
    # read. A link is no regular file, and is never followed.
    put_local("resumes", f".tmp-{RESUME_STEM}.pdf", PDF)
    put_local("resumes", f"nested/{RESUME_STEM}.pdf", PDF)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_bytes(b"# A file outside the data directory\n")
    (root / "resumes" / f"{RESUME_STEM}.evaluation.md").symlink_to(elsewhere)
    # The other `data/` entries, with names that would fit a job.
    for other in ("logs", "llm-logs", "cache", "reports", "resumes-corrected", "backups"):
        put_local(other, f"{PAIR}.md", b"# Not an artifact\n")
    put_local(".", "tracker.db.corrupt-20260918", b"synthetic")
    reads.paths.clear()

    result = run(capsys, imported)

    assert result.code == 0, result.err
    assert "attached: 6" in result.out.splitlines()
    assert "not copied, other type or name: 0" in result.out.splitlines()
    assert len(storage.objects) == 6
    inside = [root / family for family in ("resumes", "cover-letters", "answers")]
    for path in reads.under(root):
        # The three directories, and the files directly in them: no other
        # directory, no subdirectory's content, no dot file.
        assert path in inside or path.parent in inside, path
        assert not path.name.startswith(".tmp-"), path


def test_import_artifacts_needs_the_rows_first(
    account: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A track and no job, before any directory exists: the sentence says what
    # to run first, and the missing directories are not what refuses.
    empty = run(capsys, account)
    assert empty.code == 1
    assert "run harrier store import-local first" in empty.err
    assert empty.out == ""
    assert storage.requests == []
    put_good_files()
    assert "run harrier store import-local first" in run(capsys, account).err

    # A user id that is no account sees no track.
    stranger = Account(account.server, account.login, str(uuid.uuid4()))
    unknown = run(capsys, stranger)
    assert unknown.code == 1
    assert "sees no track" in unknown.err
    assert unknown.out == ""
    assert storage.requests == []

    # Files that fit no job of an account that has rows.
    import_rows(account, capsys, monkeypatch)
    for family, named in GOOD_FILES.items():
        for name in named:
            (data_dir() / family / name).unlink()
    put_local("resumes", "someone-else-another-role.pdf", PDF)
    put_local("answers", "another-company-another-role.md", b"# Answers\n")
    misfit = run(capsys, account)
    assert misfit.code == 1
    assert "fits a job" in misfit.err
    assert misfit.out == ""
    assert storage.requests == []

    # One file that fits is enough: the rest are copied as unattached.
    put_local("answers", f"{PAIR}.md", b"# Answers\n")
    fits = run(capsys, account)
    assert fits.code == 0, fits.err
    assert "attached: 1" in fits.out.splitlines()
    assert "unattached: 2" in fits.out.splitlines()


def test_import_artifacts_writes_only_under_the_owners_prefix(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    keys = put_good_files()
    # Another owner's object under the same key, which must stay as it is.
    neighbour = str(uuid.uuid4())
    taken = object_name(neighbour, f"answers/{PAIR}.md")
    storage.put(taken, b"# The neighbour's answers\n", "text/markdown")
    before = storage.objects[taken]
    count = len(storage.requests)

    result = run(capsys, imported)

    assert result.code == 0, result.err
    mine = {object_name(imported.owner, key) for key in keys}
    assert set(storage.names()) == mine | {taken}
    assert storage.objects[taken] is before
    for request in storage.requests[count:]:
        path = request.url.path
        assert f"/{BUCKET}/{imported.owner}/" in path, path
        assert neighbour not in path
        # The operator's key and nothing of a user's: no bearer token.
        assert request.headers["apikey"] == SECRET_KEY
        assert "authorization" not in request.headers
    # No object name holds a file name, a slug or the candidate.
    for name in storage.names():
        for piece in (CANDIDATE_SLUG, "example-co", "frontend", "metadata"):
            assert piece not in name


def test_import_artifacts_prints_no_file_name(
    imported: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    put_good_files()
    put_local("answers", "two words.md", b"synthetic")
    password = imported.login.split("@", 1)[0].rsplit(":", 1)[1]
    host = imported.login.split("@", 1)[1].split("/", 1)[0]
    secrets = (
        imported.owner,
        PROJECT_HOST,
        host,
        password,
        SECRET_KEY,
        "two words",
        *IDENTITY_FRAGMENTS,
        *(name for named in GOOD_FILES.values() for name in named),
    )

    outs: list[str] = []
    errs: list[str] = []
    # An incomplete run, a finished one, a stop and a denial.
    for scenario in ("incomplete", "complete", "unavailable", "denied"):
        if scenario == "complete":
            (data_dir() / "answers" / "two words.md").unlink()
        if scenario == "unavailable":
            storage.objects.clear()
            storage.raising["info"] = httpx.ConnectError(f"{PROJECT_URL}/{imported.owner}")
        if scenario == "denied":
            storage.raising.clear()
            storage.failures["info"] = [403]
        result = run(capsys, imported)
        outs.append(result.out)
        errs.append(result.err)
    assert errs[0] == errs[1] == ""
    assert all(err.startswith("error: ") and err.count("\n") == 1 for err in errs[2:])
    assert outs[0].splitlines()[-1] == "incomplete"
    assert outs[1].splitlines()[-1] == "imported"
    for text in (*outs, *errs):
        for name in secrets:
            assert name not in text, name
    for name in secrets:
        assert name not in caplog.text, name
    # No log file is set up for the run (spec 144, amendment 3).
    assert not (data_dir() / "logs").exists()


def test_import_artifacts_never_writes_the_local_files(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_good_files()
    put_local("answers", "two words.md", b"synthetic")
    put_local("resumes", f"nested/{RESUME_STEM}.pdf", PDF)

    families = [data_dir() / family for family in ("resumes", "cover-letters", "answers")]

    def snapshot() -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        for family in families:
            for path in sorted(family.rglob("*")):
                if path.is_file():
                    found[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns)
        return found

    def directories() -> list[str]:
        return sorted(str(p) for f in families for p in (f, *f.rglob("*")) if p.is_dir())

    before = snapshot()
    shape = directories()
    assert len(before) == 8

    first = run(capsys, imported)
    second = run(capsys, imported)

    # The file with a space in its name stays uncopied, and untouched.
    assert first.code == 1 and second.code == 1
    assert snapshot() == before
    assert directories() == shape


@pytest.mark.parametrize(
    "case",
    [
        "container",
        "no database url",
        "database url only in dotenv",
        "migrator url",
        "no storage url",
        "malformed storage url",
        "no secret key",
        "secret key only in dotenv",
        "secret key in dotenv and shell",
    ],
)
def test_import_artifacts_refuses_before_writing(
    case: str,
    imported: Account,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reads: Reads,
) -> None:
    put_good_files()
    objects = len(storage.objects)
    asked = len(storage.requests)
    reads.paths.clear()
    if case == "container":
        monkeypatch.setattr(
            container,
            "detect",
            lambda timeout=0.0: ContainerState(
                engine=container.REACHABLE, running=True, data_source=default_db_path().parent
            ),
        )
    elif case == "no database url":
        monkeypatch.delenv(IMPORT_URL_VARIABLE)
    elif case == "database url only in dotenv":
        monkeypatch.delenv(IMPORT_URL_VARIABLE)
        (tmp_path / ".env").write_text(f"{IMPORT_URL_VARIABLE}={imported.login}\n")
    elif case == "migrator url":
        monkeypatch.setenv(IMPORT_URL_VARIABLE, imported.server.url)
    elif case == "no storage url":
        monkeypatch.delenv(URL_NAME)
    elif case == "malformed storage url":
        monkeypatch.setenv(URL_NAME, f"http://{PROJECT_HOST}/path")
    elif case == "no secret key":
        monkeypatch.delenv(KEY_NAME)
    elif case == "secret key only in dotenv":
        monkeypatch.delenv(KEY_NAME)
        (tmp_path / ".env").write_text(f"{KEY_NAME}={SECRET_KEY}\n")
    else:
        (tmp_path / ".env").write_text(f"{KEY_NAME}={SECRET_KEY}\n")

    result = run(capsys, imported)

    assert result.code == 1, result.out
    assert result.out == ""
    assert result.err.count("\n") == 1 and result.err.startswith("error: ")
    for secret in (SECRET_KEY, imported.owner, PROJECT_HOST):
        assert secret not in result.err
    assert len(storage.requests) == asked and len(storage.objects) == objects
    assert reads.under(data_dir()) == [], "nothing was read from the data directory"

    # A malformed `--owner` is a usage error, whatever else is wrong.
    with pytest.raises(SystemExit) as raised:
        main(["store", "import-artifacts", "--owner", "a-person@example.test"])
    assert raised.value.code == 2
    assert "example.test" not in capsys.readouterr().err


def test_import_artifacts_stops_when_storage_denies_it(
    imported: Account, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_good_files()

    # Refused on the first look at an object.
    storage.failures["info"] = [401]
    looked = run(capsys, imported)
    assert looked.code == 1
    assert uploads(storage) == []

    # Refused on the upload itself, after a look that found nothing.
    storage.failures["upload"] = [403]
    sent = run(capsys, imported)
    assert sent.code == 1
    assert len(storage.objects) == 0

    for result in (looked, sent):
        assert result.err.count("\n") == 1
        assert "refused the credential" in result.err
        assert "resumes" in result.err
        for secret in (SECRET_KEY, imported.owner, PROJECT_HOST, BUCKET, RESUME_STEM):
            assert secret not in result.err
        assert result.out == ""


# --- the command's start ---

PARSER_PROBE = """
import sys
from harrier_cli.main import build_parser
args = build_parser().parse_args(
    ["store", "import-artifacts", "--owner", "00000000-0000-4000-8000-000000000000"]
)
print(args.store_command, "httpx" in sys.modules)
"""


def test_the_command_imports_no_httpx_until_it_runs() -> None:
    done = subprocess.run(
        [sys.executable, "-c", PARSER_PROBE], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["import-artifacts", "False"]
