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


def route_ats_feeds(feed_urls: list[str]) -> dict[str, list[str]]:
    """Group board URLs by importer. Split out from the file reader so the
    same routing serves configuration read from the database (spec 023)."""
    grouped: dict[str, list[str]] = {
        "greenhouse": [],
        "ashby": [],
        "lever": [],
    }
    for feed_url in feed_urls:
        hostname = (urlparse(feed_url).hostname or "").lower()
        # Label-suffix matches, not substrings: lookalike hosts route nowhere.
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            grouped["greenhouse"].append(feed_url)
        elif hostname == "jobs.ashbyhq.com":
            grouped["ashby"].append(feed_url)
        elif hostname == "lever.co" or hostname.endswith(".lever.co"):
            grouped["lever"].append(feed_url)
    return grouped


def parse_ats_feeds(path: Path | None = None) -> dict[str, list[str]]:
    """The watchlist read from a file. The orchestrator goes through
    harrier.userconfig instead; this stays for the import path and for
    callers that genuinely mean a file."""
    feeds_path = resolve_config_path(path if path is not None else FEEDS_PATH)
    return route_ats_feeds(read_line_config(feeds_path))
