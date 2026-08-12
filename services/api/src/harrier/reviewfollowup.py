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
import os
import re
from collections.abc import Callable, Iterable
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

# Whose word we are waiting on. Compared case-insensitively and by prefix,
# because the same reviewer appears as `coderabbitai` and `coderabbitai[bot]`
# depending on which API answered.
REVIEWER_LOGIN = "coderabbitai"

# A review body saying some of its findings could not be attached to a line.
# Those findings exist only in the body: no thread is created for them, so a
# query over `reviewThreads` is blind to them however it filters. Not
# hypothetical. A Major finding on PR #37 arrived this way and was missed by
# this very loop, which is why review bodies are read rather than counted.
OUTSIDE_DIFF_MARKER = "outside the diff"
ACTIONABLE_PATTERN = re.compile(r"Actionable comments posted:\s*(\d+)", re.IGNORECASE)

# A minute past the stated wait. Asking at the exact boundary races the
# service's own clock and earns another notice.
GRACE_MINUTES = 1

DEFAULT_DAILY_LIMIT = 6
STATE_FILENAME = "review-followup.json"
HANDLED_FILENAME = "review-followup-handled.json"


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
class ThreadState:
    """One review thread, and whether the last word in it is ours.

    A first finding and a reply to our answer are the same shape: the
    reviewer speaks, and we owe a response. So the question is who spoke
    last, not whether the thread is resolved. Resolving is what hides the
    reply that arrives afterwards, which makes `isResolved` the one field a
    follow-up check must not filter on.
    """

    identifier: str
    resolved: bool
    last_author: str
    last_comment_id: str

    @property
    def awaits_us(self) -> bool:
        return self.last_author.lower().startswith(REVIEWER_LOGIN)


@dataclass(frozen=True)
class ReviewBody:
    """One posted review, and what its own summary says it holds.

    Read rather than counted. A review can say "Actionable comments posted:
    4" and then note that some of them are outside the diff and could not be
    posted inline; those live here and in no thread at all.
    """

    identifier: str
    author: str
    body: str

    @property
    def from_reviewer(self) -> bool:
        return self.author.lower().startswith(REVIEWER_LOGIN)

    @property
    def actionable_count(self) -> int:
        match = ACTIONABLE_PATTERN.search(self.body)
        return int(match.group(1)) if match else 0

    @property
    def has_findings_outside_the_diff(self) -> bool:
        return OUTSIDE_DIFF_MARKER in self.body.lower()


def threads_awaiting_reply(threads: list[ThreadState], handled: set[str]) -> list[ThreadState]:
    """Threads whose last comment is the reviewer's and is new to us.

    Keyed on the comment rather than the thread, because a thread we have
    already answered can gain another reply and must come back.
    """
    return [
        thread for thread in threads if thread.awaits_us and thread.last_comment_id not in handled
    ]


def reviews_needing_a_read(reviews: list[ReviewBody], handled: set[str]) -> list[ReviewBody]:
    """Reviews from the reviewer that carry findings and are new to us.

    A review with no actionable comments and nothing outside the diff is a
    summary; surfacing it every cycle would stop the loop ever settling.
    """
    return [
        review
        for review in reviews
        if review.from_reviewer
        and review.identifier not in handled
        and (review.actionable_count > 0 or review.has_findings_outside_the_diff)
    ]


@dataclass(frozen=True)
class PullRequestState:
    """What the follow-up needs to know, gathered by the caller."""

    number: int
    head_sha: str
    review_threads: int
    comment_bodies: list[str]
    last_reviewed_sha: str = ""
    reviews_seen: int = 0
    # Threads whose last comment is the reviewer's and which we have not
    # answered, and reviews whose body we have not read. Both are empty for a
    # pull request that is genuinely settled, and both are invisible to a
    # count of unresolved threads.
    awaiting: tuple[ThreadState, ...] = ()
    unread_reviews: tuple[ReviewBody, ...] = ()

    @property
    def reviewed(self) -> bool:
        """Whether anything has actually reviewed this.

        Zero threads is the state six pull requests were in while their
        checks read as passing, so this is the distinction the check itself
        does not draw. A review that posted only findings outside the diff
        creates no threads either, so reviews count too: otherwise a reviewed
        pull request reads as unreviewed and gets asked again.
        """
        return self.review_threads > 0 or self.reviews_seen > 0

    @property
    def outstanding(self) -> bool:
        """Whether anything is waiting on us."""
        return bool(self.awaiting) or bool(self.unread_reviews)


