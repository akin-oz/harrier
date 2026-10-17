"""One seam between the domain and its files (spec 107).

Resumes, letters, answers, drafts, summaries, seen state and scoring models
are files. Every module that reads or writes one names it by a key, a path
relative to the data directory, and asks `files()` for the store that holds
it. This module is the only one that turns a key into a file.

Locally the store is `LocalFileStore`, rooted at `data_dir()` read at each
call, so `HARRIER_DATA_DIR`, demo mode and every test that moves the data
directory behave as they did. Each operation does what the call site did
before the seam existed: an atomic write goes through `harrier.atomicio`, a
plain write is a plain write, an append opens the file for append. The
bytes, the file and its mode do not change
(`services/api/tests/test_filestore.py::test_the_local_store_writes_what_it_wrote_before`).

`bind_files()` sets the store for the current context. A bound store wins
over the local default.

Hosted (`HARRIER_AUTH=supabase` in the process environment, or a hosted
run's `HARRIER_RUN_OWNER`), there is no local default: `files()` returns
the bound store or raises `FileStoreUnbound`, so a path that forgot to bind
fails instead of writing a disk every owner shares. The store a request or
a hosted run binds (`run_store`) is `SupabaseFileStore`,
which keeps each owner's files in the private bucket `harrier-files` under
the owner's prefix, through Supabase Storage's HTTP API (spec 107, "The
hosted store"). `httpx` is imported where a request is sent, never at module
level: a local command never needs it.
"""

from __future__ import annotations

import builtins
import errno
import hashlib
import json
import logging
import os
import re
import stat as stat_module
import uuid
from collections.abc import Generator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Protocol, cast

from harrier.atomicio import DamagedStateError, parse_json_mapping, write_bytes_atomic
from harrier.connection import RUN_OWNER_VARIABLE
from harrier.db import data_dir

if TYPE_CHECKING:
    import httpx

# One segment of a key: names come from `slugify`, a hex digest, a track id
# or a literal in the code, and every one of them fits.
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


class InvalidFileKey(ValueError):
    """A key outside the grammar: refused before any file is touched."""


class FileStoreError(OSError):
    """A store could not do what it was asked.

    An `OSError`, so every caller's existing `except OSError` keeps its
    meaning whichever store answered. The message names the operation and
    the key's first segment only, never an owner, a credential, the whole
    key or an object name (spec 107, "The hosted store"). `status` is the
    HTTP status Storage answered, when it answered one.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FileStoreDenied(FileStoreError):
    """The store refused the caller's credentials."""


class FileStoreUnavailable(FileStoreError):
    """The store did not answer, or answered with a server error."""


class FileTooLarge(FileStoreError):
    """The data is larger than the store accepts."""


class FileStoreUnbound(RuntimeError):
    """No store is bound where one must be.

    Deliberately not a `FileStoreError`: a missing binding is a defect in
    the caller, and an `except OSError` that reads a cache as empty or falls
    back to the rules score must not absorb it.
    """


def check_key(key: str) -> str:
    """The key itself, or `InvalidFileKey`.

    A key is a relative POSIX path: one or more segments joined by `/`, each
    matching `[A-Za-z0-9._-]+` and neither `.` nor `..`.
    """
    segments = key.split("/")
    for segment in segments:
        if segment in (".", "..") or _SEGMENT.fullmatch(segment) is None:
            raise InvalidFileKey(f"not a file key: {key!r}")
    return key


def join_key(prefix: str, name: str) -> str:
    """`prefix/name`, or `name` alone under an empty prefix."""
    return f"{prefix}/{name}" if prefix else name


def key_name(key: str) -> str:
    """The key's last segment: the file name a person sees."""
    return PurePosixPath(key).name


# Locally `(st_ino, st_mtime_ns, st_size)`; a hosted store's own version.
FileVersion = tuple[int, int, int] | str


