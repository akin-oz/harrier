"""feeds.txt reading and netloc routing (spec 008 port).

One combined config file routes ATS board URLs to the right importer; the
file format is one URL per line with # comments (product invariant).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from harrier.demo import resolve_config_path

FEEDS_PATH = Path("config") / "feeds.txt"


def read_line_config(path: Path) -> list[str]:
    if not path.is_file():
        return []
    items: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


# Where an entry that matches no importer is collected. Not an importer, and
# never iterated as one: `IMPORTER_KEYS` is what callers loop over.
UNROUTED = "unrouted"
IMPORTER_KEYS: tuple[str, ...] = ("greenhouse", "ashby", "lever")


def route_ats_feeds(feed_urls: list[str]) -> dict[str, list[str]]:
    """Group board URLs by importer, keeping the ones that match none.

    The loop had a branch per provider and no final branch, so a watchlist
    entry for anything else produced no error and no jobs, forever. For a
    single-user tool whose watchlist is edited by hand, that is the most
    likely real failure in the system: you paste a URL, nothing happens, and
    nothing says why (spec 041).

    They come back under `UNROUTED` rather than raising, because one
    unsupported entry must not stop the other sources running. Reporting is
    the caller's job, and `harrier.discovery` does it per run.
    """
    grouped: dict[str, list[str]] = {key: [] for key in IMPORTER_KEYS}
    grouped[UNROUTED] = []
    for feed_url in feed_urls:
        hostname = (urlparse(feed_url).hostname or "").lower()
        # Label-suffix matches, not substrings: lookalike hosts route nowhere.
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            grouped["greenhouse"].append(feed_url)
        elif hostname == "jobs.ashbyhq.com":
            grouped["ashby"].append(feed_url)
        elif hostname == "lever.co" or hostname.endswith(".lever.co"):
            grouped["lever"].append(feed_url)
        else:
            grouped[UNROUTED].append(feed_url)
    return grouped


def parse_ats_feeds(path: Path | None = None) -> dict[str, list[str]]:
    """The watchlist read from a file. The orchestrator goes through
    harrier.userconfig instead; this stays for the import path and for
    callers that genuinely mean a file."""
    feeds_path = resolve_config_path(path if path is not None else FEEDS_PATH)
    return route_ats_feeds(read_line_config(feeds_path))