@dataclass(frozen=True)
class Decision:
    action: str
    wait_minutes: int = 0
    reason: str = ""

    def describe(self, number: int) -> str:
        if self.action == WAIT:
            return f"PR #{number}: rate limited, {self.wait_minutes} minutes to wait"
        return f"PR #{number}: {self.reason}"


WAIT = "wait"
REQUEST = "request"
SKIP = "skip"
RESPOND = "respond"


def decide(state: PullRequestState, *, requests_today: int, daily_limit: int) -> Decision:
    """Whether to answer, ask again, wait, or leave it alone.

    Answering comes first, and before the daily bound. Asking for a fresh
    review while findings sit unanswered spends a rate-limited request on a
    conversation we have not finished, and the answer is the part that
    changes the code.
    """
    if state.outstanding:
        parts: list[str] = []
        if state.awaiting:
            parts.append(f"{len(state.awaiting)} thread(s) awaiting a reply")
        if state.unread_reviews:
            hidden = sum(
                1 for review in state.unread_reviews if review.has_findings_outside_the_diff
            )
            parts.append(f"{len(state.unread_reviews)} unread review(s)")
            if hidden:
                parts.append(f"{hidden} carrying findings outside the diff")
        return Decision(RESPOND, reason="; ".join(parts))

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


# --- what we have already read ------------------------------------------------
#
# Keyed on comment and review ids, not on thread state. `isResolved` is
# exactly the wrong key: resolving a thread is what hides the reply that
# arrives afterwards. An id recorded here is one we have read; anything else
# is new, whatever the thread now says about itself.
#
# Separate from the daily counts and never expired. A count is about today.
# "Have I read this" is permanent, and forgetting it would resurface findings
# already answered, every cycle, forever.


def handled_path() -> Path:
    return data_dir() / HANDLED_FILENAME


def load_handled() -> set[str]:
    path = handled_path()
    if not path.is_file():
        return set()
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A damaged record means re-reading things already answered, which is
        # noise. Losing a finding would be worse, so this fails towards
        # showing too much.
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in cast("list[object]", parsed)}


def record_handled(identifiers: Iterable[str]) -> set[str]:
    """Mark these comments and reviews as read. Returns the whole set."""
    handled = load_handled() | {value for value in identifiers if value}
    path = handled_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.partial")
    scratch.write_text(json.dumps(sorted(handled), indent=2), encoding="utf-8")
    os.replace(scratch, path)
    return handled


# --- the gh seam --------------------------------------------------------------

GitHubRunner = Callable[[list[str]], str]