@dataclass(frozen=True)
class Entry:
    """One file: its key, its size, when it last changed, and a version that
    changes whenever its content may have."""

    key: str
    size: int
    mtime: float
    version: FileVersion

    @property
    def name(self) -> str:
        return key_name(self.key)


def family_of(key: str) -> str:
    """The key's first segment: all a message may name of it."""
    return key.split("/", 1)[0]


class FileStore(Protocol):
    def read(self, key: str) -> bytes | None:
        """The bytes, or None when there is no such file."""
        ...

    def put(self, key: str, data: bytes, *, atomic: bool) -> None:
        """Replace the file with `data`, creating its parents."""
        ...

    def append(self, key: str, text: str) -> None:
        """Add `text` to the end of the file, creating it if absent."""
        ...

    def delete(self, key: str) -> None:
        """Remove the file; an absent one is not an error."""
        ...

    def stat(self, key: str) -> Entry | None:
        """The file's entry, or None when there is no such file."""
        ...

    def list(self, prefix: str) -> builtins.list[Entry]:
        """The files directly under `prefix`, in key order."""
        ...

    def local_path(self, key: str) -> Path | None:
        """The file on this machine's disk, or None when the store has none."""
        ...

    def qualified_name(self, key: str) -> str:
        """A name for the file unique across every store and owner."""
        ...

    def shown_name(self, key: str) -> str:
        """How a message or a log line names the file: the path locally, as
        before the seam; hosted the key's first segment only, so no message
        names an owner (spec 107)."""
        ...


class LocalFileStore:
    """Files under one directory: `data_dir()` unless a root is given.

    Without a root the directory is read at each call, as every writer read
    it before the seam existed.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else data_dir()

    def _path(self, key: str) -> Path:
        return self.root / check_key(key)

    def read(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def put(self, key: str, data: bytes, *, atomic: bool) -> None:
        path = self._path(key)
        if atomic:
            write_bytes_atomic(path, data)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def append(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def stat(self, key: str) -> Entry | None:
        path = self._path(key)
        try:
            result = path.stat()
        except FileNotFoundError:
            return None
        if not stat_module.S_ISREG(result.st_mode):
            return None
        return Entry(
            key=key,
            size=result.st_size,
            mtime=result.st_mtime,
            version=(result.st_ino, result.st_mtime_ns, result.st_size),
        )

    def list(self, prefix: str) -> builtins.list[Entry]:
        directory = self._path(prefix)
        try:
            children = builtins.list(directory.iterdir())
        except FileNotFoundError:
            return []
        entries: builtins.list[Entry] = []
        for child in children:
            # A name no key can hold is not one this system wrote.
            if _SEGMENT.fullmatch(child.name) is None:
                continue
            try:
                entry = self.stat(join_key(prefix, child.name))
            except OSError as error:
                # A child gone since the listing, or a symlink loop, is not a
                # file here, as `glob` and `is_file()` read it (CodeRabbit on
                # PR #361). Any other error still raises.
                if error.errno in _ABSENT_ERRNOS:
                    continue
                raise
            if entry is not None:
                entries.append(entry)
        return sorted(entries, key=lambda entry: entry.key)

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def qualified_name(self, key: str) -> str:
        return str(self._path(key))

    def shown_name(self, key: str) -> str:
        return str(self._path(key))


_bound: ContextVar[FileStore | None] = ContextVar("harrier_files", default=None)


# Hosted mode, as spec 104 reads it: `HARRIER_AUTH=supabase` in the process
# environment. The names are `harrier_api.hostedauth`'s, which this package
# may not import.
AUTH_VARIABLE = "HARRIER_AUTH"
HOSTED_AUTH = "supabase"

UNBOUND_TEXT = "no file store is bound here; a hosted process keeps no local files"


def hosted_files(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this process is hosted, so `files()` has no local default:
    the hosted API (`HARRIER_AUTH=supabase`), or a hosted run, which spec 106
    marks by `HARRIER_RUN_OWNER` and which carries no `HARRIER_AUTH` (spec
    107, Amendment (2026-10-16): PR 3's build). Read from the process
    environment at each call."""
    env = os.environ if environ is None else environ
    return env.get(AUTH_VARIABLE, "") == HOSTED_AUTH or RUN_OWNER_VARIABLE in env


