"""Waiting out a rate-limited review (spec 043).

The defect that motivated this: the review service's check reports success
with the reason "Review rate limited", so a pull request nothing has looked
at is indistinguishable from a reviewed one. Six were in that state at once,
with zero review threads between them and every check green.

No test here touches the network. The `gh` seam is injected, which is also
what keeps a pull request title out of a fixture (ADR-008).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harrier.reviewfollowup import (
    DEFAULT_DAILY_LIMIT,
    REQUEST,
    RESPOND,
    SKIP,
    WAIT,
    FollowUpError,
    PullRequestState,
    ThreadState,
    decide,
    gather,
    load_counts,
    newest_notice,
    outstanding_identifiers,
    parse_wait_minutes,
    record_handled,
    record_request,
    report,
    request_review,
    state_path,
)

NOTICE = """<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->
> [!WARNING]
> **Next review available in:** **{wait}**
> You've used all free OSS reviews for now.
<!-- end of auto-generated comment: rate limited by coderabbit.ai -->"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


def a_state(**overrides: object) -> PullRequestState:
    fields: dict[str, object] = {
        "number": 39,
        "head_sha": "abc1234",
        "review_threads": 0,
        "comment_bodies": [],
    }
    fields.update(overrides)
    return PullRequestState(**fields)  # pyright: ignore[reportArgumentType]


# --- the wait, in every shape the notice uses --------------------------------


@pytest.mark.parametrize(
    ("wait", "expected"),
    [
        ("38 minutes", 38),
        ("1 minute", 1),
        ("1 hour", 60),
        ("2 hours", 120),
        ("2 hours 15 minutes", 135),
        ("57 minutes", 57),
    ],
)
def test_the_wait_is_parsed(wait: str, expected: int) -> None:
    assert parse_wait_minutes(NOTICE.format(wait=wait)) == expected


def test_a_notice_with_no_readable_wait_returns_none() -> None:
    """None rather than a default. A parse that cannot find a wait must say
    so, not guess one and act on it."""
    assert parse_wait_minutes("<!-- rate limited by coderabbit.ai --> soon, probably") is None


def test_text_that_is_not_a_notice_returns_none() -> None:
    assert parse_wait_minutes("Actionable comments posted: 3") is None


def test_a_zero_wait_reads_as_no_wait() -> None:
    assert parse_wait_minutes(NOTICE.format(wait="0 minutes")) is None


# --- the newest notice wins ---------------------------------------------------


def test_the_newest_notice_is_the_one_used() -> None:
    """An older notice has usually expired, and acting on it re-requests
    immediately for no reason."""
    bodies = [NOTICE.format(wait="55 minutes"), "some chatter", NOTICE.format(wait="3 minutes")]
    newest = newest_notice(bodies)
    assert newest is not None
    assert parse_wait_minutes(newest) == 3


def test_no_notice_means_none() -> None:
    assert newest_notice(["Actionable comments posted: 3", "looks good"]) is None


def test_a_notice_inside_a_longer_comment_is_found() -> None:
    bodies = ["walkthrough text\n" + NOTICE.format(wait="12 minutes") + "\nmore text"]
    found = newest_notice(bodies)
    assert found is not None
    assert parse_wait_minutes(found) == 12


# --- what to do about it ------------------------------------------------------


def test_a_rate_limited_pull_request_waits() -> None:
    decision = decide(
        a_state(comment_bodies=[NOTICE.format(wait="38 minutes")]),
        requests_today=0,
        daily_limit=DEFAULT_DAILY_LIMIT,
    )
    assert decision.action == WAIT
    assert decision.wait_minutes == 39, "the grace minute keeps it off the exact boundary"


def test_an_unreviewed_pull_request_is_asked_again() -> None:
    decision = decide(a_state(), requests_today=0, daily_limit=DEFAULT_DAILY_LIMIT)
    assert decision.action == REQUEST


