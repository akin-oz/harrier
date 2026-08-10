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


def parse_ats_feeds(path: Path | None = None) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {
        "greenhouse": [],
        "ashby": [],
        "lever": [],
    }
    feeds_path = resolve_config_path(path if path is not None else FEEDS_PATH)
    for feed_url in read_line_config(feeds_path):
        hostname = (urlparse(feed_url).hostname or "").lower()
        # Label-suffix matches, not substrings: lookalike hosts route nowhere.
        if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
            grouped["greenhouse"].append(feed_url)
        elif hostname == "jobs.ashbyhq.com":
            grouped["ashby"].append(feed_url)
        elif hostname == "lever.co" or hostname.endswith(".lever.co"):
            grouped["lever"].append(feed_url)
    return grouped
