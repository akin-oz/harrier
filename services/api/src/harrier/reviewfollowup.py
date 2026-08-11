"""Waiting out a rate-limited review, so nobody does it by hand (spec 043).

The review service rate-limits this repository. When it does it posts a
notice saying how long to wait, and the pull request keeps its green checks:
the check reports success with the reason "Review rate limited", which is a
guard reporting success while doing nothing. Six open pull requests were in
that state at once, with zero review threads between them, while their checks
all read as passing.

This module is the mechanical half: read the newest notice, work out the
wait, decide whether asking again is worth anything, and say so. It never
edits code and it is not wired to anything that does. Reading what comes back
is judgement, and that lives in `.ai/rules/review-response.md` where every
session compiles it.

Two guards matter more than the feature:

- **A daily bound.** Without one, a repository that stays rate-limited posts
  a comment every hour forever: noise on the author's own pull request and
  load on somebody else's service.
- **Only when there is something to review.** Asking again with an unchanged
  head repeats a review that already happened. The exception is a review cut
  short by the limit, which is the case this exists for.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir

# The notice's own markers. Matched rather than assumed: the format was read
# off a live pull request before any of this was written.
NOTICE_MARKER = "rate limited by coderabbit.ai"
NOTICE_SPLIT = "<!-- This is an auto-generated comment:"

WAIT_PATTERN = re.compile(r"Next review available in:\**\s*\*+\s*([^*]+?)\s*\*+", re.IGNORECASE)
UNIT_PATTERN = re.compile(r"(\d+)\s*([a-z]+)", re.IGNORECASE)
UNIT_MINUTES = {"minute": 1, "minutes": 1, "hour": 60, "hours": 60}

REQUEST_COMMENT = "@coderabbitai review"

# A minute past the stated wait. Asking at the exact boundary races the
# service's own clock and earns another notice.
GRACE_MINUTES = 1

DEFAULT_DAILY_LIMIT = 6
STATE_FILENAME = "review-followup.json"


class FollowUpError(RuntimeError):
    """The state of a pull request could not be established."""


def parse_wait_minutes(text: str) -> int | None:
    """Minutes from a notice, or None when it carries no wait.

    Handles minutes, hours and the combined form, because the notice uses
    whichever fits. None rather than a default: a parse that cannot find a
    wait must say so, not guess one and act on it.
    """
    match = WAIT_PATTERN.search(text)
    if not match:
        return None
    total = 0
    for value, unit in UNIT_PATTERN.findall(match.group(1).lower()):
        total += int(value) * UNIT_MINUTES.get(unit, 0)
    return total or None


def newest_notice(comment_bodies: list[str]) -> str | None:
    """The most recent rate-limit notice, or None.

    Newest matters. An older notice has usually expired, and acting on it
    re-requests immediately for no reason.
    """
    notices = [
        block
        for body in comment_bodies
        for block in body.split(NOTICE_SPLIT)
        if NOTICE_MARKER in block
    ]
    return notices[-1] if notices else None


@dataclass(frozen=True)
class PullRequestState:
    """What the follow-up needs to know, gathered by the caller."""

    number: int
    head_sha: str
    review_threads: int
    comment_bodies: list[str]
    last_reviewed_sha: str = ""

    @property
    def reviewed(self) -> bool:
        """Whether anything has actually reviewed this.

        Zero threads is the state six pull requests were in while their
        checks read as passing, so this is the distinction the check itself
        does not draw.
        """
        return self.review_threads > 0


@dataclass(frozen=True)
class Decision:
    action: str
    wait_minutes: int = 0
    reason: str = ""

    def describe(self, number: int) -> str:
        if self.action == "wait":
            return f"PR #{number}: rate limited, {self.wait_minutes} minutes to wait"
        return f"PR #{number}: {self.reason}"


WAIT = "wait"
REQUEST = "request"
SKIP = "skip"


def decide(state: PullRequestState, *, requests_today: int, daily_limit: int) -> Decision:
    """Whether to ask again, wait, or leave it alone."""
    if requests_today >= daily_limit:
        return Decision(SKIP, reason=f"already asked {requests_today} times today")

    notice = newest_notice(state.comment_bodies)
    if notice is not None:
        wait = parse_wait_minutes(notice)
        if wait:
            return Decision(WAIT, wait_minutes=wait + GRACE_MINUTES, reason="rate limited")
        # A notice with no readable wait. Reported rather than guessed at.
        return Decision(SKIP, reason="a rate-limit notice carried no readable wait")

    if not state.reviewed:
        return Decision(REQUEST, reason="nothing has reviewed this yet")

    if state.last_reviewed_sha and state.last_reviewed_sha == state.head_sha:
        return Decision(SKIP, reason="already reviewed at the current head")

    return Decision(REQUEST, reason="the head has moved since the last review")


# --- how often it has asked ---------------------------------------------------


def state_path() -> Path:
    return data_dir() / STATE_FILENAME


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def load_counts() -> dict[str, int]:
    """Requests made today, per pull request.

    Only today's are kept: the bound is a daily one, so yesterday's counts
    are not just useless but actively wrong to carry forward.
    """
    # Plain read and write, deliberately. This is a counter, not the tracker:
    # the worst case of losing it is a few extra requests, bounded by the
    # daily limit, so it does not warrant the durable-state machinery spec 040
    # adds for the seen state and the mail watch. When that lands this can
    # move onto it, and the failure behaviour will not change.
    path = state_path()
    if not path.is_file():
        return {}
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    record = cast("dict[str, Any]", parsed)
    if record.get("date") != _today():
        return {}
    raw_counts = record.get("counts")
    if not isinstance(raw_counts, dict):
        return {}
    counts = cast("dict[str, Any]", raw_counts)
    return {str(key): int(value) for key, value in counts.items()}


def record_request(number: int) -> int:
    counts = load_counts()
    counts[str(number)] = counts.get(str(number), 0) + 1
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": _today(), "counts": counts}, indent=2), encoding="utf-8")
    return counts[str(number)]


# --- the gh seam --------------------------------------------------------------

GitHubRunner = Callable[[list[str]], str]


def gather(number: int, run: GitHubRunner, *, owner: str, repo: str) -> PullRequestState:
    """Everything the decision needs, in three calls.

    `run` is injected so the tests never touch the network and never need a
    token, which is also what keeps a pull request title out of a fixture
    (ADR-008).
    """
    try:
        bodies_raw = run(
            ["api", f"repos/{owner}/{repo}/issues/{number}/comments", "--jq", ".[].body"]
        )
        # --repo explicitly. `gh pr` resolves the repository from the working
        # directory, so without it this works when run from a checkout and
        # fails with "not a git repository" anywhere else, including from a
        # scheduled job. Found by running it from a temporary directory, which
        # is exactly where a scheduled job would run it.
        head = run(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                f"{owner}/{repo}",
                "--json",
                "headRefOid",
                "--jq",
                ".headRefOid",
            ]
        ).strip()
        threads_raw = run(
            [
                "api",
                "graphql",
                "-f",
                f'query={{repository(owner:"{owner}",name:"{repo}")'
                f"{{pullRequest(number:{number}){{reviewThreads(first:100){{nodes{{id}}}}}}}}}}",
                "--jq",
                ".data.repository.pullRequest.reviewThreads.nodes|length",
            ]
        )
    except Exception as error:
        raise FollowUpError(f"could not read pull request {number}: {error}") from error

    try:
        threads = int(threads_raw.strip() or 0)
    except ValueError as error:
        raise FollowUpError(f"unexpected thread count for {number}: {threads_raw!r}") from error

    return PullRequestState(
        number=number,
        head_sha=head,
        review_threads=threads,
        comment_bodies=[line for line in bodies_raw.split("\n") if line] or [bodies_raw],
    )


def request_review(number: int, run: GitHubRunner, *, owner: str, repo: str) -> None:
    """Ask for another review. `--repo` for the same reason as above."""
    run(["pr", "comment", str(number), "--repo", f"{owner}/{repo}", "--body", REQUEST_COMMENT])


def unresolved_threads(number: int, run: GitHubRunner, *, owner: str, repo: str) -> int:
    raw = run(
        [
            "api",
            "graphql",
            "-f",
            f'query={{repository(owner:"{owner}",name:"{repo}")'
            f"{{pullRequest(number:{number}){{reviewThreads(first:100)"
            f"{{nodes{{isResolved}}}}}}}}}}",
            "--jq",
            "[.data.repository.pullRequest.reviewThreads.nodes[]|select(.isResolved==false)]|length",
        ]
    )
    return int(raw.strip() or 0)


def report(states: list[PullRequestState]) -> list[str]:
    """One line per pull request, drawing the distinction the check does not.

    "reviewed" and "rate limited" read identically in the check's own status,
    which is the defect this spec exists for.
    """
    lines: list[str] = []
    for state in states:
        if state.reviewed:
            lines.append(f"PR #{state.number}: reviewed, {state.review_threads} threads")
        elif newest_notice(state.comment_bodies) is not None:
            lines.append(f"PR #{state.number}: NOT REVIEWED, rate limited")
        else:
            lines.append(f"PR #{state.number}: NOT REVIEWED, no review and no notice")
    return lines
