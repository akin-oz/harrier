"""A local store's artifacts import into one hosted account (spec 146).

`harrier store import-artifacts --owner <user id>` copies the regular files
directly inside `data/resumes`, `data/cover-letters` and `data/answers` into
the account's Storage prefix. The reading side is a `LocalFileStore`, the
writing side the operator's binding of spec 107's seam
(`SupabaseFileStore.for_operator`). This module never builds an object name:
the seam does, from the owner and the key, and the key is
`<directory>/<file name>`, the key the writers use.

The order of a run:

1. The container does not own the data directory (spec 061, spec 075). The
   command never opens the SQLite file.
2. The Storage project and the secret key, as `harrier operator` reads them.
3. The database login (spec 134's checks), the owner connection (spec 106)
   and the store's version (spec 103).
4. Inside one owner transaction that only reads: the account has a track and
   a job, and the names the app would look for, built from the account's own
   rows.
5. At least one of the three directories exists.
6. The files fit the account: at least one is in that set of names.
7. The copy, family by family, file by file. There is no transaction: each
   upload is whole or absent, and a rerun skips what is already there.

Every refusal is an `ArtifactImportRefused`, raised before the first upload.
A failure during the copy is an `ArtifactImportStopped`. Nothing here prints
a name, a key, an owner or a host: the caller's `report` receives counts and
family names only. The module deletes and changes nothing locally.
"""

from __future__ import annotations

import stat as stat_module
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import harrier.filestore as filestore
from harrier.apply.answers import ANSWERS, answers_key_for
from harrier.apply.letters import COVER_LETTERS, cover_letter_keys_for
from harrier.connection import URL_VARIABLE, Connection, StoreError, open_store, owner_claims
from harrier.db import check_database_ownership, default_db_path
from harrier.filestore import (
    CONTENT_TYPES,
    SIZE_LIMIT,
    FileStoreDenied,
    FileStoreError,
    FileStoreUnavailable,
    InvalidFileKey,
    LocalFileStore,
    SupabaseFileStore,
    check_key,
    join_key,
)
from harrier.operator import OperatorError, read_admin
from harrier.resume.content import ResumeBundleError, load_bundle
from harrier.resume.tailor import RESUMES, resume_keys_for
from harrier.tracker.importlocal import check_login_role, hosted_url
from harrier.tracks import Scope, list_tracks

# The three directories, in the order a run copies and reports them.
FAMILIES: tuple[str, ...] = (RESUMES, COVER_LETTERS, ANSWERS)

NO_ACCOUNT = "that owner sees no track: the account does not exist or has not been invited"
NO_JOBS = (
    "the account has a track and no job; run harrier store import-local first, "
    "or no artifact would be found for it"
)
NO_DIRECTORY = (
    "none of the data directory's resumes, cover-letters and answers directories exists; "
    "this is probably the wrong machine"
)
FITS_NO_JOB = (
    "none of the files fits a job of this account, so --owner is probably not the account "
    "harrier store import-local filled; nothing was uploaded"
)


class ArtifactImportRefused(StoreError):
    """A check refused the import before the first upload. The text names
    the check, never an owner, a host, a file or a key."""


class ArtifactImportStopped(StoreError):
    """The copy stopped at a file. The text names the step and the family,
    never the file, the key or the owner. Objects already copied stay."""


@dataclass
class FamilyCount:
    copied: int = 0
    present: int = 0


@dataclass
class Imported:
    """What a run did. Counts only: nothing here names a file."""

    families: dict[str, FamilyCount] = field(default_factory=dict[str, FamilyCount])
    over_limit: int = 0
    other: int = 0
    differs: int = 0
    attached: int = 0
    unattached: int = 0

    @property
    def complete(self) -> bool:
        """Whether no file was left uncopied (spec 146, Open decision 4)."""
        return not (self.over_limit or self.other or self.differs)


@dataclass(frozen=True)
class LocalFile:
    family: str
    name: str
    size: int

    @property
    def key(self) -> str:
        return join_key(self.family, self.name)


