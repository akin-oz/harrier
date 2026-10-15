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
over the local default. The hosted store and the binding of a request are
spec 107's second pull request.
"""

from __future__ import annotations

import builtins
import re
import stat as stat_module
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from harrier.atomicio import DamagedStateError, parse_json_mapping, write_bytes_atomic
from harrier.db import data_dir

# One segment of a key: names come from `slugify`, a hex digest, a track id
# or a literal in the code, and every one of them fits.
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")


class InvalidFileKey(ValueError):
    """A key outside the grammar: refused before any file is touched."""


class FileStoreError(OSError):
    """A store could not do what it was asked.

    An `OSError`, so every caller's existing `except OSError` keeps its
    meaning whichever store answered.
    """


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


@dataclass(frozen=True)
class Entry:
    """One file: its key, its size, when it last changed, and a version that
    changes whenever its content may have."""

    key: str
    size: int
    mtime: float
    version: tuple[int, int, int] | str

    @property
    def name(self) -> str:
        return key_name(self.key)


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
            entry = self.stat(join_key(prefix, child.name))
            if entry is not None:
                entries.append(entry)
        return sorted(entries, key=lambda entry: entry.key)

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def qualified_name(self, key: str) -> str:
        return str(self._path(key))


_bound: ContextVar[FileStore | None] = ContextVar("harrier_files", default=None)


def files() -> FileStore:
    """The store for this context: the bound one, else the local data
    directory."""
    store = _bound.get()
    if store is not None:
        return store
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
        raise FileStoreError(f"{key.split('/', 1)[0]} has no local file in this store")
    return path


def load_text(key: str) -> str | None:
    """The file decoded as UTF-8, or None when absent."""
    data = files().read(key)
    return None if data is None else data.decode("utf-8")


def save_text(key: str, text: str, *, atomic: bool) -> None:
    """`text` encoded as UTF-8, written as `put` writes."""
    files().put(key, text.encode("utf-8"), atomic=atomic)


def load_mapping(key: str) -> dict[str, Any] | None:
    """The JSON object in a state file, None if there is none, or raise.

    `read_json_mapping`'s rules (spec 040) over the seam: a read that fails
    is `DamagedStateError`, never an empty state.
    """
    store = files()
    where = store.qualified_name(key)
    try:
        raw = store.read(key)
    except OSError as error:
        raise DamagedStateError(f"{where} could not be read: {error}") from error
    if raw is None:
        return None
    return parse_json_mapping(raw, where)
