"""The project `.env` file, read one way (spec 011, spec 103).

The CLI loads `.env` from the working directory into its environment. The
store a process opens is also chosen from it, by the CLI and by the API, so
both read the file through this one parser and cannot disagree about it.
"""

from __future__ import annotations

from pathlib import Path

# The Supabase secret key's variable. Named here, in a module with no
# dependency, so the CLI's `.env` loader can skip it without importing
# `harrier.operator`, whose `httpx` is a dev dependency only and absent from
# the hosted images (spec 111, Amendment (2026-10-15)).
SECRET_KEY_VARIABLE = "HARRIER_SUPABASE_SECRET_KEY"


def read_env_file(path: Path) -> dict[str, str]:
    """Each `KEY=value` line of `path`, quotes stripped. Empty when absent.

    The first line for a key wins, as it did when the CLI's loader read the
    file itself: that loader never overwrote a key it had already set
    (review of PR #218).
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip().strip('"').strip("'")
    return values