def files() -> FileStore:
    """The store for this context: the bound one, else, locally, the data
    directory. Hosted, with nothing bound, `FileStoreUnbound`: never a local
    store (spec 107, "Which store a call uses")."""
    store = _bound.get()
    if store is not None:
        return store
    if hosted_files():
        raise FileStoreUnbound(UNBOUND_TEXT)
    return LocalFileStore()


@contextmanager
def bind_files(store: FileStore) -> Generator[FileStore]:
    """Make `store` the one `files()` returns until the block ends."""
    token = _bound.set(store)
    try:
        yield store
    finally:
        _bound.reset(token)


@contextmanager
def files_in(directory: Path | None, prefix: str) -> Generator[str]:
    """The key prefix a writer with an optional output directory uses.

    With no directory nothing is bound, and the prefix is the family's own.
    With one, a `LocalFileStore` rooted there is bound for the block and the
    files sit directly in it, so the prefix is empty. A caller-chosen
    directory is a local command's or a test's, never a data path.
    """
    if directory is None:
        yield prefix
        return
    with bind_files(LocalFileStore(directory)):
        yield ""


def local_file(key: str) -> Path:
    """The file behind `key` on this machine, for a caller that must hand a
    path on: a command's printed output, or a test.

    Raises `FileStoreError` when the bound store keeps no local files.
    """
    path = files().local_path(key)
    if path is None:
        raise FileStoreError(f"{family_of(key)} has no local file in this store")
    return path


def artifact_path(key: str) -> Path:
    """How a writer reports a file it has just written, for a command to
    print or a document to name: the local file, as `local_file` gives it;
    for a store with no local files, the key's last segment, the file name a
    person sees when they download it (spec 107, "Keys and object names").

    For showing only. The hosted form is a bare name, never a path to open:
    nothing on this machine's disk is behind it (spec 107, Amendment
    (2026-10-16): PR 3's build)."""
    path = files().local_path(key)
    return Path(key_name(key)) if path is None else path


# The errors `Path.is_file()` and `Path.exists()` read as "not there" rather
# than raise: a missing entry, a file where a directory should be, a bad
# descriptor and a symlink loop.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


def is_present(key: str) -> bool:
    """Whether a file is at the key, with `Path.is_file()`'s tolerance: an
    error it read as absent is absent, any other is raised. `stat` itself
    raises `NotADirectoryError`, which the model cache relies on, so the
    callers that used `is_file()` or `exists()` ask this instead
    (data-integrity review of PR #361)."""
    try:
        return files().stat(key) is not None
    except OSError as error:
        if error.errno in _ABSENT_ERRNOS:
            return False
        raise


def load_text(key: str) -> str | None:
    """The file decoded as UTF-8, or None when absent."""
    data = files().read(key)
    return None if data is None else data.decode("utf-8")


def save_text(key: str, text: str, *, atomic: bool) -> None:
    """`text` encoded as UTF-8, written as `put` writes."""
    files().put(key, text.encode("utf-8"), atomic=atomic)


def save_json(key: str, payload: object, *, atomic: bool) -> None:
    """`payload` in the form `write_json_atomic` writes it, which the plain
    JSON writers share: indented by two, UTF-8 and unescaped, with no final
    newline."""
    save_text(key, json.dumps(payload, indent=2, ensure_ascii=False), atomic=atomic)


