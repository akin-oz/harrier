"""feeds.txt reading and netloc routing (spec 008 port).

One combined config file routes ATS board URLs to the right importer; the
file format is one URL per line with # comments (product invariant).
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
# Where an entry naming a board already listed is collected (spec 124). Not
# an importer either.
DUPLICATE = "duplicate"
IMPORTER_KEYS: tuple[str, ...] = ("greenhouse", "ashby", "lever")


def board_identity(url: str) -> tuple[str, str] | None:
    """The board a watchlist entry names, or None when no importer routes it
    (spec 124).

    The host decides the provider as `route_ats_feeds` routes it. Every
    Greenhouse host shares one identity, because discovery fetches each of
    them from one API (`fetch_greenhouse_jobs`). Lever's EU host keeps its
    own, because discovery fetches it from a different API
    (`extract_lever_api_base`). The board name is the first path segment,
    lowercased; a query, a fragment, a trailing slash or a longer path does
    not change it.
    """
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    hostname = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    board = parts[0].lower()
    if hostname == "greenhouse.io" or hostname.endswith(".greenhouse.io"):
        return ("greenhouse", board)
    if hostname == "jobs.ashbyhq.com":
        return ("ashby", board)
    if hostname == "jobs.eu.lever.co":
        return ("lever-eu", board)
    if hostname == "lever.co" or hostname.endswith(".lever.co"):
        return ("lever", board)
    return None


@dataclass(frozen=True)
class CollapsedFeeds:
    """A watchlist with each board once: what was kept, in order, and each
    dropped entry with the kept entry it named the same board as."""

    kept: list[str] = field(default_factory=list[str])
    dropped: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def lines(self) -> list[str]:
        return [f"dropped {url}: the same board as {kept}" for url, kept in self.dropped]


def collapse_feeds(urls: list[str]) -> CollapsedFeeds:
    """Keep the first entry of each board, spelled as written, in order
    (spec 124). An entry with no board identity compares as written,
    trimmed, so only an exact repeat of it is a duplicate."""
    first: dict[tuple[str, str], str] = {}
    result = CollapsedFeeds()
    for url in urls:
        identity = board_identity(url) or ("", url.strip())
        kept = first.get(identity)
        if kept is None:
            first[identity] = url
            result.kept.append(url)
        else:
            result.dropped.append((url, kept))
    return result


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

    Each board is routed once: an entry naming a board already listed goes
    under `DUPLICATE`, so a file watchlist is fetched once per board too
    (spec 124).
    """
    grouped: dict[str, list[str]] = {key: [] for key in IMPORTER_KEYS}
    grouped[UNROUTED] = []
    collapsed = collapse_feeds(feed_urls)
    grouped[DUPLICATE] = [url for url, _ in collapsed.dropped]
    for feed_url in collapsed.kept:
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
