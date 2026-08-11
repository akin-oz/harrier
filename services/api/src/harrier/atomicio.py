"""State files that survive a crash, and damage that is reported (spec 040).

Two state files were written with a plain whole-file write: the mail watch's
record of which messages it has processed, and screening's record of which
postings it has judged. A crash between truncate and flush leaves half a
file, and both readers swallowed the parse error and returned an empty
value.

An empty value is the worst possible reading. For the seen state it means
every posting looks new, so the next run re-offers everything and fires a
burst of notifications. For the mail watch it means every message looks
unprocessed. In both cases the run that follows then *writes* over the
damaged file, so the original is unrecoverable and nothing ever says why.

Two functions, used by both:

- `write_json_atomic` writes a temporary file in the same directory and
  renames it. A rename within a filesystem is atomic, so a crash leaves
  either the old file or the new one, never half of either.
- `read_json_mapping` raises `DamagedStateError` rather than returning empty.
  A caller that genuinely wants to continue can catch it, but it has to say
  so, and the absence of a file is still distinguished from damage to one.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast


class DamagedStateError(RuntimeError):
    """A state file exists but cannot be read as what it should be.

    Distinct from the file being absent, which is an ordinary first run.
    """


def write_json_atomic(path: Path, payload: object) -> None:
    """Write, then rename. Never truncate the file the reader depends on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            # The rename is atomic, but renaming a file whose contents are
            # still in the page cache atomically points at nothing after a
            # power loss. fsync before it, or this is theatre.
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_json_mapping(path: Path) -> dict[str, Any] | None:
    """The object in this file, None if there is no file, or raise.

    Damage raises instead of reading as empty, because empty is a meaningful
    and wrong answer for every caller here: it says "nothing has happened
    yet" about a system where a great deal has.
    """
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DamagedStateError(f"{path} could not be read: {error}") from error
    if not raw.strip():
        raise DamagedStateError(f"{path} is empty; it was probably truncated mid-write")
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError as error:
        raise DamagedStateError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise DamagedStateError(f"{path} is {type(parsed).__name__}, expected an object")
    return cast("dict[str, Any]", parsed)