def test_a_reviewed_pull_request_at_the_same_head_is_left_alone() -> None:
    """Asking again with an unchanged head repeats a review that already
    happened."""
    decision = decide(
        a_state(review_threads=17, head_sha="abc1234", last_reviewed_sha="abc1234"),
        requests_today=0,
        daily_limit=DEFAULT_DAILY_LIMIT,
    )
    assert decision.action == SKIP
    assert "already reviewed" in decision.reason


def test_a_moved_head_is_asked_again() -> None:
    decision = decide(
        a_state(review_threads=17, head_sha="def5678", last_reviewed_sha="abc1234"),
        requests_today=0,
        daily_limit=DEFAULT_DAILY_LIMIT,
    )
    assert decision.action == REQUEST
    assert "head has moved" in decision.reason


def test_the_daily_bound_stops_the_loop() -> None:
    """Without it, a repository that stays rate-limited comments every hour
    forever: noise on the author's own pull request and load on somebody
    else's service."""
    decision = decide(
        a_state(comment_bodies=[NOTICE.format(wait="38 minutes")]),
        requests_today=DEFAULT_DAILY_LIMIT,
        daily_limit=DEFAULT_DAILY_LIMIT,
    )
    assert decision.action == SKIP
    assert "already asked" in decision.reason


def test_the_bound_wins_over_everything_else() -> None:
    """Including an unreviewed pull request, which is otherwise the strongest
    reason to ask."""
    decision = decide(a_state(), requests_today=99, daily_limit=DEFAULT_DAILY_LIMIT)
    assert decision.action == SKIP


def test_a_notice_without_a_wait_is_reported_not_guessed() -> None:
    decision = decide(
        a_state(comment_bodies=["<!-- rate limited by coderabbit.ai --> shortly"]),
        requests_today=0,
        daily_limit=DEFAULT_DAILY_LIMIT,
    )
    assert decision.action == SKIP
    assert "no readable wait" in decision.reason


# --- the counter --------------------------------------------------------------


def test_requests_are_counted_per_pull_request(env: Path) -> None:
    assert record_request(39) == 1
    assert record_request(39) == 2
    assert record_request(40) == 1
    assert load_counts() == {"39": 2, "40": 1}


def test_yesterdays_counts_do_not_carry_forward(env: Path) -> None:
    """The bound is a daily one, so an old count is not merely useless but
    actively wrong to apply."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2020-01-01", "counts": {"39": 99}}), encoding="utf-8")
    assert load_counts() == {}


def test_a_damaged_counter_does_not_block_the_loop(env: Path) -> None:
    """The worst case of losing it is a few extra requests, bounded by the
    limit, so this one degrades rather than refusing."""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_counts() == {}


def test_an_absent_counter_reads_as_empty(env: Path) -> None:
    assert load_counts() == {}


# --- the distinction the check does not draw ----------------------------------


def test_a_rate_limited_pull_request_reports_as_not_reviewed() -> None:
    """The whole point. The service's check says pass; this says nothing has
    looked at it."""
    lines = report([a_state(comment_bodies=[NOTICE.format(wait="38 minutes")])])
    assert "NOT REVIEWED" in lines[0]
    assert "rate limited" in lines[0]


def test_a_reviewed_pull_request_reports_as_reviewed() -> None:
    lines = report([a_state(review_threads=17)])
    assert "NOT REVIEWED" not in lines[0]
    assert "17 threads" in lines[0]


def test_a_pull_request_with_neither_is_still_not_reviewed() -> None:
    """No review and no notice is not success either."""
    lines = report([a_state()])
    assert "NOT REVIEWED" in lines[0]


# --- the gh seam --------------------------------------------------------------


@pytest.fixture
def handled_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory of its own, so the handled record starts empty.

    Without it, `gather` reads whatever this machine has already answered and
    the assertions depend on the developer's own history.
    """
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return tmp_path


def payload(
    *,
    threads: list[dict[str, object]] | None = None,
    reviews: list[dict[str, object]] | None = None,
) -> str:
    """A GraphQL answer in the shape gather parses.

    Built here rather than pasted, so a test says which threads and reviews it
    means instead of carrying an opaque blob.
    """
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"nodes": threads or []},
                        "reviews": {"nodes": reviews or []},
                    }
                }
            }
        }
    )