# --- the account's names ---


@dataclass(frozen=True)
class AccountNames:
    """What the account's own rows say the app would look for."""

    keys: frozenset[str]
    has_jobs: bool


def account_names(conn: Connection) -> AccountNames:
    """Every key the app would look for, for every job the owner sees.

    For each track, with its own resume bundle, and each of its jobs: every
    key `resume_keys_for`, `cover_letter_keys_for` and `answers_key_for`
    return, sidecars included, built from the job's `company` and `title` as
    written. The same helpers `harrier.artifacts` reads through, so this set
    cannot drift from where the routes look. A track with no usable bundle
    has no resume keys, as `artifacts_for_job` reports none for it.
    """
    keys: set[str] = set()
    has_jobs = False
    for track in list_tracks(conn):
        candidate: str | None
        try:
            candidate = load_bundle(conn, Scope(track=track)).name
        except ResumeBundleError:
            candidate = None
        rows = conn.execute(
            "SELECT company, title FROM jobs WHERE track_id = ? ORDER BY id", (track.id,)
        ).fetchall()
        for row in rows:
            has_jobs = True
            company = str(row[0] or "")
            role = str(row[1] or "")
            if candidate is not None:
                keys.update(resume_keys_for(candidate, company, role).values())
            keys.update(cover_letter_keys_for(company, role).values())
            keys.add(answers_key_for(company, role))
    return AccountNames(frozenset(keys), has_jobs)


def read_account(conn: Connection) -> frozenset[str]:
    """Checks 4 and 6's input, in one transaction that only reads: the owner
    sees a track and a job, and the set of keys their jobs name."""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM tracks)").fetchone()
        if row is None or not row[0]:
            raise ArtifactImportRefused(NO_ACCOUNT)
        names = account_names(conn)
        if not names.has_jobs:
            raise ArtifactImportRefused(NO_JOBS)
        return names.keys


# --- the local files ---


def local_files(root: Path, family: str) -> list[LocalFile]:
    """The regular files directly inside `root/family`, in name order.

    A name that begins with a dot, a subdirectory and anything that is not a
    regular file (a symbolic link included, which is never followed) is not
    listed. An absent directory lists nothing. Raises ArtifactImportStopped
    when the directory or a file in it cannot be read.
    """
    directory = root / family
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError:
        raise ArtifactImportStopped(f"read {family}: the directory could not be read") from None
    found: list[LocalFile] = []
    for child in children:
        if child.name.startswith("."):
            continue
        try:
            result = child.lstat()
        except OSError:
            raise ArtifactImportStopped(f"read {family}: a local file could not be read") from None
        if stat_module.S_ISREG(result.st_mode):
            found.append(LocalFile(family, child.name, result.st_size))
    return found


def skip_reason(file: LocalFile) -> str | None:
    """Why the file is never sent, or None. `other` is a name outside the
    key grammar, an extension outside the bucket's five, or no bytes;
    `over` is a size past the seam's limit. Neither the limit nor the
    extension table is restated here."""
    try:
        check_key(file.key)
    except InvalidFileKey:
        return "other"
    if PurePosixPath(file.name).suffix not in CONTENT_TYPES:
        return "other"
    if file.size == 0:
        return "other"
    if file.size > SIZE_LIMIT:
        return "over"
    return None


# --- the copy ---


def _stopped(step: str, family: str, error: FileStoreError) -> ArtifactImportStopped:
    """The one-line stop for a failed Storage call: the step, the family and
    what to do, never a key, an object name or Storage's words."""
    where = f"{step} {family}"
    if isinstance(error, FileStoreDenied):
        return ArtifactImportStopped(
            f"{where}: Storage refused the credential; the secret key or the project is wrong"
        )
    if isinstance(error, FileStoreUnavailable):
        return ArtifactImportStopped(
            f"{where}: Storage did not answer; run the command again to resume"
        )
    detail = (
        f"Storage answered {error.status}" if error.status is not None else "Storage refused it"
    )
    return ArtifactImportStopped(f"{where}: {detail}")


