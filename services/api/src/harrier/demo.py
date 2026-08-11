"""Demo mode: the switch, and the four substitutions it makes (spec 021).

A stranger clones the repo and runs `just demo`. None of the never-in-git
files exist on that machine and no keys are set, so demo mode substitutes
these and nothing else:

1. Configuration reads the committed `.example` sibling of a never-in-git
   config file (ADR-009). The example wins even when the real file exists:
   a demo has to show the same thing on every machine, and the owner's own
   watchlist is exactly the personal data it must not display. Proof:
   test_demo.py::test_demo_mode_reads_the_committed_example_even_when_a_real_config_exists.
2. Outbound HTTP is served from `fixtures/http/` instead of the network.
   A URL with no fixture raises rather than falling through to a request,
   which is what makes "the demo touches no network" a provable claim
   (test_demo.py::test_unfixtured_url_raises_instead_of_reaching_network).
3. Writes go to a temp directory rather than the clone, because data_dir()
   resolves to demo_data_dir() (harrier/db.py). Proof:
   test_demo.py::test_demo_writes_nothing_into_the_clone.
4. Outbound notifications and mailbox reads refuse rather than run
   (harrier/notify.py, harrier/mail/watch.py). Without this a demo run on
   a machine that does hold Telegram or Gmail credentials would message a
   real chat, or read a real inbox, with synthetic data. Proof:
   test_demo.py::test_demo_never_sends_telegram_even_with_credentials_present,
   test_demo.py::test_demo_refuses_to_read_a_real_mailbox.

Screening and scoring are untouched: the point is that a stranger sees the
real pipeline, on synthetic inputs.

Honest limitation: this covers HTTP that goes through
harrier.screening.http, which is every ATS and RemoteOK fetch. Apify builds
its own requests and is skipped in demo mode instead (harrier/discovery.py).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from harrier.paths import repo_root

DEMO_ENV = "HARRIER_DEMO"
HTTP_FIXTURES_ENV = "HARRIER_HTTP_FIXTURES"
FIXTURE_INDEX_NAME = "index.json"
DEMO_DIR_NAME = "harrier-demo"


class OfflineFixtureError(RuntimeError):
    """A URL was requested that the offline fixture set does not cover."""


def is_demo_mode() -> bool:
    return os.environ.get(DEMO_ENV, "") == "1"


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
    """A committed file's path, resolved against the repo rather than the
    working directory.

    Config paths are written repo-relative because production runs from the
    repo root; tests, and a stranger running from anywhere, do not. Trying
    the working directory first would let a `config/` tree in whatever
    directory the demo was launched from win over the committed one, putting
    unknown configuration in front of a stranger (review finding on PR #18).
    """
    return path if path.is_absolute() else repo_root() / path


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