def load_mapping(key: str) -> dict[str, Any] | None:
    """The JSON object in a state file, None if there is none, or raise.

    `read_json_mapping`'s rules (spec 040) over the seam: a read that fails
    is `DamagedStateError`, never an empty state.
    """
    store = files()
    # Never the qualified name, which hosted includes the owner (spec 107).
    where = store.shown_name(key)
    try:
        raw = store.read(key)
    except OSError as error:
        raise DamagedStateError(f"{where} could not be read: {error}") from error
    if raw is None:
        return None
    return parse_json_mapping(raw, where)


# --- the hosted store (spec 107, PR 2) ---

# The private bucket of ADR-013 decision 6, created by the storage migration.
BUCKET = "harrier-files"

# The largest object a `put` or an `append` sends, in bytes. Supabase's
# guidance for standard uploads, not a measurement of harrier's files; the
# bucket carries the same limit (spec 107, "Size"). Spec 146's import reads
# it from here.
SIZE_LIMIT = 6_000_000

# Each extension the bucket accepts and the content type an upload carries.
# Any other extension is refused before a request. Spec 146's import reads
# this table from here.
CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".html": "text/html",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
}

# Per request, chosen and not measured (spec 107, "Timeout").
REQUEST_TIMEOUT_SECONDS = 30.0

# What an upload asks of caches: nothing kept, so a read right after an
# overwrite is fresh (spec 107, Honest limitations, assumption 6).
UPLOAD_CACHE_CONTROL = "no-cache"

# What Storage means when a create-only upload finds an object there: HTTP
# 409, or HTTP 400 with "409" in the body's `statusCode` (spec 154).
CONFLICT = 409

# How many entries one listing request asks for, and how many names one
# delete request carries.
LIST_PAGE_SIZE = 100
DELETE_BATCH_SIZE = 100

# The families the code lists or orders by name, whose object names are the
# keys themselves (spec 107, "Keys and object names"). Their names are run
# ids, dates and digests. Every other family's name is hashed.
_LISTED_FAMILY = re.compile(
    r"(scoring/exports|scoring/models|scoring/reports|discovery/[^/]+/[^/]+/runs)/.+"
)


def is_listed_key(key: str) -> bool:
    """Whether `key` is in a listed family, so its object name is the key."""
    return _LISTED_FAMILY.fullmatch(key) is not None


def hashed_name(key: str) -> str:
    """The object name below the owner's prefix: the key itself for a listed
    family; otherwise the key's directory, the SHA-256 of the whole key in
    hex, and the key's extension. A file name that carries a candidate's or
    a contact's name never reaches Storage (spec 107, Open decision 5)."""
    if is_listed_key(key):
        return key
    directory, _, last = key.rpartition("/")
    hashed = hashlib.sha256(key.encode("utf-8")).hexdigest() + PurePosixPath(last).suffix
    return f"{directory}/{hashed}" if directory else hashed


def content_type_for(operation: str, key: str) -> str:
    """The content type an upload of `key` carries, or `FileStoreError`
    before any request for an extension the bucket does not accept."""
    content_type = CONTENT_TYPES.get(PurePosixPath(key).suffix)
    if content_type is None:
        raise FileStoreError(
            f"{operation} {family_of(key)}: the bucket accepts no file of this type"
        )
    return content_type


def storage_client() -> httpx.Client:
    """The HTTP client one Storage operation uses, closed after it. The
    tests replace this with one over a fake server, so no test reaches the
    network."""
    import httpx

    return httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)


# httpx and httpcore log every request at INFO with its URL, which holds the
# owner's id and the object name. Harrier's logs never name an object (spec
# 107, Honest limitations), so their request lines never reach a log.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def quiet_request_logs() -> None:
    """Hold the HTTP client's loggers at WARNING: a request line is INFO."""
    for name in HTTP_CLIENT_LOGGERS:
        logger = logging.getLogger(name)
        if logger.level < logging.WARNING:
            logger.setLevel(logging.WARNING)


