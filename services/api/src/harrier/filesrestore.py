"""A `backup-files` copy uploaded into one hosted account (spec 155).

`harrier store restore-files --owner <user id> --from <dated directory>` runs
on the operator's machine, after a restore into a new Supabase project and
after `harrier store restore-storage` has made the bucket. It reads
`HARRIER_SUPABASE_URL` and `HARRIER_SUPABASE_SECRET_KEY` from the shell and
refuses a secret key defined in `.env` (`harrier.operator.read_admin`, as
`backup-files` does).

`--from` must be a dated directory directly under the root `backup-files`
writes (`harrier.filesbackup.files_root`, `data/hosted/backups/files`). Any
other path is refused before any request. The `<date>.index.json` listings sit
beside the dated directories, not inside them, so they are never uploaded.

Each file's path relative to the directory is the object's name below the
owner's prefix, as `backup-files` wrote it. It is uploaded create-only (spec
154) through the operator's binding (`SupabaseFileStore.create_object`), with
the content type its extension maps to in spec 107's `CONTENT_TYPES`. An
object already there is read and compared: the same bytes count `already
there`, other bytes `left, differs`, and it is never overwritten. A file that
is not a regular file, whose name the key grammar refuses, or whose extension
maps to no content type is `not written` with no request sent; so is any other
Storage error after the 429 waits (`artifactimport.patiently`). A 429 past the
last wait stops the run, naming the step.

Nothing printed names a key, an object name, the owner or the host: the
output is counts. Nothing on disk is changed.
"""

from __future__ import annotations

import os
import stat as stat_module
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import harrier.filestore as filestore
from harrier.artifactimport import RATE_LIMITED, patiently
from harrier.connection import FilesRestoreStopped
from harrier.filesbackup import WHERE, files_root, is_dated_name
from harrier.filestore import (
    CONTENT_TYPES,
    FileStoreError,
    InvalidFileKey,
    SupabaseFileStore,
    check_key,
)
from harrier.operator import OperatorError, read_admin

NOT_A_BACKUP = f"--from must be a dated directory directly under {WHERE}"


@dataclass
class FilesRestore:
    """What a run did. Counts only: nothing here names a file."""

    uploaded: int = 0
    present: int = 0
    differs: int = 0
    unwritten: int = 0

    @property
    def complete(self) -> bool:
        """An object left because it differs is not lost, so only a file not
        written makes the run incomplete (spec 155, Decisions taken, 3)."""
        return self.unwritten == 0


def backup_directory(source: Path) -> Path:
    """The dated directory `source` names, resolved, or FilesRestoreStopped.

    It must exist, be a directory, carry a `YYYY-MM-DD` name, and sit
    directly under the backup root once every link is resolved, so a
    symbolic link or a `..` cannot lead elsewhere."""
    try:
        root = files_root().resolve(strict=True)
        resolved = source.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FilesRestoreStopped(NOT_A_BACKUP) from None
    if resolved.parent != root or not is_dated_name(resolved.name) or not resolved.is_dir():
        raise FilesRestoreStopped(NOT_A_BACKUP)
    return resolved


def _walk(directory: Path) -> Iterator[tuple[str, Path, bool]]:
    """Every entry below `directory` that is not a directory, in name order:
    its path relative to `directory` in POSIX form, its path, and whether it
    is a regular file. A symbolic link is never followed, to a directory or
    to a file."""
    for current, folders, names in os.walk(directory, followlinks=False):
        here = Path(current)
        folders.sort()
        linked = [folder for folder in folders if (here / folder).is_symlink()]
        for folder in linked:
            folders.remove(folder)
        for name in sorted([*names, *linked]):
            path = here / name
            try:
                regular = stat_module.S_ISREG(path.lstat().st_mode)
            except OSError:
                regular = False
            yield path.relative_to(directory).as_posix(), path, regular


def _sendable(name: str, regular: bool) -> bool:
    """Whether a request may be sent for the file: a regular file whose name
    the key grammar accepts and whose extension maps to a content type."""
    if not regular:
        return False
    try:
        check_key(name)
    except InvalidFileKey:
        return False
    return PurePosixPath(name).suffix in CONTENT_TYPES


def _rate_limited(step: str) -> FilesRestoreStopped:
    return FilesRestoreStopped(
        f"{step}: Storage answered {RATE_LIMITED} after every wait; run the command again to resume"
    )


def _restore_one(
    store: SupabaseFileStore,
    name: str,
    path: Path,
    result: FilesRestore,
    report: Callable[[str], None],
) -> None:
    """Upload one file create-only, or compare it with the object already
    there, and count it."""
    try:
        data = path.read_bytes()
    except OSError:
        result.unwritten += 1
        return
    try:
        created = patiently(lambda: store.create_object(name, data), report, "restore")
    except FileStoreError as error:
        if error.status == RATE_LIMITED:
            raise _rate_limited("upload") from None
        result.unwritten += 1
        return
    if created:
        result.uploaded += 1
        return
    try:
        held = patiently(lambda: store.read_object(name), report, "restore")
    except FileStoreError as error:
        if error.status == RATE_LIMITED:
            raise _rate_limited("compare") from None
        result.unwritten += 1
        return
    if held is None:
        # Gone between the upload and the read: the next run writes it.
        result.unwritten += 1
    elif held == data:
        result.present += 1
    else:
        result.differs += 1


def restore_files(
    owner: str,
    source: Path,
    environ: Mapping[str, str],
    report: Callable[[str], None],
) -> FilesRestore:
    """Upload every file of the dated directory `source` into the account
    `owner` names.

    `environ` supplies the Storage project and its secret key. `report`
    receives one line at a time. A refusal, or a 429 past the waits, raises
    FilesRestoreStopped, with the objects already uploaded kept; the result
    says whether a file was not written.
    """
    try:
        account = str(uuid.UUID(owner))
    except ValueError:
        raise FilesRestoreStopped("--owner must be the account's user id, a UUID") from None
    try:
        admin = read_admin(environ)
    except OperatorError as error:
        raise FilesRestoreStopped(error.message) from None
    directory = backup_directory(source)
    entries = list(_walk(directory))
    report(f"files: {len(entries)}")
    result = FilesRestore()
    with filestore.storage_client() as http:
        store = SupabaseFileStore.for_operator(
            account, origin=admin.origin, secret_key=admin.secret_key, http=http
        )
        for name, path, regular in entries:
            if not _sendable(name, regular):
                result.unwritten += 1
                continue
            _restore_one(store, name, path, result, report)
    report(f"uploaded: {result.uploaded}")
    report(f"already there: {result.present}")
    report(f"left, differs: {result.differs}")
    report(f"not written: {result.unwritten}")
    report("restored" if result.complete else f"incomplete: {result.unwritten} not written")
    return result
