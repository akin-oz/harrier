"""Decisions the CLI makes that no test executed (spec 045).

Each of these was proven dead by mutation: the branch was disabled, or made
to return the opposite value, and the whole suite stayed green. They are
grouped here because they share a cause rather than a subject. A command
handler that only ever runs through a mocked helper has its own logic
untested, and the logic is where the refusals live.

The tests call the handler with a real argparse namespace, the way `main`
does, rather than calling the library function the handler wraps. Testing
`verify_archive` proves the archive reader works; it does not prove the
command reports a corrupt archive as a failure, which is the decision that
matters to whoever runs it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harrier.reviewfollowup import PullRequestState, ReviewBody, ThreadState, record_handled
from harrier_cli.main import main

REVIEWER = "coderabbitai"


# --- verify-backup ---------------------------------------------------------


def test_a_corrupt_archive_is_reported_as_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The except arm could `return 0` and nothing failed, so a backup that
    does not open reported that it did: the one question the command exists
    to answer, answered wrong."""
    archive = tmp_path / "broken.tar.gz"
    archive.write_bytes(b"this is not a gzip stream")

    code = main(["verify-backup", str(archive)])

    assert code == 1, "a corrupt archive reported success"
    assert "not usable" in capsys.readouterr().err


def test_a_missing_archive_is_reported_as_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["verify-backup", str(tmp_path / "absent.tar.gz")])
    assert code == 1
    assert "not usable" in capsys.readouterr().err


# --- cutover ---------------------------------------------------------------


