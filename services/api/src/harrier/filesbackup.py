"""One hosted account's Storage objects, copied to the operator's disk (spec 153).

`harrier store backup-files --owner <user id>` runs on the operator's machine,
with `HARRIER_SUPABASE_URL` and `HARRIER_SUPABASE_SECRET_KEY` in the shell and
refused from `.env` (`harrier.operator.read_admin`, spec 111). The secret key
therefore never enters GitHub, which is why the nightly workflow backs up the
database and this command backs up the files.

It lists every object under the owner's prefix through spec 107's operator
binding (`SupabaseFileStore.for_operator`) and writes each to
`<data dir>/hosted/backups/files/<UTC date>/<name>`, where `<name>` is the
object's name below the owner's prefix. Most families store a hashed name, so
that name is the SHA-256 of the key and cannot be turned back into one; it is
the name a restore uploads under (`docs/deploy.md`). The directory is under
`data/`, which is never in git (`config/data-classification.json`).

An object whose listing (size and version) equals the newest earlier copy's is
hard-linked from that copy instead of downloaded. A listing that changed but
whose bytes are equal is linked too, after the download. The listing of each
run is kept in `<date>.index.json` beside the dated directory, so a rerun the
same day resumes, and the next day's run has something to compare with.

A 429 waits and asks again, with spec 146's waits (`artifactimport.patiently`,
shared rather than copied). Nothing printed names a key, an object name, the
owner or the host: the output is counts, and a stop names its step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import harrier.filestore as filestore
from harrier.artifactimport import patiently
from harrier.connection import FilesBackupStopped
from harrier.db import data_dir
from harrier.filestore import (
    FileStoreDenied,
    FileStoreError,
    FileStoreUnavailable,
    InvalidFileKey,
    StoredObject,
    SupabaseFileStore,
    check_key,
)
from harrier.operator import OperatorError, read_admin

ROOT_PARTS: tuple[str, ...] = ("hosted", "backups", "files")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

# What the output says of where the copy went: the relative place, no path of
# this machine.
WHERE = "data/hosted/backups/files"


@dataclass
class FilesBackup:
    """What a run did. Counts only: nothing here names an object."""

    downloaded: int = 0
    linked: int = 0
    present: int = 0
    unwritten: int = 0

    @property
    def complete(self) -> bool:
        return self.unwritten == 0


@dataclass(frozen=True)
class Copy:
    """An earlier copy of one object: the directory that holds it, and the
    size and version its listing gave."""

    directory: Path
    size: int
    version: str


def files_root() -> Path:
    return data_dir().joinpath(*ROOT_PARTS)


def is_dated_name(name: str) -> bool:
    """Whether `name` is a dated directory's: `YYYY-MM-DD`, as a run names
    its copy. Spec 155's `restore-files` uploads only from one."""
    return _DATE.fullmatch(name) is not None


def _stopped(step: str, error: FileStoreError) -> FilesBackupStopped:
    """The one-line stop for a failed Storage call: the step and what to do,
    never a key, an object name or Storage's words."""
    if isinstance(error, FileStoreDenied):
        return FilesBackupStopped(
            f"{step}: Storage refused the credential; the secret key or the project is wrong"
        )
    if isinstance(error, FileStoreUnavailable):
        return FilesBackupStopped(
            f"{step}: Storage did not answer; run the command again to resume"
        )
    detail = (
        f"Storage answered {error.status}" if error.status is not None else "Storage refused it"
    )
    return FilesBackupStopped(f"{step}: {detail}")


# --- the earlier copies ---


def _index_path(root: Path, day: str) -> Path:
    return root / f"{day}.index.json"


