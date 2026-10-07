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
import math
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

# The phrase before the wait, in each wording the service has used: "Next
# review available in: **38 minutes**" when this was written, and "Next
# included review available in 52 minutes." since (spec 043 amendment). The
# wait is whatever follows the phrase on its line.
WAIT_PATTERN = re.compile(
    r"Next\s+(?:included\s+)?review\s+available\s+in:?([^\n]*)", re.IGNORECASE
)
UNIT_PATTERN = re.compile(r"(\d+)\s*([a-z]+)", re.IGNORECASE)
UNIT_MINUTES = {"minute": 1, "minutes": 1, "hour": 60, "hours": 60}

REQUEST_COMMENT = "@coderabbitai review"

# A review the service is still writing. Asking again then buys a second
# review of the same commits, or a notice (spec 043 amendment).
IN_PROGRESS_MARKER = "review in progress by coderabbit.ai"
# A completed review with no findings creates no review object, only this
# line in the service's summary comment, beside the commits it covered. A
# rate-limit notice carries the same commit line, so only this one counts.
CLEAN_REVIEW_MARKER = "No actionable comments were generated"
REVIEWED_RANGE_PATTERN = re.compile(r"between ([0-9a-f]{7,40}) and ([0-9a-f]{7,40})")

# Whose word we are waiting on. Compared case-insensitively and by prefix,
# because the same reviewer appears as `coderabbitai` and `coderabbitai[bot]`
# depending on which API answered.
REVIEWER_LOGIN = "coderabbitai"

# What is read from each page of the reviews connection. The cursor is how an
# earlier page is asked for, because a reply in a thread is a review node and
# enough of them push the review they answer off the newest page.
REVIEW_FIELDS = "pageInfo{hasPreviousPage startCursor} nodes{id author{login} body commit{oid}}"
# Earlier pages read before giving up and failing closed. Each holds a
# hundred nodes, so this reads a thousand behind the newest twenty.
REVIEW_PAGE_LIMIT = 10

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
    # The GitHub connections this is gathered from are bounded. If either had
    # another page, what we did not read may hold the finding, so the honest
    # answer is "something may be waiting" rather than "nothing is".
    truncated: bool = False
    # When the comment holding the newest notice was last written, as the API
    # gives it. The notice states its wait from then, not from whenever this
    # runs. Empty when unknown.
    notice_at: str = ""
    # Merged or closed. The service refuses to review a closed pull request,
    # so asking only adds a comment (spec 043 amendment).
    closed: bool = False
    # A review the service is still writing.
    in_progress: bool = False
    # A completed review that found nothing, read from the summary comment
    # because it leaves no review object behind.
    clean_review: bool = False

    @property
    def reviewed(self) -> bool:
        """Whether anything has actually reviewed this.

        Zero threads is the state six pull requests were in while their
        checks read as passing, so this is the distinction the check itself
        does not draw. A review that posted only findings outside the diff
        creates no threads either, so reviews count too: otherwise a reviewed
        pull request reads as unreviewed and gets asked again.
        """
        return self.review_threads > 0 or self.reviews_seen > 0 or self.clean_review

    @property
    def reviewed_at_head(self) -> bool:
        """Whether a review covered the head itself, not only an earlier one.

        The decision, the report line and the exit code all read this. When
        only the decision did, a pull request whose last review came before a
        push printed "reviewed, N threads, nothing outstanding" and exited 0,
        while the decision asked for the review the push earned (spec 043
        amendment).
        """
        return bool(self.last_reviewed_sha) and self.last_reviewed_sha == self.head_sha

    @property
    def outstanding(self) -> bool:
        """Whether anything is waiting on us.

        Truncation counts. A bounded query that had another page cannot show
        that nothing is outstanding; it can only show that nothing it read
        was. Failing closed here is what stops the tool exiting 0 on the
        busiest pull request (spec 045).
        """
        return bool(self.awaiting) or bool(self.unread_reviews) or self.truncated


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


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def remaining_minutes(wait: int, notice_at: str, now: datetime) -> int:
    """Minutes left before asking again: the stated wait and the grace minute,
    counted from when the notice was posted.

    A notice can sit on a pull request for hours, and counting its wait from
    now made a limit that had long passed cost another full wait (spec 043
    amendment). An unknown time keeps the whole wait, because guessing an
    earlier one could ask too soon.
    """
    total = wait + GRACE_MINUTES
    posted = _parse_time(notice_at) if notice_at else None
    if posted is None:
        return total
    elapsed = (now - posted).total_seconds() / 60
    return max(0, math.ceil(total - elapsed))


