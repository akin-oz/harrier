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

from pathlib import Path

import pytest

from harrier.reviewfollowup import PullRequestState, ReviewBody, ThreadState
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


def _install(monkeypatch: pytest.MonkeyPatch, state: PullRequestState) -> None:
    def fake_gather(*_a: object, **_k: object) -> PullRequestState:
        return state

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
            reviews_seen=1,
        ),
    )
    assert main(_argv([4])) == 0