def answered_status(response: httpx.Response) -> int:
    """The status Storage meant. Some Storage versions answer 400 with the
    real status as a string in the body's `statusCode` (an absent object as
    "404", a policy refusal as "403"); that one wins over the 400."""
    status = response.status_code
    if status != 400:
        return status
    try:
        body: object = response.json()
    except ValueError:
        return status
    if isinstance(body, dict):
        inner = cast("dict[str, object]", body).get("statusCode")
        if isinstance(inner, int) and not isinstance(inner, bool):
            return inner
        if isinstance(inner, str) and inner.isdigit():
            return int(inner)
    return status


def storage_error(operation: str, family: str, status: int) -> FileStoreError:
    """The error for a Storage answer that is neither success nor absence
    (spec 107, the error table). The message names the operation and the
    family, never anything Storage sent."""
    where = f"{operation} {family}"
    if status in (401, 403):
        return FileStoreDenied(f"{where}: Storage refused the credentials", status=status)
    if status == 413:
        return FileTooLarge(f"{where}: Storage refused an object over its limit", status=status)
    if status >= 500:
        return FileStoreUnavailable(f"{where}: Storage answered {status}", status=status)
    return FileStoreError(f"{where}: Storage answered {status}", status=status)


def _absent(status: int, response: httpx.Response) -> bool:
    """Whether Storage said the object is not there: 404, or the not-found
    error a 400 can carry."""
    if status == 404:
        return True
    try:
        body: object = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and cast("dict[str, object]", body).get("error") in (
        "not_found",
        "NoSuchKey",
    )


