"""Where the repository is (spec 038).

One definition, because there were two. `demo.py` resolved the repository
root to find its fixtures and `schedule.py` resolved it again to write
`WorkingDirectory` into the generated launchd jobs. The two agreed only by
coincidence: both counted the same number of parents from a file at the same
depth, so moving either module would have pointed the scheduler at a
directory that does not contain the code, silently, with every job still
reporting success.

Kept in its own module rather than in either caller so neither owns it, and
so the parent count is stated once next to the reason it is that number.
"""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """The repository root, resolved from this file and never hardcoded.

    This module lives at services/api/src/harrier/paths.py, so the root is
    four parents up. Moving it changes that number, which is why every caller
    imports this rather than counting for itself.
    """
    return Path(__file__).resolve().parents[4]