def thread(
    identifier: str, *, author: str = "coderabbitai", comment: str = "c1", resolved: bool = False
) -> dict[str, object]:
    return {
        "id": identifier,
        "isResolved": resolved,
        "comments": {"nodes": [{"id": comment, "author": {"login": author}}]},
    }


def review(
    identifier: str, *, author: str = "coderabbitai", body: str = "", commit: str = ""
) -> dict[str, object]:
    return {
        "id": identifier,
        "author": {"login": author},
        "body": body,
        "commit": {"oid": commit},
    }


def stub_gh(comments: str, head: str, detail: str):
    """Answer by which call it is, graphql first.

    The issue-comments branch used to match on the word "comments" anywhere
    in the arguments, and the graphql query now contains `comments(last:1)`,
    so it swallowed the graphql call and returned the wrong string. Matching
    the most specific thing first is the fix.
    """

    def run(argv: list[str]) -> str:
        if "graphql" in argv:
            return detail
        if argv[0] == "pr" and "headRefOid" in " ".join(argv):
            return head
        if argv[0] == "api" and "issues/" in " ".join(argv):
            return comments
        raise AssertionError(f"unexpected gh call: {argv}")

    return run


def test_gather_reads_what_the_decision_needs(handled_env: Path) -> None:
    state = gather(
        39,
        stub_gh(
            NOTICE.format(wait="8 minutes"),
            "abc1234\n",
            payload(threads=[thread(f"t{n}") for n in range(17)]),
        ),
        owner="o",
        repo="r",
    )
    assert state.number == 39
    assert state.head_sha == "abc1234"
    assert state.review_threads == 17
    assert newest_notice(state.comment_bodies) is not None


def test_gh_failing_is_reported_not_swallowed() -> None:
    """A follow-up that cannot read the pull request must not report it as
    anything: an unreadable pull request is not an unreviewed one."""

    def explode(_argv: list[str]) -> str:
        raise RuntimeError("gh: not authenticated")

    with pytest.raises(FollowUpError, match="could not read"):
        gather(39, explode, owner="o", repo="r")


def test_an_unreadable_payload_is_reported(handled_env: Path) -> None:
    with pytest.raises(FollowUpError, match="unexpected review payload"):
        gather(39, stub_gh("", "abc\n", "not json"), owner="o", repo="r")


def test_every_gh_call_names_the_repository() -> None:
    """`gh pr` resolves the repository from the working directory, so a call
    without `--repo` works from a checkout and fails everywhere else,
    including from the scheduled job this is meant to run in. Found by
    running it from a temporary directory (PR #40)."""
    calls: list[list[str]] = []

    def record(argv: list[str]) -> str:
        calls.append(argv)
        if argv[0] == "pr":
            return "abc1234\n"
        if "graphql" in argv:
            return payload()
        return ""

    gather(39, record, owner="o", repo="r")
    request_review(39, record, owner="o", repo="r")

    pr_calls = [argv for argv in calls if argv and argv[0] == "pr"]
    assert pr_calls, "no gh pr call was made; this test is looking at the wrong place"
    for argv in pr_calls:
        assert "--repo" in argv, f"gh pr call depends on the working directory: {argv}"
        assert argv[argv.index("--repo") + 1] == "o/r"


# --- follow-ups: what a count of unresolved threads cannot see ----------------


def test_a_reply_in_a_resolved_thread_still_needs_an_answer(handled_env: Path) -> None:
    """The blind spot that cost a missed finding.

    Resolving a thread is what hides the reply that arrives after it, so a
    check that filters on `isResolved` is blind to exactly the disagreement
    worth reading. The question is who spoke last.
    """
    state = gather(
        39,
        stub_gh("", "abc\n", payload(threads=[thread("t1", resolved=True, comment="c9")])),
        owner="o",
        repo="r",
    )
    assert state.outstanding
    assert [t.identifier for t in state.awaiting] == ["t1"]


