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
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harrier.reviewfollowup import (
    PullRequestState,
    ReviewBody,
    ThreadState,
    handled_path,
    record_handled,
)
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


# --- review-followup: recording what was read (spec 043 amendment) ----------

REVIEWED, PUSHED = "a" * 40, "b" * 40
MARK_ALL = ["--mark-read", "ack1", "ack2", "r1"]


def _review(identifier: str, author: str, body: str, commit: str) -> dict[str, object]:
    return {"id": identifier, "author": {"login": author}, "body": body, "commit": {"oid": commit}}


def _thread(identifier: str, last_comment: str) -> dict[str, object]:
    return {
        "id": identifier,
        "isResolved": True,
        "comments": {"nodes": [{"id": last_comment, "author": {"login": REVIEWER}}]},
    }


def _answered_after_a_push(
    state: str = "MERGED",
    *,
    last_comments: tuple[str, str] = ("ack1", "ack2"),
    more_threads: bool = False,
) -> dict[str, object]:
    """PR #147's shape: a review with two findings at one commit, a push that
    answered both, and the reviewer's thanks as the last word in each thread.
    Nothing is recorded as read, so both thanks and the review are owed a
    read."""
    first, second = last_comments
    return {
        "state": state,
        "reviewThreads": {
            "pageInfo": {"hasNextPage": more_threads},
            "nodes": [_thread("t1", first), _thread("t2", second)],
        },
        "reviews": {
            "nodes": [
                _review("r1", REVIEWER, "**Actionable comments posted: 2**", REVIEWED),
                _review("r2", "akin-oz", "", PUSHED),
                _review("r3", "akin-oz", "", PUSHED),
                _review("r4", REVIEWER, "", PUSHED),
                _review("r5", REVIEWER, "", PUSHED),
            ]
        },
    }


def _github(
    monkeypatch: pytest.MonkeyPatch,
    pulls: dict[int, dict[str, object]],
    *,
    comments: list[dict[str, object]] | None = None,
    unreadable: int | None = None,
) -> list[list[str]]:
    """`gh` as the command calls it, answering from the pull requests given,
    each with PUSHED as its head. A comment the command posts is kept rather
    than sent, so a test can say none was, and a command this does not know
    fails the test."""
    posted: list[list[str]] = []

    def answer(command: list[str], stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
        stderr = "HTTP 502" if code else ""
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr=stderr)

    def gh(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[0] == "gh", f"unexpected command: {command}"
        if command[1:3] == ["pr", "comment"]:
            posted.append(command)
            return answer(command, "")
        found = re.search(
            r"pullRequest\(number:(\d+)\)|issues/(\d+)/comments|^pr view (\d+)",
            " ".join(command[1:]),
        )
        assert found, f"unexpected command: {command}"
        number = int(next(group for group in found.groups() if group))
        if number == unreadable:
            return answer(command, "", code=1)
        if "graphql" in command:
            return answer(
                command, json.dumps({"data": {"repository": {"pullRequest": pulls[number]}}})
            )
        if command[1] == "pr":
            return answer(command, f"{PUSHED}\n")
        return answer(command, json.dumps(comments or []))

    monkeypatch.setattr("subprocess.run", gh)
    return posted


def _recorded() -> list[str] | None:
    path = handled_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


@pytest.mark.parametrize(
    ("repository", "flags"),
    [
        pytest.param((), "", id="the default repository"),
        pytest.param(
            ("--owner", "example", "--repo", "example"),
            " --owner example --repo example",
            id="another repository",
        ),
    ],
)
def test_marking_read_clears_what_the_run_printed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    repository: tuple[str, ...],
    flags: str,
) -> None:
    """PR #147, replayed on its live data on 2026-10-07, exited 3 on two
    thank-yous and a review whose findings were both fixed, and only a Python
    call could record them as read (spec 043 amendment). The run now prints
    the ids and the command that records them, and that command is run here
    exactly as printed."""
    posted = _github(monkeypatch, {147: _answered_after_a_push()})

    code = main(["review-followup", "147", *repository])

    out = capsys.readouterr().out.splitlines()
    assert "  thread t1 (resolved=True), last comment ack1" in out
    assert "  thread t2 (resolved=True), last comment ack2" in out
    once = (
        "  once read, record them with: "
        f"harrier review-followup 147{flags} --mark-read ack1 ack2 r1"
    )
    assert once in out
    assert (code, _recorded()) == (3, None)

    code = main(once.split(": ", 1)[1].split()[1:])

    out = capsys.readouterr().out.splitlines()
    assert [line for line in out if line.startswith("recorded as read: ")] == [
        "recorded as read: ack1",
        "recorded as read: ack2",
        "recorded as read: r1",
    ]
    assert out[-1] == (
        "PR #147: NOT REVIEWED AT THE HEAD, last reviewed at aaaaaaa; "
        "closed, so the service will not now"
    )
    assert (code, _recorded(), posted) == (2, ["ack1", "ack2", "r1"], [])


