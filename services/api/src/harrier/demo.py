"""Demo mode: the switch, and the two substitutions it makes (spec 021).

A stranger clones the repo and runs `just demo`. None of the never-in-git
files exist on that machine and no keys are set, so demo mode substitutes
two things and nothing else:

1. Configuration falls back to the committed `.example` sibling of any
   never-in-git config file (ADR-009). The real file wins whenever it
   exists, so a demo run on the owner's machine still reads their config
   only if they have one.
2. Outbound HTTP is served from `fixtures/http/` instead of the network.
   A URL with no fixture raises rather than falling through to a request,
   which is what makes "the demo touches no network" a provable claim
   (test_demo.py::test_unfixtured_url_raises_instead_of_reaching_network).

Demo mode never changes screening, scoring, or persistence: the point is
that a stranger sees the real pipeline, on synthetic inputs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

DEMO_ENV = "HARRIER_DEMO"
HTTP_FIXTURES_ENV = "HARRIER_HTTP_FIXTURES"
FIXTURE_INDEX_NAME = "index.json"
DEMO_DIR_NAME = "harrier-demo"


class OfflineFixtureError(RuntimeError):
    """A URL was requested that the offline fixture set does not cover."""


def is_demo_mode() -> bool:
    return os.environ.get(DEMO_ENV, "") == "1"


def repo_root() -> Path:
    # services/api/src/harrier/demo.py -> the repo root is four parents up.
    return Path(__file__).resolve().parents[4]


def fixtures_dir() -> Path:
    return repo_root() / "fixtures"


def demo_data_dir() -> Path:
    """Where a demo run writes. Outside the clone on purpose: a stranger's
    checkout must be unchanged after the demo (spec 021 acceptance)."""
    return Path(tempfile.gettempdir()) / DEMO_DIR_NAME


def http_fixtures_dir() -> Path | None:
    """Where offline HTTP responses come from, or None to use the network."""
    override = os.environ.get(HTTP_FIXTURES_ENV, "").strip()
    if override:
        return Path(override)
    return fixtures_dir() / "http" if is_demo_mode() else None


def example_path_for(path: Path) -> Path:
    """config/feeds.txt -> config/feeds.example.txt."""
    return path.with_name(f"{path.stem}.example{path.suffix}")


def anchored_path(path: Path) -> Path:
    """A committed file's path, found even when the working directory is not
    the repo root. Config paths are written repo-relative because production
    runs from there; tests, and a stranger running from anywhere, do not."""
    if path.is_absolute() or path.is_file():
        return path
    candidate = repo_root() / path
    return candidate if candidate.is_file() else path


def resolve_config_path(path: Path) -> Path:
    """The real config file, or its committed example when in demo mode.

    Outside demo mode the path is returned unchanged, so this sits on the
    normal read path without altering it. Inside demo mode the example wins
    even when the real file exists: a demo has to show the same thing on
    every machine, and the owner's own watchlist is exactly the personal
    data the demo must not display (ADR-009).
    """
    if not is_demo_mode():
        return path
    example = anchored_path(example_path_for(path))
    return example if example.is_file() else path