def test_cutover_refuses_when_the_old_repo_is_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_cmd_cutover` could be a no-op returning 0 with the suite green. This
    is its first refusal, and it is the one that protects an operator who
    mistyped the path from being told the cutover succeeded."""
    code = main(["cutover", "--old-root", str(tmp_path / "not-there"), "preflight"])

    assert code == 1, "cutover accepted a path with no repository at it"
    assert "no old repo" in capsys.readouterr().err


def test_cutover_preflight_reports_blocking_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A blocked preflight has to exit non-zero. Exiting 0 here is how an
    operator reads "not ready" as "ready"."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    old_root = tmp_path / "old"
    old_root.mkdir()

    code = main(["cutover", "--old-root", str(old_root), "preflight"])

    captured = capsys.readouterr()
    # An empty directory is not a usable old repo, so preflight must block.
    assert code == 1, f"preflight passed on an empty old repo:\n{captured.out}"
    assert "blocking check" in captured.err


# --- review-followup -------------------------------------------------------


def _argv(numbers: list[int]) -> list[str]:
    return [
        "review-followup",
        *[str(n) for n in numbers],
        "--owner",
        "example",
        "--repo",
        "example",
        "--daily-limit",
        "3",
        "--dry-run",
    ]


def _install(monkeypatch: pytest.MonkeyPatch, *states: PullRequestState) -> None:
    by_number = {state.number: state for state in states}

    def fake_gather(number: int, *_a: object, **_k: object) -> PullRequestState:
        return by_number[number]

    def no_counts() -> dict[str, int]:
        return {}

    monkeypatch.setattr("harrier.reviewfollowup.gather", fake_gather)
    monkeypatch.setattr("harrier.reviewfollowup.load_counts", no_counts)

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a dry run must not request a review")

    monkeypatch.setattr("harrier.reviewfollowup.request_review", refuse)


def test_an_unreviewed_pull_request_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero threads and zero reviews is the state six pull requests were in
    while their checks read as passing. Disabling this exit changed nothing
    in the suite, and it is the mechanism the review-response rule is built
    on."""
    _install(
        monkeypatch,
        PullRequestState(number=1, head_sha="abc", review_threads=0, comment_bodies=[]),
    )
    assert main(_argv([1])) == 2


def test_an_unanswered_finding_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewed, but the reviewer spoke last. Exiting 0 here is what let a
    Major finding sit unread."""
    _install(
        monkeypatch,
        PullRequestState(
            number=2,
            head_sha="abc",
            review_threads=1,
            comment_bodies=[],
            reviews_seen=1,
            awaiting=(
                ThreadState(
                    identifier="t1",
                    resolved=True,
                    last_author=REVIEWER,
                    last_comment_id="c1",
                ),
            ),
        ),
    )
    assert main(_argv([2])) == 3


def test_a_finding_that_never_became_a_thread_still_exits_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Some comments are outside the diff and can't be posted inline" leaves
    findings in a review body and no thread at all, so a query over threads
    returns nothing however it filters."""
    _install(
        monkeypatch,
        PullRequestState(
            number=3,
            head_sha="abc",
            review_threads=0,
            comment_bodies=[],
            reviews_seen=1,
            unread_reviews=(
                ReviewBody(
                    identifier="r1",
                    author=REVIEWER,
                    body="Actionable comments posted: 4\nSome comments are outside the diff",
                ),
            ),
        ),
    )
    assert main(_argv([3])) == 3


def test_a_settled_pull_request_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The counterpart that stops the exits above from being unconditional:
    a gate that always fails is as useless as one that never does."""
    _install(
        monkeypatch,
        PullRequestState(
            number=4,
            head_sha="abc",
            review_threads=2,
            comment_bodies=[],
            # Settled means a review covered the head itself (spec 043
            # amendment).
            last_reviewed_sha="abc",
            reviews_seen=1,
        ),
    )
    assert main(_argv([4])) == 0


def test_a_truncated_page_of_findings_still_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both GitHub connections are bounded and neither hasNextPage was read,
    so past the bound the unread findings were simply absent and the tool
    exited 0 on exactly the pull request most likely to have something
    outstanding: the one with the most review traffic."""
    _install(
        monkeypatch,
        PullRequestState(
            number=5,
            head_sha="abc",
            review_threads=100,
            comment_bodies=[],
            reviews_seen=20,
            truncated=True,
        ),
    )
    assert main(_argv([5])) == 3


def test_a_head_reviewed_before_it_moved_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The decision asked for a review of the new head while the exit code
    called the pull request settled: PR #147's shape, after a push that
    answered its review, printed "reviewed, 2 threads, nothing outstanding"
    and exited 0 (spec 043 amendment). Through the real `gather`, with only
    `gh` stubbed, because that spec has twice recorded a defect that tests
    bypassing `gather` could not see."""
    reviewed, pushed = "a" * 40, "b" * 40

    def review(identifier: str, author: str, body: str, commit: str) -> dict[str, object]:
        return {
            "id": identifier,
            "author": {"login": author},
            "body": body,
            "commit": {"oid": commit},
        }

    def thread(identifier: str, last_comment: str) -> dict[str, object]:
        return {
            "id": identifier,
            "isResolved": True,
            "comments": {"nodes": [{"id": last_comment, "author": {"login": REVIEWER}}]},
        }

    pull = {
        "state": "OPEN",
        "reviewThreads": {"nodes": [thread("t1", "ack1"), thread("t2", "ack2")]},
        "reviews": {
            "nodes": [
                review("r1", REVIEWER, "**Actionable comments posted: 2**", reviewed),
                review("r2", "akin-oz", "", pushed),
                review("r3", "akin-oz", "", pushed),
                review("r4", REVIEWER, "", pushed),
                review("r5", REVIEWER, "", pushed),
            ]
        },
    }
    answers = {
        "graphql": json.dumps({"data": {"repository": {"pullRequest": pull}}}),
        "pr": f"{pushed}\n",
        "api": "[]",
    }

    def gh(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == "gh", f"unexpected command: {command}"
        answer = answers["graphql" if "graphql" in command else command[1]]
        return subprocess.CompletedProcess(command, 0, stdout=answer, stderr="")

    monkeypatch.setattr("subprocess.run", gh)
    # Every finding answered, and the reviewer's acknowledgements read.
    record_handled(["r1", "ack1", "ack2"])

    code = main(_argv([147]))

    lines = capsys.readouterr().out.splitlines()
    assert "PR #147: the head has moved since the last review" in lines
    # The report line comes last, after the decisions.
    assert (code, lines[-1]) == (2, "PR #147: NOT REVIEWED AT THE HEAD, last reviewed at aaaaaaa")


def test_an_unanswered_finding_after_a_push_still_exits_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push that answers some findings moves the head while another thread
    still waits on us. The decision answers before it asks, and the
    review-response rule says the command exits 3 while anything is
    outstanding, so the moved head must not turn that into exit 2."""
    _install(
        monkeypatch,
        PullRequestState(
            number=6,
            head_sha="new",
            review_threads=2,
            comment_bodies=[],
            last_reviewed_sha="old",
            reviews_seen=1,
            awaiting=(
                ThreadState(
                    identifier="t1",
                    resolved=False,
                    last_author=REVIEWER,
                    last_comment_id="c1",
                ),
            ),
        ),
    )
    assert main(_argv([6])) == 3


def test_one_pull_request_awaiting_review_hides_another_awaiting_an_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Across several pull requests the codes combine as an error first, then
    2, then 3, so a head reviewed only before it moved hides another pull
    request's unanswered finding from the exit code. The report lines still
    show both. Pinned rather than changed (spec 043 amendment, out of scope):
    moving the order changes what a run over several pull requests exits."""
    _install(
        monkeypatch,
        PullRequestState(
            number=7,
            head_sha="new",
            review_threads=2,
            comment_bodies=[],
            last_reviewed_sha="old",
            reviews_seen=1,
        ),
        PullRequestState(
            number=8,
            head_sha="abc",
            review_threads=1,
            comment_bodies=[],
            last_reviewed_sha="abc",
            reviews_seen=1,
            awaiting=(
                ThreadState(
                    identifier="t1",
                    resolved=False,
                    last_author=REVIEWER,
                    last_comment_id="c1",
                ),
            ),
        ),
    )

    code = main(_argv([7, 8]))

    lines = capsys.readouterr().out.splitlines()
    assert "PR #7: NOT REVIEWED AT THE HEAD, last reviewed at old" in lines
    assert "PR #8: NEEDS A REPLY: 1 thread(s) awaiting a reply" in lines
    assert code == 2


# --- portability ------------------------------------------------------------


def test_a_missing_launchctl_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launchctl exists only on macOS, and subprocess.run raises rather than
    returning a code when it is absent. Every caller expected a code, so the
    README's "the scheduler is not portable, and reports as much on other
    systems" was false: it did not report, it crashed with FileNotFoundError.

    Found when the cutover preflight test above first ran on Linux CI. Tested
    by simulating the absence, because this suite's own CI runs on Linux and
    the maintainer's machine is macOS: neither alone exercises both sides.
    """
    from harrier.schedule import LAUNCHCTL_ABSENT, default_launchctl

    def absent(*_a: object, **_k: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "launchctl")

    monkeypatch.setattr("harrier.schedule.subprocess.run", absent)

    code, stdout, stderr = default_launchctl(["print", "gui/501/example"])

    assert code == LAUNCHCTL_ABSENT
    assert stdout == ""
    assert "macOS" in stderr


def test_cutover_preflight_survives_a_machine_without_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure exactly as CI hit it: preflight reached launchctl on a
    machine that has none and the FileNotFoundError escaped the command."""
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    old_root = tmp_path / "old"
    old_root.mkdir()

    calls: list[object] = []

    def absent(*_a: object, **_k: object) -> object:
        calls.append(_a)
        raise FileNotFoundError(2, "No such file or directory", "launchctl")

    monkeypatch.setattr("harrier.schedule.subprocess.run", absent)

    code = main(["cutover", "--old-root", str(old_root), "preflight"])

    # An empty old_root already blocks, so exit 1 alone does not prove the
    # launchctl path was reached at all (review of PR #50).
    assert calls, "preflight never reached launchctl, so this proved nothing"
    assert code == 1, "preflight should block, not crash"
    assert "blocking check" in capsys.readouterr().err
