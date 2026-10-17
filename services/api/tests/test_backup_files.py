"""`harrier store backup-files` copies one hosted account's Storage objects to
the operator's disk (spec 153).

Each test drives the command through `main` against the fake Storage server
(`storage_support`, spec 107; the autouse `storage` fixture routes every
hosted file store to it, so no test reaches a network). The objects are
synthetic and sit under a synthetic owner; the copy lands in the test's own
`HARRIER_DATA_DIR`. No test reads the real `data/`. The command needs no
database: it reads Storage and writes files.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from storage_support import BUCKET, FakeStorage

from harrier import artifactimport, container, db, filesbackup
from harrier.container import ContainerState
from harrier.db import data_dir, default_db_path
from harrier.filesbackup import backup_files
from harrier.filestore import InvalidFileKey, SupabaseFileStore, hashed_name
from harrier_cli.main import main

UNREACHABLE = ContainerState(engine=container.UNREACHABLE)

PROJECT_HOST = "synthetic-project.supabase.test"
PROJECT_URL = f"https://{PROJECT_HOST}"
SECRET_KEY = "sb_secret_synthetic_value_0123456789"
URL_NAME = "HARRIER_SUPABASE_URL"
KEY_NAME = "HARRIER_SUPABASE_SECRET_KEY"

OWNER = "11111111-1111-4111-8111-111111111111"
OTHER_OWNER = "22222222-2222-4222-8222-222222222222"

# key -> bytes: what the harrier writers store for one job, and a run's export
# in a listed family two folders down.
FILES: dict[str, bytes] = {
    "resumes/example-co-role.pdf": b"%PDF-1.4 synthetic resume\n",
    "cover-letters/example-co-role.md": "# Letter ü\n".encode(),
    "answers/example-co-role.md": b"# Answers\n",
    "scoring/exports/2026-10-16.json": b'{"synthetic": true}',
}


def object_name(key: str) -> str:
    """The name below the owner's prefix, as the seam stores it."""
    return hashed_name(key)


@pytest.fixture()
def operator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """The environment an operator's shell holds: the Storage project and its
    secret key, a working directory with no `.env`, and a data directory no
    container holds."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(URL_NAME, PROJECT_URL)
    monkeypatch.setenv(KEY_NAME, SECRET_KEY)
    monkeypatch.setattr(db, "live_data_root", lambda: default_db_path().parent)
    monkeypatch.setattr(container, "detect", lambda timeout=0.0: UNREACHABLE)
    monkeypatch.setattr(container, "running_in", lambda: "host")
    yield
    db.release_host_lease()


class Clock:
    """The date the command believes it is, in UTC."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, day: str = "2026-10-17") -> None:
        self.day = day

        clock = self

        class Fixed(datetime):
            @classmethod
            def now(cls, tz: object = None) -> Fixed:  # pyright: ignore[reportIncompatibleMethodOverride]
                return cls.fromisoformat(f"{clock.day}T03:30:00+00:00")

        monkeypatch.setattr(filesbackup, "datetime", Fixed)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    return Clock(monkeypatch)


def put_files(storage: FakeStorage, owner: str = OWNER) -> None:
    for key, data in FILES.items():
        storage.put(f"{owner}/{object_name(key)}", data)


def day_directory(day: str) -> Path:
    return data_dir() / "hosted" / "backups" / "files" / day


def written(day: str) -> dict[str, bytes]:
    """Every file under a day's directory, by name below it."""
    root = day_directory(day)
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def run(owner: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["store", "backup-files", "--owner", owner])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def downloads_since(storage: FakeStorage, start: int) -> int:
    return storage.operations()[start:].count("download")