@pytest.mark.parametrize(
    ("given", "last_comments"),
    [
        pytest.param("ack1x", ("ack1", "ack2"), id="mistyped"),
        pytest.param("ack9", ("ack1", "ack2"), id="from a pull request not named"),
        pytest.param("ack1", ("ack3", "ack2"), id="a reply a newer one replaced"),
    ],
)
def test_an_id_not_outstanding_records_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    given: str,
    last_comments: tuple[str, str],
) -> None:
    """An id counts only when a pull request named holds it outstanding.
    Recording one that matches nothing would report success while doing
    nothing, and recording a reply a newer one replaced would leave the
    person believing a thread read that they have not seen the end of. The
    valid id beside it is not recorded either: all or nothing."""
    posted = _github(
        monkeypatch,
        {
            147: _answered_after_a_push(last_comments=last_comments),
            150: _answered_after_a_push(last_comments=("ack9", "ack8")),
        },
    )

    code = main(["review-followup", "147", "--mark-read", "ack2", given])

    err = capsys.readouterr().err.splitlines()
    assert (
        f"error: nothing was recorded; not outstanding on the pull requests named: {given}" in err
    )
    assert (code, _recorded(), posted) == (1, None, [])
    # Nothing was recorded, so the next run still prints what is outstanding.
    main(["review-followup", "147", "--dry-run"])
    assert f"  thread t1 (resolved=True), last comment {last_comments[0]}" in (
        capsys.readouterr().out.splitlines()
    )


def test_a_pull_request_that_cannot_be_read_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Its ids cannot be checked, so none of the run's ids is recorded, even
    the ones another pull request named holds outstanding."""
    posted = _github(
        monkeypatch,
        {147: _answered_after_a_push(), 150: _answered_after_a_push()},
        unreadable=150,
    )

    code = main(["review-followup", "147", "150", *MARK_ALL])

    err = capsys.readouterr().err.splitlines()
    assert "error: could not read pull request 150: HTTP 502" in err
    assert "error: nothing was recorded" in err
    assert (code, _recorded(), posted) == (1, None, [])


def test_marking_read_twice_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The second run of the same command finds every id already recorded,
    leaves the record as it was, and reports as the first did."""
    _github(monkeypatch, {147: _answered_after_a_push()})
    first = main(["review-followup", "147", *MARK_ALL])
    first_out = capsys.readouterr().out.splitlines()
    record = handled_path().read_bytes()

    second = main(["review-followup", "147", *MARK_ALL])

    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [
        "already recorded: ack1",
        "already recorded: ack2",
        "already recorded: r1",
    ]
    assert out[3:] == first_out[3:]
    assert (second, handled_path().read_bytes()) == (first, record)


@pytest.mark.parametrize(
    ("flags", "notice"),
    [
        pytest.param((), False, id="no notice"),
        pytest.param(("--wait",), False, id="no notice, --wait"),
        pytest.param(("--wait",), True, id="rate limited, --wait"),
    ],
)
def test_marking_read_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
    notice: bool,
) -> None:
    """Saying what was read must not spend the hour's one review as a side
    effect, or sleep out a limit to spend it later: a plain run after the
    record asks (spec 043 amendment). This is not a dry run, so without that
    rule the open pull request here is asked for its review."""
    comments: list[dict[str, object]] = []
    if notice:
        comments.append(
            {
                "body": (
                    "<!-- This is an auto-generated comment: rate limited by coderabbit.ai -->\n"
                    "> **Next review available in:** **38 minutes**"
                ),
                "user": {"login": "coderabbitai[bot]"},
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
    posted = _github(monkeypatch, {147: _answered_after_a_push("OPEN")}, comments=comments)

    def no_sleep(_seconds: float) -> None:
        raise AssertionError("--mark-read waited out a limit")

    monkeypatch.setattr("time.sleep", no_sleep)

    code = main(["review-followup", "147", *flags, *MARK_ALL])

    out = capsys.readouterr().out.splitlines()
    if notice:
        assert any(line.startswith("PR #147: rate limited, ") for line in out)
    else:
        assert "PR #147: the head has moved since the last review" in out
    assert (code, posted) == (2, [])


def test_a_dry_run_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Beside --mark-read, --dry-run checks the ids, says what it would
    record, and writes nothing. The rest is a plain dry run's, over the
    record as it stands."""
    _github(monkeypatch, {147: _answered_after_a_push()})

    code = main(["review-followup", "147", "--dry-run", *MARK_ALL])

    out = capsys.readouterr().out.splitlines()
    assert out[:3] == [
        "would record as read: ack1",
        "would record as read: ack2",
        "would record as read: r1",
    ]
    assert (code, _recorded()) == (3, None)


def test_marking_read_leaves_a_truncated_pull_request_outstanding(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """What a bounded query did not read cannot have been read, so recording
    every id the run printed leaves the pull request outstanding (spec 045),
    and truncation, having no id, gets no `once read` line."""
    _github(monkeypatch, {147: _answered_after_a_push(more_threads=True)})

    code = main(["review-followup", "147", *MARK_ALL])

    out = capsys.readouterr().out.splitlines()
    assert "PR #147: a bounded query had another page, so this is not a full picture" in out
    assert not any("once read" in line for line in out)
    assert (code, _recorded()) == (3, ["ack1", "ack2", "r1"])


@pytest.mark.parametrize("problem", ["cannot be read", "cannot be written"])
def test_a_record_it_cannot_use_is_left_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    problem: str,
) -> None:
    """Writing over a record that cannot be read loses every id it held, and
    a record that cannot be written must not report success."""
    _github(monkeypatch, {147: _answered_after_a_push()})
    if problem == "cannot be read":
        kept = handled_path()
        kept.parent.mkdir(parents=True)
        kept.write_text("{not json", encoding="utf-8")
        expected = f"error: nothing was recorded; the record at {kept} cannot be read"
    else:
        kept = tmp_path / "occupied"
        kept.write_text("a file where the data directory goes", encoding="utf-8")
        monkeypatch.setenv("HARRIER_DATA_DIR", str(kept))
        expected = f"error: nothing was recorded; could not write {handled_path()}"
    before = kept.read_bytes()

    code = main(["review-followup", "147", *MARK_ALL])

    assert expected in capsys.readouterr().err.splitlines()
    assert (code, kept.read_bytes()) == (1, before)


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
