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
    SKIP,
    WAIT,
    FollowUpError,
    PullRequestState,
    decide,
    gather,
    load_counts,
    newest_notice,
    parse_wait_minutes,
    record_request,
    report,
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


def stub_gh(comments: str, head: str, threads: str):
    def run(argv: list[str]) -> str:
        if argv[0] == "api" and "comments" in " ".join(argv):
            return comments
        if argv[0] == "pr" and "headRefOid" in " ".join(argv):
            return head
        if argv[0] == "api" and "graphql" in argv:
            return threads
        raise AssertionError(f"unexpected gh call: {argv}")

    return run


def test_gather_reads_what_the_decision_needs() -> None:
    state = gather(
        39, stub_gh(NOTICE.format(wait="8 minutes"), "abc1234\n", "17\n"), owner="o", repo="r"
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


def test_an_unreadable_thread_count_is_reported() -> None:
    with pytest.raises(FollowUpError, match="unexpected thread count"):
        gather(39, stub_gh("", "abc\n", "not a number\n"), owner="o", repo="r")