def decide(
    state: PullRequestState,
    *,
    requests_today: int,
    daily_limit: int,
    now: datetime | None = None,
) -> Decision:
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
        if state.truncated:
            # Otherwise a truncation-only outstanding state printed
            # "NEEDS A REPLY:" with nothing after the colon (review of PR #50).
            parts.append("a bounded query had another page, so this is not a full picture")
        return Decision(RESPOND, reason="; ".join(parts))

    if state.closed:
        # After answering, which a merged pull request is still owed.
        return Decision(SKIP, reason="closed: the service does not review a closed pull request")

    if state.in_progress:
        return Decision(SKIP, reason="a review is in progress")

    if requests_today >= daily_limit:
        return Decision(SKIP, reason=f"already asked {requests_today} times today")

    notice = newest_notice(state.comment_bodies)
    if notice is not None:
        wait = parse_wait_minutes(notice)
        if not wait:
            # A notice with no readable wait. Reported rather than guessed at.
            return Decision(SKIP, reason="a rate-limit notice carried no readable wait")
        left = remaining_minutes(wait, state.notice_at, now or datetime.now(UTC))
        if left > 0:
            return Decision(WAIT, wait_minutes=left, reason="rate limited")
        # The wait has passed. What the notice cut short is decided as if
        # there were no notice: asked for when unreviewed or moved, left alone
        # when a review has since covered the head.

    if not state.reviewed:
        return Decision(REQUEST, reason="nothing has reviewed this yet")

    if state.reviewed_at_head:
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
        # The whole JSON, parsed here. `--jq .[].body` emits the bodies
        # newline-separated and a comment body is itself multi-line, so there
        # is no way to tell one body from the next in that output. Splitting
        # it by line shredded every notice: `newest_notice` then matched the
        # closing `<!-- end of auto-generated comment: rate limited ... -->`
        # marker, which carries no wait, and the whole feature reported "a
        # rate-limit notice carried no readable wait" against every genuinely
        # rate-limited pull request. The tests passed because they handed the
        # parser one whole notice as a single element.
        comments_raw = run(["api", f"repos/{owner}/{repo}/issues/{number}/comments"])
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
                f"{{pullRequest(number:{number}){{state "
                f"reviewThreads(first:100){{pageInfo{{hasNextPage}} nodes{{id isResolved "
                f"comments(last:1){{nodes{{id author{{login}}}}}}}}}} "
                f"reviews(last:20){{{REVIEW_FIELDS}}}"
                f"}}}}}}",
            ]
        )
    except Exception as error:
        raise FollowUpError(f"could not read pull request {number}: {error}") from error

    try:
        comment_payload: object = json.loads(comments_raw)
    except json.JSONDecodeError as error:
        raise FollowUpError(f"unexpected comment payload for {number}: {error}") from error
    comments = [_as_dict(entry) for entry in _as_list(comment_payload)]
    comment_bodies = [str(comment.get("body") or "") for comment in comments]
    # The newest notice is in the last comment that holds one, as
    # `newest_notice` reads it; the service edits that comment in place, so
    # its update time is when the notice was written.
    notice_at = ""
    in_progress = False
    clean_review = False
    summary_sha = ""
    for comment, body in zip(comments, comment_bodies, strict=True):
        if any(NOTICE_MARKER in block for block in body.split(NOTICE_SPLIT)):
            notice_at = str(comment.get("updated_at") or comment.get("created_at") or "")
        author = str(_as_dict(comment.get("user")).get("login", ""))
        if not author.lower().startswith(REVIEWER_LOGIN):
            continue
        # The service rewrites its summary comment as it goes and drops the
        # marker when the review lands; a later reply of its own must not
        # hide a marker that is still there (spec 043 amendment).
        in_progress = in_progress or IN_PROGRESS_MARKER in body
        if CLEAN_REVIEW_MARKER in body:
            covered = REVIEWED_RANGE_PATTERN.findall(body)
            if covered:
                clean_review = True
                summary_sha = covered[-1][1]

    try:
        payload: object = json.loads(detail_raw)
        pull = _as_dict(
            _as_dict(_as_dict(_as_dict(payload).get("data")).get("repository")).get("pullRequest")
        )
        threads_conn = _as_dict(pull.get("reviewThreads"))
        reviews_conn = _as_dict(pull.get("reviews"))
        closed = str(pull.get("state") or "OPEN").upper() != "OPEN"
        thread_nodes = _as_list(threads_conn.get("nodes"))
        review_nodes = _as_list(reviews_conn.get("nodes"))
        # Both connections are bounded. Past the bound the unread findings are
        # simply absent from the payload, so the tool exited 0 on exactly the
        # pull request most likely to have something outstanding: the one with
        # the most review traffic. Reading hasNextPage makes truncation
        # visible, and `outstanding` below makes it fail closed (spec 045).
        # reviewThreads is first:100, so anything beyond it is a NEXT page.
        # reviews is last:20, so it already holds the newest and anything
        # omitted is a PREVIOUS one: hasNextPage is always false there and
        # reading it made the reviews half of this check inert (review of #50).
        threads_truncated = bool(_as_dict(threads_conn.get("pageInfo")).get("hasNextPage"))
    except json.JSONDecodeError as error:
        raise FollowUpError(f"unexpected review payload for {number}: {error}") from error
    # Every reply in a thread is a review node, so twenty nodes can be all
    # replies and the review they answer sits on an earlier page. Read back
    # until there is none, or fail closed at the page bound (spec 043
    # amendment on the review window).
    earlier, reviews_truncated = _earlier_reviews(
        number, run, owner=owner, repo=repo, page_info=_as_dict(reviews_conn.get("pageInfo"))
    )
    review_nodes = earlier + review_nodes
    truncated = threads_truncated or reviews_truncated

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
        body = str(node.get("body") or "")
        reviews.append(
            ReviewBody(
                identifier=str(node.get("id", "")),
                author=author,
                body=body,
            )
        )
        # The sha the newest review from the reviewer actually looked at.
        # This was never populated, so the "already reviewed at the current
        # head" branch could not fire and the loop asked again every cycle.
        # Its tests passed because they built the state by hand rather than
        # going through here.
        #
        # Only a node with a body is a review. A reply in a review thread is a
        # node too, with an empty body and the head at the moment of the reply
        # as its commit, so one acknowledgement after a push read the new head
        # as reviewed and the review the push earned was never asked for (PR
        # #147, spec 043 amendment).
        if author.lower().startswith(REVIEWER_LOGIN) and body:
            reviewed_sha = str(_as_dict(node.get("commit")).get("oid", "")) or reviewed_sha

    if clean_review and summary_sha:
        # The summary is rewritten on every review, so it is the newest word.
        reviewed_sha = summary_sha
    handled = load_handled()
    return PullRequestState(
        number=number,
        head_sha=head,
        review_threads=len(threads),
        comment_bodies=comment_bodies,
        last_reviewed_sha=reviewed_sha,
        reviews_seen=sum(1 for review in reviews if review.from_reviewer),
        awaiting=tuple(threads_awaiting_reply(threads, handled)),
        unread_reviews=tuple(reviews_needing_a_read(reviews, handled)),
        truncated=truncated,
        notice_at=notice_at,
        closed=closed,
        in_progress=in_progress,
        clean_review=clean_review,
    )


