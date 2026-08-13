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
import subprocess
from pathlib import Path

import pytest

from harrier.demo import repo_root

ROOT = repo_root()
HOOKS = ROOT / ".claude" / "hooks"

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
]

# A guard stricter than the workflow it protects is its own failure.
ORDINARY = [
    'git commit -m "message" -m "Spec: 045"',
    "git commit --amend --no-edit",
    'git commit -am "message" -m "Spec: 045"',
    "git status",
    "git add README.md",
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


def test_the_commit_guard_still_requires_a_spec_trailer() -> None:
    assert run_guard("guard-commit.sh", 'git commit -m "no trailer here"') == DENY


def test_the_commit_guard_still_refuses_to_stage_an_env_file() -> None:
    assert run_guard("guard-commit.sh", "git add .env") == DENY
    assert run_guard("guard-commit.sh", "git add .env.example") == ALLOW


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
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=repo, check=True)
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


def test_a_null_base_checks_every_commit_not_on_main() -> None:
    """A branch's first push has no previous tip. Checking one commit there
    would skip the rest of the branch."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.strip()
    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "spec_gate.py"), str(ROOT), "0" * 40, head],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.stdout.count("ok:") > 1, "a null base checked only the tip"
