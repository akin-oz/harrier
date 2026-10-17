"""`harrier store restore-files` uploads a `backup-files` copy into one hosted
account's Storage prefix, create-only (spec 155).

Each test drives the command through `main` against the fake Storage server
(`storage_support`, spec 107; the autouse `storage` fixture routes every
hosted file store to it, so no test reaches a network). The copy is written
by the test under its own `HARRIER_DATA_DIR`, as `backup-files` lays it out;
its objects are synthetic and sit under a synthetic owner. No test reads the
real `data/`. The command needs no database.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from storage_support import BUCKET, FakeStorage
from test_backup_files import (
    FILES,
    KEY_NAME,
    OTHER_OWNER,
    OWNER,
    PROJECT_HOST,
    SECRET_KEY,
    object_name,
    operator,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)

from harrier import artifactimport
from harrier.db import data_dir
from harrier.filesrestore import NOT_A_BACKUP
from harrier.filestore import CONTENT_TYPES
from harrier_cli.main import main

DAY = "2026-10-17"
RELATIVE = f"data/hosted/backups/files/{DAY}"


def backup_root() -> Path:
    return data_dir() / "hosted" / "backups" / "files"


def write_copy(files: dict[str, bytes], day: str = DAY) -> Path:
    """A `backup-files` copy: each object under its name below the owner's
    prefix, and the day's listing beside the directory, not inside it."""
    directory = backup_root() / day
    for key, data in files.items():
        path = directory / object_name(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (backup_root() / f"{day}.index.json").write_text("{}\n", encoding="utf-8")
    return directory


def run(source: str | Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(["store", "restore-files", "--owner", OWNER, "--from", str(source)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def uploads(storage: FakeStorage, start: int = 0) -> list[httpx.Request]:
    return [
        request for request in storage.requests[start:] if storage.operation(request)[0] == "upload"
    ]


def held(storage: FakeStorage, key: str, owner: str = OWNER) -> bytes:
    return storage.objects[f"{owner}/{object_name(key)}"].data


def test_restore_files_uploads_a_backup_copy_create_only_and_names_none(
    operator: None,  # noqa: F811
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every file of the dated directory goes to `<owner>/<its relative
    path>`, without upsert, with its content type. An equal object counts
    `already there`; one with other bytes is left and the run goes on, exit
    0; a file of no accepted type is `not written`, with no request, and the
    run exits 1. No line names a key, a name, the owner or the host (spec
    155, acceptance; Decisions taken, 3)."""
    write_copy(FILES)
    same = "answers/example-co-role.md"
    differs = "cover-letters/example-co-role.md"
    storage.put(f"{OWNER}/{object_name(same)}", FILES[same], "text/markdown")
    storage.put(f"{OWNER}/{object_name(differs)}", b"# Written on the new project\n")
    storage.put(f"{OTHER_OWNER}/{object_name(same)}", b"theirs")
    names = {object_name(key) for key in FILES}

    code, out, err = run(RELATIVE, capsys)

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "files: 4",
        "uploaded: 2",
        "already there: 1",
        "left, differs: 1",
        "not written: 0",
        "restored",
    ]
    sent = uploads(storage)
    assert {request.headers["x-upsert"] for request in sent} == {"false"}
    assert sorted(storage.operation(request)[1] for request in sent) == sorted(
        f"{OWNER}/{name}" for name in names
    )
    for key, data in FILES.items():
        if key != differs:
            assert held(storage, key) == data, key
    for request in sent:
        name = storage.operation(request)[1]
        assert request.headers["content-type"] == CONTENT_TYPES[Path(name).suffix], name
    # The differing object is never overwritten, and another owner's is untouched.
    assert held(storage, differs) == b"# Written on the new project\n"
    assert held(storage, same, OTHER_OWNER) == b"theirs"
    # The day's listing sits beside the directory and is never uploaded.
    assert not any("index" in name for name in storage.names())
    shown = out + err
    for secret in (OWNER, PROJECT_HOST, SECRET_KEY, BUCKET, "resumes", "example-co", *names):
        assert secret not in shown

    # --- a second run: everything is there, the differing object still left ---
    code, out, err = run(RELATIVE, capsys)

    assert (code, err) == (0, "")
    assert out.splitlines()[1:5] == [
        "uploaded: 0",
        "already there: 3",
        "left, differs: 1",
        "not written: 0",
    ]
    assert held(storage, differs) == b"# Written on the new project\n"

    # --- a file of a type the bucket does not accept: not written, no request ---
    odd = backup_root() / DAY / "resumes" / "notes.txt"
    odd.write_bytes(b"synthetic notes\n")
    before = len(storage.requests)

    code, out, err = run(backup_root() / DAY, capsys)

    assert (code, err) == (1, "")
    assert out.splitlines()[0] == "files: 5"
    assert out.splitlines()[4:] == ["not written: 1", "incomplete: 1 not written"]
    assert not any(
        storage.operation(request)[1].endswith(".txt") for request in storage.requests[before:]
    )
    assert "notes" not in out


def test_restore_files_counts_a_failed_upload_and_stops_past_the_rate_limit(
    operator: None,  # noqa: F811
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage refusing one upload counts it `not written` and the run goes
    on, as before `restore-storage` made the bucket. A 429 waits as spec
    146's import does; one past the last wait stops the run, naming the
    step, and a rerun finishes."""
    waits: list[float] = []
    monkeypatch.setattr(artifactimport, "_sleep", waits.append)
    write_copy(FILES)
    storage.failures["upload"] = [400, 429]

    code, out, err = run(RELATIVE, capsys)

    assert (code, err) == (1, "")
    assert waits == [2]
    assert out.count("Storage asked the restore to slow down; waiting") == 1
    assert out.splitlines()[-5:] == [
        "uploaded: 3",
        "already there: 0",
        "left, differs: 0",
        "not written: 1",
        "incomplete: 1 not written",
    ]

    storage.failures["upload"] = [429] * (len(artifactimport.RETRY_WAITS) + 1)
    code, out, err = run(RELATIVE, capsys)

    assert code == 1
    assert out.splitlines() == [
        "files: 4",
        *(
            f"Storage asked the restore to slow down; waiting {wait} s"
            for wait in artifactimport.RETRY_WAITS
        ),
    ]
    assert err == (
        "error: upload: Storage answered 429 after every wait; run the command again to resume\n"
    )

    code, out, err = run(RELATIVE, capsys)

    assert (code, err) == (0, "")
    assert out.splitlines()[1:3] == ["uploaded: 1", "already there: 3"]
    assert all(held(storage, key) == data for key, data in FILES.items())


def test_restore_files_refuses_a_foreign_directory_and_an_env_key(
    operator: None,  # noqa: F811
    storage: FakeStorage,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`--from` must be a dated directory directly under the backup root,
    once links are resolved; the secret key may not be defined in `.env`.
    Both are refused before any request (spec 155, acceptance)."""
    write_copy(FILES)
    elsewhere = tmp_path / "elsewhere" / "2026-10-18"
    elsewhere.mkdir(parents=True)
    (elsewhere / "a.md").write_bytes(b"synthetic\n")
    linked = backup_root() / "2026-10-18"
    linked.symlink_to(elsewhere, target_is_directory=True)
    (backup_root() / "latest").mkdir()
    foreign: list[str | Path] = [
        elsewhere,
        linked,
        backup_root(),
        backup_root() / "latest",
        backup_root() / f"{DAY}.index.json",
        backup_root() / DAY / "resumes",
        backup_root() / DAY / ".." / ".." / "files-elsewhere",
        f"{RELATIVE}/../../../..",
        backup_root() / "2026-10-19",
        data_dir(),
    ]

    for source in foreign:
        code, out, err = run(source, capsys)
        assert (code, out, err) == (1, "", f"error: {NOT_A_BACKUP}\n"), source
    assert storage.requests == []
    assert NOT_A_BACKUP == (
        "--from must be a dated directory directly under data/hosted/backups/files"
    )

    (tmp_path / ".env").write_text(f"{KEY_NAME}=sb_secret_other_value\n", encoding="utf-8")

    code, out, err = run(RELATIVE, capsys)

    assert (code, out) == (1, "")
    assert err.startswith(f"error: {KEY_NAME} is defined in .env;")
    assert err.count("\n") == 1
    assert storage.requests == []
    for secret in (SECRET_KEY, "sb_secret_other_value", OWNER, PROJECT_HOST):
        assert secret not in err