def _parse_time(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _size(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _version(etag: object, updated: object, fallback: object) -> str:
    """Storage's version of an object: its entity tag and its update time,
    which change with every overwrite."""
    tag = etag if isinstance(etag, str) and etag else str(fallback or "")
    return f"{tag}|{updated if isinstance(updated, str) else ''}"


@dataclass(frozen=True)
class StoredObject:
    """One object of an owner's prefix, as the operator lists it: its name
    below the prefix (a hashed name for most families), its size and a
    version that changes whenever its bytes may have."""

    name: str
    size: int
    version: str


@dataclass(frozen=True)
class SupabaseFileStore:
    """One owner's files in the private bucket, through Storage's HTTP API.

    Every object name is `<owner>/<name>` (`hashed_name`), the owner in its
    canonical lowercase form, so this store cannot build a name outside its
    owner's prefix. Requests go to `<origin>/storage/v1` with `apikey` and,
    for a request's binding (`for_request`), that request's bearer token.

    The operator's binding (`for_operator`) sends the operator's secret key
    in `apikey` alone, outside any request, for spec 111's erase and spec
    146's import. A secret key bypasses the row policy, so there the owner
    prefix this store builds is the only boundary.

    `http`, when given, is a client the caller owns and closes; otherwise
    each operation opens one from `storage_client()` and closes it.
    """

    owner: str
    origin: str
    apikey: str = field(repr=False)
    token: str | None = field(default=None, repr=False)
    http: httpx.Client | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        # ValueError for anything but a UUID: nothing else becomes a prefix.
        object.__setattr__(self, "owner", str(uuid.UUID(self.owner)))

    @classmethod
    def for_request(
        cls, owner: str, token: str, *, origin: str, publishable_key: str
    ) -> SupabaseFileStore:
        """The binding of a verified request: its `sub`, its own token and
        the publishable key (spec 107, "Binding a request")."""
        return cls(owner=owner, origin=origin, apikey=publishable_key, token=token)

    @classmethod
    def for_operator(
        cls, owner: str, *, origin: str, secret_key: str, http: httpx.Client | None = None
    ) -> SupabaseFileStore:
        """The operator's binding of one owner, outside any request (spec
        107, Note (2026-10-15); spec 146). Built beside a local store, never
        through `files()`."""
        return cls(owner=owner, origin=origin, apikey=secret_key, http=http)

    # --- names ---

    def object_name(self, key: str) -> str:
        """`<owner>/<name>`: the one place an object name is built."""
        return f"{self.owner}/{hashed_name(check_key(key))}"

    def local_path(self, key: str) -> Path | None:
        check_key(key)
        return None

    def qualified_name(self, key: str) -> str:
        return f"{self.owner}/{check_key(key)}"

    def shown_name(self, key: str) -> str:
        return family_of(check_key(key))

    # --- requests ---

    def _send(
        self,
        operation: str,
        family: str,
        method: str,
        path: str,
        *,
        content: bytes | None = None,
        json_body: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One request, or the error its failure maps to. A transport error
        is named by its class only: its text can quote the URL, which holds
        the owner's id."""
        import httpx

        quiet_request_logs()
        sent = {"apikey": self.apikey}
        if self.token is not None:
            sent["Authorization"] = f"Bearer {self.token}"
        sent.update(headers or {})
        url = f"{self.origin}/storage/v1{path}"

        def go(client: httpx.Client) -> httpx.Response:
            return client.request(method, url, headers=sent, content=content, json=json_body)

        try:
            if self.http is not None:
                return go(self.http)
            with storage_client() as client:
                return go(client)
        except httpx.TransportError as error:
            raise FileStoreUnavailable(
                f"{operation} {family}: Storage did not answer ({type(error).__name__})"
            ) from None
        except httpx.HTTPError as error:
            raise FileStoreError(
                f"{operation} {family}: the request failed ({type(error).__name__})"
            ) from None

    # --- the operations ---

    def read(self, key: str) -> bytes | None:
        name = self.object_name(key)
        family = family_of(key)
        response = self._send("read", family, "GET", f"/object/{BUCKET}/{name}")
        if response.status_code == 200:
            return response.content
        status = answered_status(response)
        if _absent(status, response):
            return None
        raise storage_error("read", family, status)

    def put(self, key: str, data: bytes, *, atomic: bool) -> None:
        # Whole or not at all whatever `atomic` says: Storage keeps an upload
        # complete or not at all.
        self._upload("put", key, data)

    def create(self, key: str, data: bytes) -> bool:
        """Upload `data` to `key` only when no object is there (spec 154).
        True when stored; False when Storage answered that the object
        exists, which leaves it unchanged. Not on the `FileStore` protocol:
        only the artifact import must never replace a hosted object."""
        response = self._send_upload("create", key, data, upsert=False)
        if response.status_code == 200:
            return True
        status = answered_status(response)
        if status == CONFLICT:
            return False
        raise storage_error("create", family_of(key), status)

    def _upload(self, operation: str, key: str, data: bytes) -> None:
        response = self._send_upload(operation, key, data, upsert=True)
        if response.status_code == 200:
            return
        raise storage_error(operation, family_of(key), answered_status(response))

    def _send_upload(
        self, operation: str, key: str, data: bytes, *, upsert: bool
    ) -> httpx.Response:
        name = self.object_name(key)
        family = family_of(key)
        content_type = content_type_for(operation, key)
        if len(data) > SIZE_LIMIT:
            raise FileTooLarge(f"{operation} {family}: the object is over {SIZE_LIMIT} bytes")
        return self._send(
            operation,
            family,
            "POST",
            f"/object/{BUCKET}/{name}",
            content=data,
            headers={
                "Content-Type": content_type,
                "x-upsert": "true" if upsert else "false",
                "Cache-Control": UPLOAD_CACHE_CONTROL,
            },
        )

    def append(self, key: str, text: str) -> None:
        """Read, add, upload the whole. Two appenders at once can lose one
        append (spec 107, Honest limitations)."""
        family = family_of(check_key(key))
        content_type_for("append", key)
        added = text.encode("utf-8")
        if len(added) > SIZE_LIMIT:
            raise FileTooLarge(f"append {family}: the object is over {SIZE_LIMIT} bytes")
        before = self.read(key) or b""
        self._upload("append", key, before + added)

    def delete(self, key: str) -> None:
        self._remove("delete", family_of(check_key(key)), [self.object_name(key)])

    def _remove(self, operation: str, family: str, names: Sequence[str]) -> None:
        """Remove objects by name. An absent one is not an error."""
        response = self._send(
            operation, family, "DELETE", f"/object/{BUCKET}", json_body={"prefixes": list(names)}
        )
        if response.status_code == 200:
            return
        status = answered_status(response)
        if _absent(status, response):
            return
        raise storage_error(operation, family, status)

    def stat(self, key: str) -> Entry | None:
        name = self.object_name(key)
        family = family_of(key)
        response = self._send("stat", family, "GET", f"/object/info/{BUCKET}/{name}")
        if response.status_code != 200:
            status = answered_status(response)
            if _absent(status, response):
                return None
            raise storage_error("stat", family, status)
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise FileStoreError(f"stat {family}: Storage's answer is not an object")
        info = cast("dict[str, object]", body)
        updated = info.get("updated_at") or info.get("last_modified")
        return Entry(
            key=key,
            size=_size(info.get("size")),
            mtime=_parse_time(updated),
            version=_version(info.get("etag"), updated, info.get("version")),
        )

    def _listing(self, operation: str, family: str, prefix: str) -> builtins.list[dict[str, Any]]:
        """Every entry directly under `prefix`, a full object name without
        a trailing slash, page by page."""
        entries: builtins.list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._send(
                operation,
                family,
                "POST",
                f"/object/list/{BUCKET}",
                json_body={
                    "prefix": prefix,
                    "limit": LIST_PAGE_SIZE,
                    "offset": offset,
                    "sortBy": {"column": "name", "order": "asc"},
                },
            )
            if response.status_code != 200:
                raise storage_error(operation, family, answered_status(response))
            try:
                page: object = response.json()
            except ValueError:
                page = None
            if not isinstance(page, list):
                raise FileStoreError(f"{operation} {family}: Storage's answer is not a list")
            items = cast("builtins.list[object]", page)
            entries.extend(cast("dict[str, Any]", item) for item in items if isinstance(item, dict))
            if len(items) < LIST_PAGE_SIZE:
                return entries
            offset += LIST_PAGE_SIZE

    def list(self, prefix: str) -> builtins.list[Entry]:
        """The objects directly under a listed family's prefix. A hashed
        family's names cannot be turned back into keys, so listing one is
        refused."""
        check_key(prefix)
        family = family_of(prefix)
        if not is_listed_key(f"{prefix}/x"):
            raise FileStoreError(f"list {family}: this family's names are hashed")
        result: builtins.list[Entry] = []
        for item in self._listing("list", family, f"{self.owner}/{prefix}"):
            name = item.get("name")
            # A folder has no id; only objects directly under the prefix count.
            if item.get("id") is None or not isinstance(name, str):
                continue
            if _SEGMENT.fullmatch(name) is None or name in (".", ".."):
                continue
            metadata = item.get("metadata")
            meta = cast("dict[str, object]", metadata) if isinstance(metadata, dict) else {}
            updated = item.get("updated_at")
            result.append(
                Entry(
                    key=join_key(prefix, name),
                    size=_size(meta.get("size")),
                    mtime=_parse_time(updated),
                    version=_version(meta.get("eTag"), updated, item.get("id")),
                )
            )
        return sorted(result, key=lambda entry: entry.key)

    # --- the operator's whole prefix (spec 111's erase; spec 107, Note) ---

    def _walk(self) -> builtins.list[tuple[str, dict[str, Any]]]:
        """Every object under the owner's prefix, at any depth, as its full
        object name and its listing entry. Folders are walked, never
        returned."""
        found: builtins.list[tuple[str, dict[str, Any]]] = []
        pending = [self.owner]
        while pending:
            folder = pending.pop()
            for item in self._listing("list", "the owner's prefix", folder):
                name = item.get("name")
                if not isinstance(name, str) or name in ("", ".", "..") or "/" in name:
                    continue
                full = f"{folder}/{name}"
                if item.get("id") is None:
                    pending.append(full)
                else:
                    found.append((full, item))
        return found

    def object_names(self) -> builtins.list[str]:
        """Every object under the owner's prefix, at any depth, as full
        object names. Folders are walked, never returned."""
        return sorted(full for full, _ in self._walk())

    def stored_objects(self) -> builtins.list[StoredObject]:
        """Every object under the owner's prefix, at any depth, by name below
        the prefix, with its size and a version that changes whenever its
        bytes may have (spec 153's `backup-files`)."""
        start = len(self.owner) + 1
        result: builtins.list[StoredObject] = []
        for full, item in self._walk():
            metadata = item.get("metadata")
            meta = cast("dict[str, object]", metadata) if isinstance(metadata, dict) else {}
            updated = item.get("updated_at")
            result.append(
                StoredObject(
                    name=full[start:],
                    size=_size(meta.get("size")),
                    version=_version(meta.get("eTag"), updated, item.get("id")),
                )
            )
        return sorted(result, key=lambda stored: stored.name)

    def read_object(self, name: str) -> bytes | None:
        """The bytes of an object by its name below the owner's prefix, as
        `stored_objects` gives it, or None when it is gone. The name is
        checked against the key grammar, so it cannot leave the prefix."""
        check_key(name)
        family = family_of(name)
        response = self._send("read", family, "GET", f"/object/{BUCKET}/{self.owner}/{name}")
        if response.status_code == 200:
            return response.content
        status = answered_status(response)
        if _absent(status, response):
            return None
        raise storage_error("read", family, status)

    def delete_everything(self) -> int:
        """Delete every object under the owner's prefix through the Storage
        API, never with SQL. Returns how many objects were listed and
        removed."""
        names = self.object_names()
        for start in range(0, len(names), DELETE_BATCH_SIZE):
            self._remove("delete", "the owner's prefix", names[start : start + DELETE_BATCH_SIZE])
        return len(names)


# --- a hosted run's binding (spec 107, Amendment (2026-10-13), and the
# decisions taken 2026-10-15 and 2026-10-16) ---

# What a hosted run's process finds in its environment, set by the run's
# binding (`harrier_api.runenv`): the token the run reaches Storage with, the
# project's origin and the publishable key. The last two names are
# `harrier_api.hostedauth`'s, which this package may not import.
STORAGE_TOKEN_VARIABLE = "HARRIER_STORAGE_TOKEN"
SUPABASE_URL_VARIABLE = "HARRIER_SUPABASE_URL"
PUBLISHABLE_KEY_VARIABLE = "HARRIER_SUPABASE_PUBLISHABLE_KEY"


def run_store(environ: MutableMapping[str, str]) -> SupabaseFileStore | None:
    """The store a hosted run binds before any domain code runs: its owner's
    prefix, reached with the token its environment carries.

    The token is taken out of `environ` whatever happens next, so no tool
    the run starts (Chromium, `pdfinfo`) inherits it. None when the run
    carries no token, or lacks the origin or the publishable key beside it:
    then nothing is bound, and `files()` refuses with `FileStoreUnbound` at
    the run's first file, because `HARRIER_RUN_OWNER` makes the process
    hosted. A run that touches no file still runs.
    """
    token = environ.pop(STORAGE_TOKEN_VARIABLE, "").strip()
    owner = environ.get(RUN_OWNER_VARIABLE, "")
    origin = environ.get(SUPABASE_URL_VARIABLE, "").strip().removesuffix("/")
    apikey = environ.get(PUBLISHABLE_KEY_VARIABLE, "").strip()
    if not (token and owner and origin and apikey):
        return None
    return SupabaseFileStore.for_request(owner, token, origin=origin, publishable_key=apikey)