def test_backup_files_copies_every_object_and_names_none(
    operator: None,
    clock: Clock,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each listed object is written under the dated directory by its name;
    the next day's run links an unchanged object from the newest earlier copy
    and downloads only what changed; no line names a key, a name, the owner or
    the host; and a secret key defined in `.env` is refused before any
    request (spec 153, acceptance)."""
    put_files(storage)
    storage.put(f"{OTHER_OWNER}/{object_name('resumes/theirs.pdf')}", b"%PDF-1.4 theirs\n")
    names = {object_name(key): data for key, data in FILES.items()}

    # --- the first day: everything is downloaded ---
    code, out, err = run(OWNER, capsys)

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "objects: 4",
        "downloaded: 4",
        "linked from an earlier copy: 0",
        "already there: 0",
        "not written: 0",
        "backed up under data/hosted/backups/files",
    ]
    assert written("2026-10-17") == names
    assert not any("theirs" in name for name in written("2026-10-17"))
    root = day_directory("2026-10-17")
    private = [root.parent.parent, root.parent, root, *(p for p in root.rglob("*") if p.is_dir())]
    assert len(private) >= 6
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in private)
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600 for path in root.rglob("*") if path.is_file()
    )
    # The listing is kept beside the day, not inside it.
    assert (root.parent / "2026-10-17.index.json").is_file()
    assert not (root / "2026-10-17.index.json").exists()
    shown = out + err
    for secret in (
        OWNER,
        PROJECT_HOST,
        SECRET_KEY,
        BUCKET,
        "resumes",
        "scoring",
        "example-co",
        *names,
    ):
        assert secret not in shown

    # --- the next day: one object changed, three are linked, one is downloaded ---
    changed = f"{OWNER}/{object_name('answers/example-co-role.md')}"
    storage.put(changed, b"# Answers, revised\n")
    clock.day = "2026-10-18"
    before = len(storage.requests)

    code, out, err = run(OWNER, capsys)

    assert (code, err) == (0, "")
    assert out.splitlines()[1:5] == [
        "downloaded: 1",
        "linked from an earlier copy: 3",
        "already there: 0",
        "not written: 0",
    ]
    assert downloads_since(storage, before) == 1
    second = written("2026-10-18")
    assert second == {**names, object_name("answers/example-co-role.md"): b"# Answers, revised\n"}
    for name in names:
        first_file = day_directory("2026-10-17") / name
        next_file = day_directory("2026-10-18") / name
        same_file = first_file.stat().st_ino == next_file.stat().st_ino
        assert same_file == (name != object_name("answers/example-co-role.md")), name
    # The earlier day is whole as it was.
    assert written("2026-10-17") == names

    # --- a secret key in `.env` is refused before any request ---
    (tmp_path / ".env").write_text(f"{KEY_NAME}=sb_secret_other_value\n", encoding="utf-8")
    clock.day = "2026-10-19"
    before = len(storage.requests)

    code, out, err = run(OWNER, capsys)

    assert (code, out) == (1, "")
    assert err.startswith(f"error: {KEY_NAME} is defined in .env;")
    assert err.count("\n") == 1
    assert len(storage.requests) == before
    assert written("2026-10-19") == {}
    assert not os.path.exists(day_directory("2026-10-19"))
    for secret in (SECRET_KEY, "sb_secret_other_value", OWNER, PROJECT_HOST):
        assert secret not in err


def test_backup_files_links_a_changed_listing_with_equal_bytes(
    operator: None, clock: Clock, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """An upload of the same bytes changes the listing's version. The object
    is downloaded to find that out, and then linked, not stored twice."""
    put_files(storage)
    assert run(OWNER, capsys)[0] == 0
    # The same bytes again: the object's update time moves, its content does not.
    storage.put(f"{OWNER}/{object_name('answers/example-co-role.md')}", b"# Answers\n")
    clock.day = "2026-10-18"
    before = len(storage.requests)

    code, out, _ = run(OWNER, capsys)

    assert code == 0
    assert downloads_since(storage, before) == 1
    assert out.splitlines()[1:3] == ["downloaded: 0", "linked from an earlier copy: 4"]
    name = object_name("answers/example-co-role.md")
    assert (day_directory("2026-10-17") / name).stat().st_ino == (
        day_directory("2026-10-18") / name
    ).stat().st_ino


def test_backup_files_run_twice_on_one_day_downloads_nothing_new(
    operator: None, clock: Clock, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_files(storage)
    assert run(OWNER, capsys)[0] == 0
    before = len(storage.requests)

    code, out, _ = run(OWNER, capsys)

    assert code == 0
    assert downloads_since(storage, before) == 0
    assert out.splitlines()[1:5] == [
        "downloaded: 0",
        "linked from an earlier copy: 0",
        "already there: 4",
        "not written: 0",
    ]


def test_backup_files_waits_out_a_rate_limit(
    operator: None,
    clock: Clock,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage answered 429. The listing and a download each wait, as spec
    146's import does, and the run ends as an unlimited one does."""
    waits: list[float] = []
    monkeypatch.setattr(artifactimport, "_sleep", waits.append)
    put_files(storage)
    storage.failures["list"] = [429, 429]
    storage.failures["download"] = [429]

    code, out, err = run(OWNER, capsys)

    assert (code, err) == (0, "")
    assert waits == [2, 4, 2]
    assert out.count("Storage asked the backup to slow down; waiting") == 3
    assert out.splitlines()[-1] == "backed up under data/hosted/backups/files"
    assert len(written("2026-10-17")) == 4


def test_backup_files_stops_when_the_rate_limit_outlasts_the_waits(
    operator: None,
    clock: Clock,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(artifactimport, "_sleep", waits.append)
    put_files(storage)
    storage.failures["list"] = [429] * (len(artifactimport.RETRY_WAITS) + 1)

    code, _, err = run(OWNER, capsys)

    assert code == 1
    # Spec 146's waits, which this command shares: changing them is that spec's.
    assert waits == [2, 4, 8, 16, 32, 60]
    assert err == "error: list: Storage answered 429\n"
    assert written("2026-10-17") == {}


def test_backup_files_stops_naming_the_step_and_a_rerun_resumes(
    operator: None, clock: Clock, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """Storage refuses the credential at the second download: the run stops
    with the step named, keeps what it wrote, and the next run downloads the
    rest only."""
    put_files(storage)
    seen: list[int] = []

    def refuse_the_second(_request: httpx.Request) -> None:
        seen.append(1)
        if len(seen) == 2:
            storage.failures["download"] = [403]

    storage.before["download"] = refuse_the_second

    code, out, err = run(OWNER, capsys)

    assert code == 1
    assert out.splitlines() == ["objects: 4"]
    assert err == (
        "error: download: Storage refused the credential; the secret key or the project is wrong\n"
    )
    assert len(written("2026-10-17")) == 1
    for secret in (SECRET_KEY, OWNER, PROJECT_HOST, BUCKET):
        assert secret not in err
    storage.before.clear()
    before = len(storage.requests)

    code, out, err = run(OWNER, capsys)

    assert (code, err) == (0, "")
    assert downloads_since(storage, before) == 3
    assert out.splitlines()[1:5] == [
        "downloaded: 3",
        "linked from an earlier copy: 0",
        "already there: 1",
        "not written: 0",
    ]
    assert len(written("2026-10-17")) == 4


def test_backup_files_finishes_the_rest_and_ends_incomplete_for_an_object_it_cannot_write(
    operator: None, clock: Clock, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    """A name outside the key grammar cannot become a path safely. It is
    counted, never named, and the run exits 1 after the other objects."""
    put_files(storage)
    storage.put(f"{OWNER}/resumes/has a space.pdf", b"%PDF-1.4 odd\n")
    storage.put(f"{OWNER}/resumes/..%2Fescape.pdf", b"%PDF-1.4 odd\n")

    code, out, err = run(OWNER, capsys)

    assert (code, err) == (1, "")
    assert out.splitlines()[0:5] == [
        "objects: 6",
        "downloaded: 4",
        "linked from an earlier copy: 0",
        "already there: 0",
        "not written: 2",
    ]
    assert out.splitlines()[-1] == "incomplete"
    assert len(written("2026-10-17")) == 4
    assert "space" not in out and "escape" not in out


def test_backup_files_counts_an_object_that_vanishes_or_is_cut_short(
    operator: None, clock: Clock, storage: FakeStorage, capsys: pytest.CaptureFixture[str]
) -> None:
    put_files(storage)
    gone = f"{OWNER}/{object_name('answers/example-co-role.md')}"
    cut = f"{OWNER}/{object_name('resumes/example-co-role.pdf')}"
    calls: list[str] = []

    def damage(request: httpx.Request) -> None:
        calls.append(request.url.path)
        if len(calls) == 1:
            del storage.objects[gone]
            storage.objects[cut].data = storage.objects[cut].data[:4]

    storage.before["download"] = damage

    code, out, _ = run(OWNER, capsys)

    assert code == 1
    assert out.splitlines()[1:5] == [
        "downloaded: 2",
        "linked from an earlier copy: 0",
        "already there: 0",
        "not written: 2",
    ]
    assert len(written("2026-10-17")) == 2


def test_backup_files_names_the_variable_a_missing_setting_lacks(
    operator: None,
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(KEY_NAME)
    code, out, err = run(OWNER, capsys)
    assert (code, out) == (1, "")
    assert err == f"error: {KEY_NAME} is not set\n"
    assert storage.requests == []


def test_backup_files_takes_a_user_id_only(
    operator: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--owner` is a UUID, and the library function refuses anything else
    without echoing it."""
    with pytest.raises(SystemExit):
        main(["store", "backup-files", "--owner", "someone@example.test"])
    assert "someone@example.test" not in capsys.readouterr().out
    with pytest.raises(filesbackup.FilesBackupStopped) as refused:
        backup_files("not-a-uuid", os.environ, lambda _line: None)
    assert str(refused.value) == "--owner must be the account's user id, a UUID"


def test_backup_files_with_nothing_stored_writes_no_directory(
    operator: None, clock: Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(OWNER, capsys)
    assert (code, err) == (0, "")
    assert out.splitlines()[0] == "objects: 0"
    assert not day_directory("2026-10-17").exists()


def test_the_files_root_is_under_the_data_directory_never_in_git() -> None:
    """`data/**` is never-in-git (`config/data-classification.json`)."""
    root = filesbackup.files_root()
    assert root == data_dir() / "hosted" / "backups" / "files"
    classes = (
        Path(__file__).resolve().parents[3] / "config" / "data-classification.json"
    ).read_text(encoding="utf-8")
    assert '"data/**"' in classes


def test_the_operator_binding_reads_an_object_only_inside_the_owners_prefix(
    storage: FakeStorage,
) -> None:
    """`read_object` takes a name below the prefix, checked against the key
    grammar, so a name cannot climb out of the owner's folder to another's."""
    storage.put(f"{OWNER}/{object_name('answers/a.md')}", b"mine")
    storage.put(f"{OTHER_OWNER}/{object_name('answers/a.md')}", b"theirs")
    with storage.client() as http:
        store = SupabaseFileStore.for_operator(
            OWNER, origin=PROJECT_URL, secret_key=SECRET_KEY, http=http
        )
        assert store.read_object(object_name("answers/a.md")) == b"mine"
        assert store.read_object("answers/absent.md") is None
        for name in (f"../{OTHER_OWNER}/{object_name('answers/a.md')}", "a b", "", "/x"):
            with pytest.raises(InvalidFileKey):
                store.read_object(name)
        listed = {stored.name: stored.size for stored in store.stored_objects()}
    assert listed == {object_name("answers/a.md"): 4}
