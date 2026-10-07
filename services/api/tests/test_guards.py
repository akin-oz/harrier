"""The guards deny the bypasses that were proven against them (spec 045).

Each case here is a bypass the readiness review executed, not one imagined.
A guard that reports success while doing nothing is worse than no guard: it
occupies the place where a working one would go.

The guards are shell scripts fed a JSON tool-call on stdin, so these run them
the way the harness does rather than reimplementing their logic. A test that
grepped the script source would pass for the wrong reason and break on a
wrapped line, which is the failure mode `.ai/rules/review-response.md` calls
out by name.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import time
from pathlib import Path

import pytest

from harrier.demo import repo_root

ROOT = repo_root()
HOOKS = ROOT / ".claude" / "hooks"

GIT_IDENTITY = [
    "-c",
    "user.email=test@example.com",
    "-c",
    "user.name=Test",
    "-c",
    "commit.gpgsign=false",
]


def git(repo: Path, *args: str) -> str:
    """git in a throwaway repo. The identity flags are not decoration: a CI
    runner has no user.email, so `git commit` exits 128 there and passes
    locally, which is how both of these tests went green before CI saw them."""
    result = subprocess.run(
        ["git", *GIT_IDENTITY, *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


def a_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    return repo


DENY = 2
ALLOW = 0


def run_guard(script: str, command: str) -> int:
    payload = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        ["bash", str(HOOKS / script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    return result.returncode


# The bundled short form is the one that got through: git accepts -nm "msg",
# and the old pattern required whitespace or end-of-line straight after the n.
#
# EVERY case carries a valid Spec trailer on purpose. Without one the guard
# denies for the missing trailer instead, and the test passes while proving
# nothing about the bypass. The first draft of this list made exactly that
# mistake and the -nm case went green against the unfixed guard.
BYPASSES = [
    'git commit -nm "message Spec: 045"',
    'git commit --no-verify -m "message" -m "Spec: 045"',
    'git commit -n -m "message" -m "Spec: 045"',
    'git commit -m "message" -m "Spec: 045" --no-verify',
    'git -c core.hooksPath=/dev/null commit -m "m" -m "Spec: 045"',
    'git --git-dir=/tmp/x commit -m "m" -m "Spec: 045"',
    # Spellings that skip the hooks and that the whole-string pattern let
    # through. Each skipped a failing pre-commit hook under git 2.43 in a
    # throwaway repository (spec 045's amendment on the commit guard).
    'git commit "-n" -m "message" -m "Spec: 045"',
    'git commit -nm"a b" -m "Spec: 045"',
    'git commit -m "message" -m "Spec: 045" -n; echo done',
    'git commit --no-veri -m "message" -m "Spec: 045"',
    '(git commit -m "message" -m "Spec: 045" -n)',
    'x=$(git commit -m "message" -m "Spec: 045" -n)',
    # Denied before by the whole-string match, and reading only a commit's own
    # words must not let them through: a substitution or a redirection does
    # not end the words, text another command runs is still checked, and so
    # are the arguments that text receives.
    'git commit -m "message" $(true) -n -m "Spec: 045"',
    'git commit -m "message" -m "Spec: 045" &>/dev/null -n',
    "bash -c \"git commit -n -m 'message' -m 'Spec: 045'\"",
    "bash <<'EOF'\ngit commit -n -m 'message' -m 'Spec: 045'\nEOF",
    "bash -c 'git commit \"$@\"' _ -n -m 'message' -m 'Spec: 045'",
    "bash -s -- -n -m 'message' -m 'Spec: 045' <<'EOF'\ngit commit \"$@\"\nEOF",
    'f() { git commit "$@"; }; f -n -m "message" -m "Spec: 045"',
    # A commit after git's global options, with git or commit quoted, or with
    # commit after a line continuation. The test for whether a command commits
    # needed git and commit side by side on one line, so each of these skipped
    # a failing pre-commit hook under git 2.43 and passed the guard with a
    # trailer (spec 045's amendment on git's global options). The --git-dir
    # form was denied already, by the git dir check.
    'git -C . commit -n -m "message" -m "Spec: 045"',
    'git -c user.name=x commit -n -m "message" -m "Spec: 045"',
    'git --no-pager commit -n -m "message" -m "Spec: 045"',
    'git --work-tree . commit -n -m "message" -m "Spec: 045"',
    'git --work-tree=. commit -n -m "message" -m "Spec: 045"',
    'git --git-dir .git commit -n -m "message" -m "Spec: 045"',
    'git -P commit -n -m "message" -m "Spec: 045"',
    'git -p commit -n -m "message" -m "Spec: 045"',
    'git "commit" -n -m "message" -m "Spec: 045"',
    '"git" commit -n -m "message" -m "Spec: 045"',
    'git \\\ncommit -n -m "message" -m "Spec: 045"',
    "bash -c \"git -C . commit -n -m 'message' -m 'Spec: 045'\"",
    # Text is read a word at a time, and a quoted run is one word, blank and
    # all. A git inside a word that one reading passes over still starts a
    # reading of its own, as the pattern did. Each skipped a failing
    # pre-commit hook under git 2.43 (spec 045's amendment after review of PR
    # #158).
    "bash -c \"git -c 'user.name=a b' commit -n -m 'message' -m 'Spec: 045'\"",
    "bash -c \"git -c alias.x='!git commit -n -q' x -m 'message' -m 'Spec: 045'\"",
    # Text that is not a commit's message, though it sits inside one: a
    # heredoc its cat pipes to a shell, and a command inside ${...}, which the
    # reader does not read. Each skipped a failing pre-commit hook under git
    # 2.43 (spec 045's amendment on the env file check, after review of PR
    # #164).
    "git commit -m \"$(cat <<'EOF' | bash\ngit commit -n -m 'message'\nEOF\n)\" -m 'Spec: 045'",
    "git commit -m \"${X:-$(git commit -n -m 'message')}\" -m 'Spec: 045'",
    # A word that is only a command substitution, where git reads the commit's
    # options: its output is the argument, here -n. The reader kept such a
    # word as empty, so it read as no option at all. Each skipped a failing
    # pre-commit hook under git 2.43 (spec 045's amendment on a command
    # substitution among a commit's options).
    "git commit \"$(cat <<'EOF'\n-n\nEOF\n)\" -m 'message' -m 'Spec: 045'",
    "git commit \"`cat <<'EOF'\n-n\nEOF\n`\" -m 'message' -m 'Spec: 045'",
]

# A guard stricter than the workflow it protects is its own failure.
ORDINARY = [
    'git commit -m "message" -m "Spec: 045"',
    "git commit --amend --no-edit",
    'git commit -am "message" -m "Spec: 045"',
    "git status",
    "git add README.md",
    # Global options before commit, or before another subcommand. The -c value
    # holds the word commit, so the reader runs and must find log after it.
    'git -C dir commit -m "message" -m "Spec: 045"',
    "git -C dir commit -C HEAD",
    "git -C dir log -n 3",
    "git -c commit.gpgsign=false log -n 3",
    # --work-tree takes the next word, commit here, as its value, so the
    # subcommand is log (spec 045's amendment after review of PR #158).
    "git --work-tree commit log -n 1",
    "bash -c 'git --work-tree commit log -n 1'",
    # A substitution that gives a value, not an option: the value of -m, text
    # attached to -m, -am or --message=, and a path after --. None is marked
    # unread (spec 045's amendment on a command substitution among a commit's
    # options). The first is the form this repository's own commits use.
    "git commit -m \"$(cat <<'EOF'\nmessage\n\nSpec: 045\nEOF\n)\"",
    'git commit -m"$(printf message)" -m "Spec: 045"',
    'git commit -am"$(printf message)" -m "Spec: 045"',
    'git commit --message="$(printf message)" -m "Spec: 045"',
    'git commit -m "message" -m "Spec: 045" -- "$(printf README.md)"',
]


@pytest.mark.parametrize("command", BYPASSES)
def test_the_commit_guard_denies_every_proven_bypass(command: str) -> None:
    assert run_guard("guard-commit.sh", command) == DENY, (
        f"guard-commit.sh allowed a hook bypass: {command}"
    )


@pytest.mark.parametrize("command", ORDINARY)
def test_the_commit_guard_allows_ordinary_work(command: str) -> None:
    assert run_guard("guard-commit.sh", command) == ALLOW, (
        f"guard-commit.sh blocked ordinary work: {command}"
    )


# An -n that belongs to another command in the chain, or that sits in the
# commit message. The first is the shape of the command the whole-string match
# denied while committing PR #148 on 2026-10-06. Each carries a Spec trailer,
# so an ALLOW proves the -n check passed rather than that nothing was checked.
N_OUTSIDE_THE_COMMIT = [
    "sed -n 1,6p specs/045-claims-and-gates.md"
    " && git add specs/045-claims-and-gates.md"
    " && git commit -q -F - <<'EOF'\nAmend spec 045\n\nSpec: 045\nEOF",
    'grep -n "Spec" specs/045-claims-and-gates.md && git commit -m "message" -m "Spec: 045"',
    'head -n 3 README.md && git commit -m "message" -m "Spec: 045"',
    'echo -n done && git commit -m "message" -m "Spec: 045"',
    'git log -n 3 && git commit -m "message" -m "Spec: 045"',
    'find specs -name "045-*" && git commit -m "message" -m "Spec: 045"',
    'git commit -m "message" -m "Spec: 045" && git log -n 1',
    'git commit -m "Read sed -n as sed reads it" -m "Spec: 045"',
    "git commit -q -F - <<'EOF'\nThe guard read sed -n and head -n as a bypass.\n\nSpec: 045\nEOF",
    "git commit -q -F - <<'EOF'\nDeny git commit -n and --no-verify.\n\nSpec: 045\nEOF",
    "git commit -m \"$(cat <<'EOF'\nIt's the -n of sed, not a bypass.\n\nSpec: 045\nEOF\n)\"",
]


@pytest.mark.parametrize("command", N_OUTSIDE_THE_COMMIT)
def test_an_n_outside_the_commit_does_not_block_it(command: str) -> None:
    """The guard matched the whole command string, so another command's -n,
    or one in the message, denied an ordinary commit: the failure mode spec
    045 says a guard change must not introduce. It now reads only the words
    the commit receives (spec 045's amendment on the commit guard)."""
    assert run_guard("guard-commit.sh", command) == ALLOW, (
        f"guard-commit.sh blocked ordinary work: {command}"
    )


def test_a_long_message_costs_time_in_proportion_to_its_length(tmp_path: Path) -> None:
    """The first version of the word reader held the command as one string,
    and reading it was quadratic. Under mawk, eight times the message took
    about thirty times as long, 1.1 s for 8000 lines. Under original-awk,
    the codebase macOS awk comes from, a 6000-line message took 16 s. The
    guard runs before every command that mentions a commit.

    mawk is used where it exists, as on the CI runner, so the ratio measures
    the reader rather than whichever awk a machine provides. A linear reader
    stays under eight: eight times the lines, plus the same process start.
    """
    env = dict(os.environ)
    mawk = shutil.which("mawk")
    if mawk:
        (tmp_path / "awk").symlink_to(mawk)
        env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    line = "A line of commit message text that says nothing in particular.\n"

    def seconds(lines: int) -> float:
        command = "git commit -q -F - <<'EOF'\n" + line * lines + "\nSpec: 045\nEOF"
        payload = json.dumps({"tool_input": {"command": command}})
        runs: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            result = subprocess.run(
                ["bash", str(HOOKS / "guard-commit.sh")],
                input=payload,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=env,
                check=False,
            )
            runs.append(time.perf_counter() - start)
            assert result.returncode == ALLOW, result.stderr
        return statistics.median(runs)

    short, longer = seconds(1000), seconds(8000)
    assert longer < 10 * short, f"1000 lines: {short:.3f} s, 8000 lines: {longer:.3f} s"


def test_a_run_of_global_options_costs_time_in_proportion_to_its_length(tmp_path: Path) -> None:
    """The pattern for a commit after global options could read --work-tree
    with or without a value, and mawk tried both ways for each one: 28 of them
    took 1.07 s, and 38 held the guard past 65 s with a hook bypass after
    them. A guard that does not finish cannot deny. Text is now read a word at
    a time (spec 045's amendment after review of PR #158).

    mawk is used where it exists, as on the CI runner. Read a word at a time,
    ten times the options costs about the same as the process start, and the
    old pattern did not finish 40 of them within the timeout.
    """
    env = dict(os.environ)
    mawk = shutil.which("mawk")
    if mawk:
        (tmp_path / "awk").symlink_to(mawk)
        env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"

    def seconds(options: int) -> float:
        command = "git" + " --work-tree" * options + ' status; git commit -n -m "m" -m "Spec: 045"'
        payload = json.dumps({"tool_input": {"command": command}})
        runs: list[float] = []
        for _ in range(3):
            start = time.perf_counter()
            result = subprocess.run(
                ["bash", str(HOOKS / "guard-commit.sh")],
                input=payload,
                capture_output=True,
                text=True,
                cwd=ROOT,
                env=env,
                check=False,
                timeout=30,
            )
            runs.append(time.perf_counter() - start)
            assert result.returncode == DENY, result.stderr
        return statistics.median(runs)

    few, many = seconds(4), seconds(40)
    assert many < 10 * few, f"4 options: {few:.3f} s, 40 options: {many:.3f} s"


def test_checks_that_need_no_reader_do_not_wait_for_it(tmp_path: Path) -> None:
    """The hooksPath check and the env file check ran before any parsing
    until the guard read every command that mentions a commit first, and then
    they waited for it. An awk that never returns in time stands in for a
    reader that is slow or stuck (spec 045's amendment after review of PR
    #158). A commit's env file check needs the reader now, to tell its
    message from its paths, so `git add .env`, which it does not read, stands
    for that check (spec 045's amendment on the env file check)."""
    stuck = tmp_path / "awk"
    stuck.write_text("#!/bin/sh\nexec sleep 30\n")
    stuck.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    for command in (
        'git -c core.hooksPath=/dev/null commit -m "m" -m "Spec: 045"',
        "git add .env",
    ):
        payload = json.dumps({"tool_input": {"command": command}})
        result = subprocess.run(
            ["bash", str(HOOKS / "guard-commit.sh")],
            input=payload,
            capture_output=True,
            text=True,
            cwd=ROOT,
            env=env,
            check=False,
            timeout=10,
        )
        assert result.returncode == DENY, f"{command}: {result.stderr}"


def test_the_commit_guard_still_requires_a_spec_trailer() -> None:
    assert run_guard("guard-commit.sh", 'git commit -m "no trailer here"') == DENY


# A commit after a global option, or with commit quoted, and no trailer. The
# test for whether a command commits needed git and commit side by side, so
# none of these reached the trailer check. And -C <path> would have read as
# the -C <commit> that reuses a message, which the trailer check exempts.
NO_TRAILER_AFTER_GLOBAL_OPTIONS = [
    'git -C dir commit -m "no trailer here"',
    'git --no-pager commit -m "no trailer here"',
    'git "commit" -m "no trailer here"',
    # With a value, --exec-path sets the path and git runs the subcommand
    # after it (spec 045's amendment after review of PR #158).
    'git --exec-path=/usr/lib/git-core commit -m "no trailer here"',
]


@pytest.mark.parametrize("command", NO_TRAILER_AFTER_GLOBAL_OPTIONS)
def test_a_commit_after_global_options_still_requires_a_spec_trailer(command: str) -> None:
    """Spec 045's amendment on git's global options."""
    assert run_guard("guard-commit.sh", command) == DENY, (
        f"guard-commit.sh allowed a commit with no trailer: {command}"
    )


# Options after which git runs help or version in place of the subcommand, or
# prints and exits. Under git 2.43 none of these made a commit in a throwaway
# repository with a staged change (spec 045's amendment after review of PR
# #158). No trailer, so an ALLOW proves the guard read no commit.
NO_SUBCOMMAND_RUNS = [
    'git --help commit -m "no trailer here"',
    'git -h commit -m "no trailer here"',
    'git --version commit -m "no trailer here"',
    'git -v commit -m "no trailer here"',
    'git --exec-path commit -m "no trailer here"',
    'git --html-path commit -m "no trailer here"',
    'git --man-path commit -m "no trailer here"',
    'git --info-path commit -m "no trailer here"',
    'git --list-cmds=main commit -m "no trailer here"',
    "bash -c 'git --help commit -m \"no trailer here\"'",
]


@pytest.mark.parametrize("command", NO_SUBCOMMAND_RUNS)
def test_no_subcommand_runs_after_a_help_or_query_option(command: str) -> None:
    """The reader took every word that starts with - for an option before
    the subcommand, so `git --help commit` needed a trailer."""
    assert run_guard("guard-commit.sh", command) == ALLOW, (
        f"guard-commit.sh read a commit where git runs none: {command}"
    )


# A -C that is not one of the commit's own words: in its message, or in another
# command. Neither reuses a message that carries a trailer, and both passed
# while the exemption read the whole string.
NOT_THE_COMMITS_OWN_C = [
    'git commit -m "pass -C to tar"',
    'tar -C /tmp -cf /dev/null . && git commit -m "no trailer here"',
    # A -C that git reads as the value of another option, or as a path after
    # --. Each passed once the exemption read the commit's own words (spec
    # 045's amendment after review of PR #158).
    'git commit -m "Fix the parser" -m "-C"',
    "git -C . commit -m Fix -m '-C'",
    "git commit -F -C",
    'git commit -m "no trailer here" -- -C',
]


@pytest.mark.parametrize("command", NOT_THE_COMMITS_OWN_C)
def test_the_reuse_exemption_reads_only_the_commits_own_words(command: str) -> None:
    """The trailer check exempts -C <commit>, which reuses a message. Read from
    the whole string, -C was also git's global -C <path>, so it now counts only
    among the commit's own words (spec 045's amendment on git's global
    options), and only where git reads it as an option: not as the value of
    another option, nor as a path after `--` (the amendment after review of PR
    #158). `git -C dir commit -C HEAD` in ORDINARY is the case it keeps."""
    assert run_guard("guard-commit.sh", command) == DENY, (
        f"guard-commit.sh exempted a commit from its trailer: {command}"
    )


def test_the_commit_guard_still_refuses_to_stage_an_env_file() -> None:
    assert run_guard("guard-commit.sh", "git add .env") == DENY
    assert run_guard("guard-commit.sh", "git add .env.example") == ALLOW
    assert run_guard("guard-commit.sh", 'git -C . commit -m "message" -m "Spec: 045" .env') == DENY


# A commit whose message names an env file, in each form the message takes.
# None stages or commits one, and each was denied while the check read the
# whole command (spec 045's amendment on the env file check). Each carries a
# trailer, so an ALLOW proves the env file check passed.
ENV_FILE_ONLY_IN_A_MESSAGE = [
    'git commit -m "Stop reading .env in tests" -m "Spec: 045"',
    "git commit -q -F - <<'EOF'\nIgnore .env.local in the loader\n\nSpec: 045\nEOF",
    "git commit -m \"$(cat <<'EOF'\nKeep .env out of git\n\nSpec: 045\nEOF\n)\"",
    'git add src/app.py && git commit -m "Read .env.local lazily" -m "Spec: 045"',
    'git commit -am "Load .env lazily" -m "Spec: 045"',
    'git commit -m"Load .env lazily" -m "Spec: 045"',
    'git commit --message="Load .env lazily" -m "Spec: 045"',
    'git commit --message "Load .env lazily" -m "Spec: 045"',
    'git -C . commit -m "Load .env lazily" -m "Spec: 045"',
    "git commit -F - <<< 'Load .env lazily\n\nSpec: 045'",
    'git commit -m"$(cat <<\'EOF\'\nKeep .env out of git\nEOF\n)" -m "Spec: 045"',
    "git commit --file - <<'EOF'\nIgnore .env.local in the loader\n\nSpec: 045\nEOF",
]


@pytest.mark.parametrize("command", ENV_FILE_ONLY_IN_A_MESSAGE)
def test_an_env_file_named_only_in_a_message_does_not_block_the_commit(command: str) -> None:
    """The env file check matched the whole command, so a message that only
    names an env file denied the commit that carried it."""
    assert run_guard("guard-commit.sh", command) == ALLOW, (
        f"guard-commit.sh blocked a commit for its message: {command}"
    )


# Commands that stage an env file, commit one as a path, or read one into the
# message. Each must still be denied by the env file check itself, whatever
# the bypass and trailer checks would say (spec 045's amendment on the env
# file check).
ENV_FILE_STAGED_OR_READ = [
    "git add .env",
    "git add -f .env.local",
    'git add .env && git commit -m "message" -m "Spec: 045"',
    'git -C . commit -m "message" -m "Spec: 045" .env',
    'git commit -m "message" -m "Spec: 045" -- .env.local',
    'git commit -m "message" -m "Spec: 045" -- -m .env',
    "git commit -F .env.local",
    'git commit -m "$(cat .env)" -m "Spec: 045"',
    "git commit -F - < .env.local",
    "git commit -F - <<EOF\n$(cat .env)\nEOF",
    'bash -c "git add .env"',
    "git add .env.example; git commit -m x .env.prod -m 'Spec: 045'",
    # A command substitution gives message text only inside the message.
    # Elsewhere its output names a file or a path, so a cat there is no
    # message. Under git 2.43 the first four wrote the env file into the
    # commit message (review of PR #164).
    'git commit -F "$(cat <<< .env)"',
    "git commit -F - < \"$(cat <<'EOF'\n.env.local\nEOF\n)\"",
    'git commit -m "$(cat "$(cat <<< .env)")" -m "Spec: 045"',
    'git commit -m "$(cat <<\'EOF\' | xargs cat\n.env\nEOF\n)" -m "Spec: 045"',
    "git commit -m x -m 'Spec: 045' --pathspec-from-file=- <<'EOF'\n.env\nEOF",
    "git commit -m x -m 'Spec: 045' --pathspec-from-file=<(cat <<< .env)",
    'git commit -m "${X:-$(cat .env)}" -m "Spec: 045"',
]


@pytest.mark.parametrize("command", ENV_FILE_STAGED_OR_READ)
def test_the_env_file_check_still_denies_a_staged_or_read_env_file(command: str) -> None:
    """Reading past a commit's message must not let an env file through:
    named as a path, read into the message by -F or a redirection, or written
    into it by a command substitution."""
    payload = json.dumps({"tool_input": {"command": command}})
    result = subprocess.run(
        ["bash", str(HOOKS / "guard-commit.sh")],
        input=payload,
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == DENY, f"guard-commit.sh allowed an env file: {command}"
    assert "refusing to stage/commit .env* files" in result.stderr, (
        f"guard-commit.sh denied {command!r} for something else: {result.stderr}"
    )


def test_the_turn_gate_sees_a_file_that_is_only_added(tmp_path: Path) -> None:
    """`git diff --name-only HEAD` lists tracked changes only, so a turn that
    added a new module and nothing else reported no change and ran no gate:
    blindest exactly when the most new code had arrived.

    Runs the hook for real against a throwaway repository holding one
    untracked Python file, with a stub `just` on PATH so the assertion is that
    the gate was invoked rather than that the real suite passed. The first
    version grepped the script's source, which the review-response rule calls
    a last resort: it breaks on a wrapped line and passes for the wrong reason
    (review of PR #50).
    """
    repo = a_repo(tmp_path, "repo")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    (repo / "added.py").write_text("x = 1\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "invoked"
    stub = bindir / "just"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--summary" ]; then echo gate; exit 0; fi\n'
        f'printf "%s" "$*" >> {marker}\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)

    result = subprocess.run(
        ["bash", str(HOOKS / "verify-on-stop.sh")],
        input=json.dumps({"stop_hook_active": False}),
        capture_output=True,
        text=True,
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "CLAUDE_PROJECT_DIR": str(repo),
        },
    )

    assert marker.exists(), (
        f"the gate never ran for a turn that only added a file (exit {result.returncode})"
    )
    assert "gate" in marker.read_text(encoding="utf-8")


def test_the_spec_structure_check_refuses_an_empty_directory(tmp_path: Path) -> None:
    """Zero specs found reported success, so a checker pointed at the wrong
    directory read as "all clean". A gate whose file set is empty has not
    passed; it has not run."""
    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "check_spec_structure.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 2, "an empty spec directory reported success"


def test_the_spec_structure_check_reads_subdirectories(tmp_path: Path) -> None:
    """The flat glob made a spec filed one level down invisible."""
    nested = tmp_path / "archive"
    nested.mkdir()
    (nested / "099-incomplete.md").write_text("# Spec 099\n\nno headings\n", encoding="utf-8")
    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "check_spec_structure.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 1, "a spec in a subdirectory was not checked"
    assert "099-incomplete.md" in result.stdout


def test_the_turn_gate_gates_a_shell_guard_change() -> None:
    """The extension filter excluded .sh, so a turn that changed only a guard
    script ran no gate: the gate was blindest on the files that are the gate
    (review of PR #50)."""
    source = (HOOKS / "verify-on-stop.sh").read_text(encoding="utf-8")
    assert "|sh)$" in source, "a shell guard change runs no gate"


def test_the_spec_gate_refuses_a_base_it_cannot_resolve() -> None:
    """Falling back to head^ checked the tip commit only, so a force push
    could land earlier commits with no approved-spec trailer and the gate
    reported success on the one commit it looked at (review of PR #50)."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.strip()
    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "spec_gate.py"), str(ROOT), "deadbeef" * 5, head],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 2, "an unresolvable base did not fail the gate"
    assert "does not resolve" in result.stderr


def test_a_null_base_checks_every_commit_not_on_main(tmp_path: Path) -> None:
    """A branch's first push has no previous tip, and the old fallback checked
    one commit there, so everything before the tip went unchecked.

    Built against a synthetic repository with a known shape. The first version
    read this repository, which passed locally and failed in CI because CI
    checks out a merge ref whose range is a single trailer-less commit: a test
    that depended on the topology it happened to run in.
    """
    repo = a_repo(tmp_path, "nullbase")
    (repo / "spec.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")

    git(repo, "checkout", "-q", "-b", "work")
    for n in (1, 2):
        (repo / f"f{n}.txt").write_text("x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"work {n}\n\nSpec: 044")
    head = git(repo, "rev-parse", "HEAD").strip()

    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "spec_gate.py"), str(repo), "0" * 40, head],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    # Both work commits are in the range, not just the tip.
    assert result.stdout.count("no 'Spec: NNN' trailer") == 0, result.stdout
    assert result.stdout.count("work 1") == 1, f"the first commit was skipped:\n{result.stdout}"
    assert result.stdout.count("work 2") == 1, result.stdout