def _read_index(root: Path, day: str) -> dict[str, tuple[int, str]]:
    """A day's listing: name to (size, version). Absent or unreadable is
    empty, so those objects are downloaded again."""
    try:
        document: object = json.loads(_index_path(root, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    entries: dict[str, tuple[int, str]] = {}
    for name, entry in cast("dict[str, object]", document).items():
        if not isinstance(entry, list) or len(cast("list[object]", entry)) != 2:
            continue
        size, version = cast("list[object]", entry)
        if isinstance(size, int) and isinstance(version, str):
            entries[name] = (size, version)
    return entries


def _earlier_days(root: Path, today: str) -> list[str]:
    """The dated directories on or before `today`, newest first."""
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    days = [
        child.name
        for child in children
        if child.is_dir() and _DATE.fullmatch(child.name) and child.name <= today
    ]
    return sorted(days, reverse=True)


def _newest_copy(
    root: Path, days: list[str], indexes: dict[str, dict[str, tuple[int, str]]], name: str
) -> Copy | None:
    for day in days:
        entry = indexes[day].get(name)
        if entry is not None and (root / day / name).is_file():
            return Copy(root / day, entry[0], entry[1])
    return None


# --- the files ---


def _make_private(directory: Path) -> None:
    """Make `directory`, and every directory below `data/hosted` on the way
    to it, readable by the operator alone. `data/hosted` itself is the
    erasure ledger's and keeps its mode. `mkdir(parents=True, mode=...)`
    would apply the mode to the last directory only."""
    boundary = files_root().parent.parent
    boundary.mkdir(parents=True, exist_ok=True)
    missing: list[Path] = []
    current = directory
    while current != boundary and not current.exists():
        missing.append(current)
        current = current.parent
    for created in reversed(missing):
        created.mkdir(mode=0o700)
        created.chmod(0o700)


def _write(path: Path, data: bytes) -> None:
    """Whole or not at all, readable by the operator alone."""
    _make_private(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _link(source: Path, path: Path) -> None:
    """`path` as a hard link to `source`; a copy where the file system will
    not link."""
    _make_private(path.parent)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        try:
            os.link(source, temporary)
        except OSError:
            shutil.copyfile(source, temporary)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _save_index(root: Path, day: str, entries: Mapping[str, tuple[int, str]]) -> None:
    document = {name: [size, version] for name, (size, version) in sorted(entries.items())}
    _write(_index_path(root, day), (json.dumps(document, indent=1) + "\n").encode("utf-8"))


# --- the run ---


def backup_files(
    owner: str,
    environ: Mapping[str, str],
    report: Callable[[str], None],
    now: Callable[[], datetime] | None = None,
) -> FilesBackup:
    """Copy every object of the account `owner` names.

    `environ` supplies the Storage project and its secret key. `report`
    receives one line at a time. A refusal or a stop raises FilesBackupStopped
    with the objects already written kept; the result says whether anything
    was left unwritten.
    """
    try:
        account = str(uuid.UUID(owner))
    except ValueError:
        raise FilesBackupStopped("--owner must be the account's user id, a UUID") from None
    try:
        admin = read_admin(environ)
    except OperatorError as error:
        raise FilesBackupStopped(error.message) from None
    today = (now() if now is not None else datetime.now(UTC)).strftime("%Y-%m-%d")
    root = files_root()
    day = root / today
    days = _earlier_days(root, today)
    indexes = {earlier: _read_index(root, earlier) for earlier in days}
    result = FilesBackup()
    written: dict[str, tuple[int, str]] = dict(indexes.get(today, {}))
    try:
        with filestore.storage_client() as http:
            store = SupabaseFileStore.for_operator(
                account, origin=admin.origin, secret_key=admin.secret_key, http=http
            )
            try:
                objects = patiently(store.stored_objects, report, "backup")
            except FileStoreError as error:
                raise _stopped("list", error) from None
            report(f"objects: {len(objects)}")
            for stored in objects:
                _copy_one(store, stored, root, day, days, indexes, written, result, report)
    finally:
        # What was written is kept, so a rerun resumes and tomorrow's run has
        # a listing to compare with.
        if written:
            _save_index(root, today, written)
    report(f"downloaded: {result.downloaded}")
    report(f"linked from an earlier copy: {result.linked}")
    report(f"already there: {result.present}")
    report(f"not written: {result.unwritten}")
    report(f"backed up under {WHERE}" if result.complete else "incomplete")
    return result


def _copy_one(
    store: SupabaseFileStore,
    stored: StoredObject,
    root: Path,
    day: Path,
    days: list[str],
    indexes: dict[str, dict[str, tuple[int, str]]],
    written: dict[str, tuple[int, str]],
    result: FilesBackup,
    report: Callable[[str], None],
) -> None:
    """Write one object to the day's directory, or link it, or count it
    unwritten. A Storage failure other than a 429 past its waits stops the
    run."""
    try:
        check_key(stored.name)
    except InvalidFileKey:
        result.unwritten += 1
        return
    target = day / stored.name
    entry = (stored.size, stored.version)
    earlier = _newest_copy(root, days, indexes, stored.name)
    if earlier is not None and (earlier.size, earlier.version) == entry:
        if earlier.directory == day:
            result.present += 1
        else:
            _link(earlier.directory / stored.name, target)
            result.linked += 1
        written[stored.name] = entry
        return
    try:
        data = patiently(lambda: store.read_object(stored.name), report, "backup")
    except FileStoreError as error:
        raise _stopped("download", error) from None
    if data is None or (stored.size > 0 and len(data) != stored.size):
        # Gone since the listing, or cut short: written by the next run.
        result.unwritten += 1
        return
    if earlier is not None and (earlier.directory / stored.name).read_bytes() == data:
        if earlier.directory == day:
            result.present += 1
        else:
            _link(earlier.directory / stored.name, target)
            result.linked += 1
    else:
        _write(target, data)
        result.downloaded += 1
    written[stored.name] = entry