def test_a_thread_we_answered_last_is_not_outstanding(handled_env: Path) -> None:
    state = gather(
        39,
        stub_gh("", "abc\n", payload(threads=[thread("t1", author="akin-oz")])),
        owner="o",
        repo="r",
    )
    assert not state.outstanding


def test_a_finding_outside_the_diff_is_found(handled_env: Path) -> None:
    """A review can post findings it could not attach to a line. They exist
    only in the review body, so no thread carries them and no query over
    `reviewThreads` can return them however it filters. One Major finding
    arrived this way on PR #37 and this loop missed it."""
    body = (
        "Actionable comments posted: 4\n"
        "Caution: Some comments are outside the diff and can't be posted "
        "inline due to platform limitations."
    )
    state = gather(
        39, stub_gh("", "abc\n", payload(reviews=[review("r1", body=body)])), owner="o", repo="r"
    )
    assert state.outstanding
    assert state.unread_reviews[0].actionable_count == 4
    assert state.unread_reviews[0].has_findings_outside_the_diff
    assert any("OUTSIDE THE DIFF" in line for line in report([state]))


def test_a_summary_review_is_not_treated_as_a_finding(handled_env: Path) -> None:
    """Otherwise every cycle re-surfaces the walkthrough and never settles."""
    state = gather(
        39,
        stub_gh(
            "", "abc\n", payload(reviews=[review("r1", body="Walkthrough: this adds a thing")])
        ),
        owner="o",
        repo="r",
    )
    assert not state.outstanding


def test_answering_is_remembered_so_it_does_not_resurface(handled_env: Path) -> None:
    detail = payload(threads=[thread("t1", comment="c9")])
    first = gather(39, stub_gh("", "abc\n", detail), owner="o", repo="r")
    assert first.outstanding

    record_handled(outstanding_identifiers(first))
    second = gather(39, stub_gh("", "abc\n", detail), owner="o", repo="r")
    assert not second.outstanding


def test_a_further_reply_comes_back_after_being_answered(handled_env: Path) -> None:
    """Keyed on the comment, not the thread: a thread already answered can
    gain another reply, and that reply is a new thing to read."""
    first = gather(
        39, stub_gh("", "abc\n", payload(threads=[thread("t1", comment="c9")])), owner="o", repo="r"
    )
    record_handled(outstanding_identifiers(first))
    again = gather(
        39,
        stub_gh("", "abc\n", payload(threads=[thread("t1", comment="c10")])),
        owner="o",
        repo="r",
    )
    assert again.outstanding


def test_answering_comes_before_asking_for_more() -> None:
    """A rate-limited request spent while a finding sits unanswered buys
    nothing: the answer is the part that changes the code."""
    state = PullRequestState(
        number=39,
        head_sha="abc",
        review_threads=1,
        comment_bodies=[],
        awaiting=(ThreadState("t1", False, "coderabbitai", "c1"),),
    )
    assert decide(state, requests_today=0, daily_limit=6).action == RESPOND
    # And it outranks the daily bound, which would otherwise hide it.
    assert decide(state, requests_today=99, daily_limit=6).action == RESPOND


def test_the_reviewed_sha_is_read_from_the_reviews(handled_env: Path) -> None:
    """This was never populated, so the "already reviewed at this head"
    branch could not fire and the loop asked again every cycle. Its tests
    passed because they built the state by hand rather than through gather."""
    state = gather(
        39,
        stub_gh("", "abc1234\n", payload(reviews=[review("r1", commit="abc1234")])),
        owner="o",
        repo="r",
    )
    assert state.last_reviewed_sha == "abc1234"
    assert decide(state, requests_today=0, daily_limit=6).action == SKIP


def test_a_review_with_no_threads_still_counts_as_reviewed(handled_env: Path) -> None:
    """A review that posted only findings outside the diff creates no
    threads, and a pull request that has been reviewed must not read as
    unreviewed and be asked again."""
    state = gather(
        39,
        stub_gh("", "abc\n", payload(reviews=[review("r1", body="Walkthrough")])),
        owner="o",
        repo="r",
    )
    assert state.review_threads == 0
    assert state.reviewed