def _read_local(store: LocalFileStore, file: LocalFile) -> bytes:
    try:
        data = store.read(file.key)
    except OSError:
        raise ArtifactImportStopped(f"read {file.family}: a local file could not be read") from None
    if data is None:
        raise ArtifactImportStopped(f"read {file.family}: a local file could not be read")
    return data


def _copy_one(
    hosted: SupabaseFileStore,
    local: LocalFileStore,
    file: LocalFile,
    result: Imported,
    attached: frozenset[str],
) -> None:
    """Send one file that passed the skip rules, or leave it as it is."""
    family = file.family
    count = result.families[family]
    key = file.key
    data = _read_local(local, file)
    try:
        found = hosted.stat(key)
        hosted_bytes = hosted.read(key) if found is not None else None
    except FileStoreError as error:
        raise _stopped("check", family, error) from None
    if hosted_bytes is not None:
        if hosted_bytes != data:
            result.differs += 1
            return
        count.present += 1
    else:
        try:
            hosted.put(key, data, atomic=True)
            after = hosted.stat(key)
        except FileStoreError as error:
            raise _stopped("upload", family, error) from None
        if after is None or after.size != len(data):
            raise ArtifactImportStopped(
                f"upload {family}: the size read back differs from the local file; "
                "the object is left as Storage holds it"
            )
        count.copied += 1
    if key in attached:
        result.attached += 1
    else:
        result.unattached += 1


def _family_line(family: str, count: FamilyCount) -> str:
    return f"{family}: {count.copied} copied, {count.present} already there"


def import_artifacts(
    owner: str, environ: Mapping[str, str], report: Callable[[str], None]
) -> Imported:
    """Copy the three directories into the account `owner` names.

    `environ` supplies the hosted database URL and the Storage project and
    secret key. `report` receives one line at a time, as each is final. A
    refusal raises ArtifactImportRefused (or the seam's StoreError for the
    database) with nothing uploaded; DatabaseOwnershipError is raised for a
    data directory a container holds. The result says whether anything was
    left uncopied.
    """
    claims = owner_claims(owner)
    # 1. The data directory is the host's. The SQLite file is never opened.
    check_database_ownership(default_db_path())
    # 2. The Storage project and its secret key.
    try:
        admin = read_admin(environ)
    except OperatorError as error:
        raise ArtifactImportRefused(error.message) from None
    # 3 and 4. The database, as the owner, read only.
    url = hosted_url(environ)
    check_login_role(url)
    conn = open_store({URL_VARIABLE: url}, owner=str(claims["sub"]))
    try:
        attached = read_account(conn)
    finally:
        conn.close()
    # 5. The local directories.
    local = LocalFileStore()
    if not any((local.root / family).is_dir() for family in FAMILIES):
        raise ArtifactImportRefused(NO_DIRECTORY)
    inventory = {family: local_files(local.root, family) for family in FAMILIES}
    # 6. The files fit the account.
    everything = [file for files in inventory.values() for file in files]
    if everything and not any(file.key in attached for file in everything):
        raise ArtifactImportRefused(FITS_NO_JOB)
    # 7. The copy.
    result = Imported()
    with filestore.storage_client() as http:
        hosted = SupabaseFileStore.for_operator(
            str(claims["sub"]), origin=admin.origin, secret_key=admin.secret_key, http=http
        )
        for family in FAMILIES:
            result.families[family] = FamilyCount()
            for file in inventory[family]:
                reason = skip_reason(file)
                if reason == "other":
                    result.other += 1
                elif reason == "over":
                    result.over_limit += 1
                else:
                    _copy_one(hosted, local, file, result, attached)
            report(_family_line(family, result.families[family]))
    report(f"not copied, over the size limit: {result.over_limit}")
    report(f"not copied, other type or name: {result.other}")
    report(f"not copied, differs from the hosted object: {result.differs}")
    report(f"attached: {result.attached}")
    report(f"unattached: {result.unattached}")
    report("imported" if result.complete else "incomplete")
    return result