def _as_dict(value: object) -> dict[str, object]:
    """A mapping, or an empty one. GraphQL nulls arrive as None everywhere."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


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
        # One query for everything a follow-up decision needs. Threads carry
        # the id and login of their *last* comment, because who spoke last is
        # the question. Reviews carry their bodies, because a finding that
        # could not be attached to a line exists only there, and the commit
        # they looked at, because that is how "already reviewed at this head"
        # is decided.
        detail_raw = run(
            [
                "api",
                "graphql",
                "-f",
                f'query={{repository(owner:"{owner}",name:"{repo}")'
                f"{{pullRequest(number:{number}){{"
                f"reviewThreads(first:100){{nodes{{id isResolved "
                f"comments(last:1){{nodes{{id author{{login}}}}}}}}}} "
                f"reviews(last:20){{nodes{{id author{{login}} body commit{{oid}}}}}}"
                f"}}}}}}",
            ]
        )
    except Exception as error:
        raise FollowUpError(f"could not read pull request {number}: {error}") from error

    try:
        payload: object = json.loads(detail_raw)
        pull = _as_dict(
            _as_dict(_as_dict(_as_dict(payload).get("data")).get("repository")).get("pullRequest")
        )
        thread_nodes = _as_list(_as_dict(pull.get("reviewThreads")).get("nodes"))
        review_nodes = _as_list(_as_dict(pull.get("reviews")).get("nodes"))
    except json.JSONDecodeError as error:
        raise FollowUpError(f"unexpected review payload for {number}: {error}") from error

    threads: list[ThreadState] = []
    for raw_thread in thread_nodes:
        node = _as_dict(raw_thread)
        comments = _as_list(_as_dict(node.get("comments")).get("nodes"))
        last = _as_dict(comments[-1]) if comments else {}
        threads.append(
            ThreadState(
                identifier=str(node.get("id", "")),
                resolved=bool(node.get("isResolved")),
                last_author=str(_as_dict(last.get("author")).get("login", "")),
                last_comment_id=str(last.get("id", "")),
            )
        )

    reviews: list[ReviewBody] = []
    reviewed_sha = ""
    for raw_review in review_nodes:
        node = _as_dict(raw_review)
        author = str(_as_dict(node.get("author")).get("login", ""))
        reviews.append(
            ReviewBody(
                identifier=str(node.get("id", "")),
                author=author,
                body=str(node.get("body") or ""),
            )
        )
        # The sha the newest review from the reviewer actually looked at.
        # This was never populated, so the "already reviewed at the current
        # head" branch could not fire and the loop asked again every cycle.
        # Its tests passed because they built the state by hand rather than
        # going through here.
        if author.lower().startswith(REVIEWER_LOGIN):
            reviewed_sha = str(_as_dict(node.get("commit")).get("oid", "")) or reviewed_sha

    handled = load_handled()
    return PullRequestState(
        number=number,
        head_sha=head,
        review_threads=len(threads),
        comment_bodies=[line for line in bodies_raw.split("\n") if line] or [bodies_raw],
        last_reviewed_sha=reviewed_sha,
        reviews_seen=sum(1 for review in reviews if review.from_reviewer),
        awaiting=tuple(threads_awaiting_reply(threads, handled)),
        unread_reviews=tuple(reviews_needing_a_read(reviews, handled)),
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
    """One line per pull request, drawing the distinctions the check does not.

    "reviewed" and "rate limited" read identically in the check's own status,
    which is the defect this spec exists for. "reviewed" and "reviewed, and it
    said something nobody has answered" also read identically, which is the
    defect found by using this loop: a reply in a resolved thread and a
    finding in a review body are both invisible to a count of unresolved
    threads, and one of each was missed.
    """
    lines: list[str] = []
    for state in states:
        if state.outstanding:
            detail: list[str] = []
            if state.awaiting:
                detail.append(f"{len(state.awaiting)} thread(s) awaiting a reply")
            if state.unread_reviews:
                detail.append(f"{len(state.unread_reviews)} unread review(s)")
            hidden = [r for r in state.unread_reviews if r.has_findings_outside_the_diff]
            if hidden:
                detail.append(
                    f"{len(hidden)} with findings OUTSIDE THE DIFF, which no thread carries"
                )
            lines.append(f"PR #{state.number}: NEEDS A REPLY: {'; '.join(detail)}")
        elif state.reviewed:
            lines.append(
                f"PR #{state.number}: reviewed, {state.review_threads} threads, nothing outstanding"
            )
        elif newest_notice(state.comment_bodies) is not None:
            lines.append(f"PR #{state.number}: NOT REVIEWED, rate limited")
        else:
            lines.append(f"PR #{state.number}: NOT REVIEWED, no review and no notice")
    return lines


def outstanding_identifiers(state: PullRequestState) -> list[str]:
    """Every id that answering this round would mark as read."""
    return [thread.last_comment_id for thread in state.awaiting] + [
        review.identifier for review in state.unread_reviews
    ]