def _earlier_reviews(
    number: int, run: GitHubRunner, *, owner: str, repo: str, page_info: dict[str, object]
) -> tuple[list[object], bool]:
    """The review nodes older than the newest page, oldest first, and whether
    any were left unread.

    A page that says there is more but carries no cursor, or more pages than
    `REVIEW_PAGE_LIMIT`, leaves the rest unread, and the caller fails closed.
    """
    nodes: list[object] = []
    for _ in range(REVIEW_PAGE_LIMIT):
        cursor = page_info.get("startCursor")
        if not page_info.get("hasPreviousPage"):
            return nodes, False
        if not isinstance(cursor, str) or not cursor:
            return nodes, True
        try:
            raw = run(
                [
                    "api",
                    "graphql",
                    "-f",
                    f'query={{repository(owner:"{owner}",name:"{repo}")'
                    f"{{pullRequest(number:{number}){{"
                    f"reviews(last:100,before:{json.dumps(cursor)}){{{REVIEW_FIELDS}}}"
                    f"}}}}}}",
                ]
            )
            payload: object = json.loads(raw)
        except json.JSONDecodeError as error:
            raise FollowUpError(f"unexpected review payload for {number}: {error}") from error
        except Exception as error:
            raise FollowUpError(f"could not read pull request {number}: {error}") from error
        conn = _as_dict(
            _as_dict(
                _as_dict(_as_dict(_as_dict(payload).get("data")).get("repository")).get(
                    "pullRequest"
                )
            ).get("reviews")
        )
        nodes = _as_list(conn.get("nodes")) + nodes
        page_info = _as_dict(conn.get("pageInfo"))
    return nodes, bool(page_info.get("hasPreviousPage"))


def request_review(number: int, run: GitHubRunner, *, owner: str, repo: str) -> None:
    """Ask for another review. `--repo` for the same reason as above."""
    run(["pr", "comment", str(number), "--repo", f"{owner}/{repo}", "--body", REQUEST_COMMENT])


def _why_not_reviewed(state: PullRequestState) -> str:
    """The reason the never-reviewed lines below give, in their order, or
    nothing when none applies."""
    if state.closed:
        return "closed, so the service will not now"
    if state.in_progress:
        return "a review is in progress"
    if newest_notice(state.comment_bodies) is not None:
        return "rate limited"
    return ""


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
        elif state.reviewed and not state.reviewed_at_head:
            # Reviewed, but only before the head moved: the push that answered
            # the findings has had no review of its own (spec 043 amendment).
            seen = (
                f"last reviewed at {state.last_reviewed_sha[:7]}"
                if state.last_reviewed_sha
                else "no review names a commit"
            )
            why = _why_not_reviewed(state)
            lines.append(
                f"PR #{state.number}: NOT REVIEWED AT THE HEAD, {seen}"
                + (f"; {why}" if why else "")
            )
        elif state.reviewed:
            lines.append(
                f"PR #{state.number}: reviewed, {state.review_threads} threads, nothing outstanding"
            )
        elif state.closed:
            lines.append(f"PR #{state.number}: NOT REVIEWED, closed, so the service will not now")
        elif state.in_progress:
            lines.append(f"PR #{state.number}: NOT REVIEWED YET, a review is in progress")
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
