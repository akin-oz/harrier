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


def test_the_turn_gate_sees_a_file_that_is_only_added() -> None:
    """`git diff --name-only HEAD` lists tracked changes only, so a turn that
    added a new module and nothing else reported no change and ran no gate:
    blindest exactly when the most new code had arrived.

    Asserted on the command the script uses, because running the real gate
    here would take minutes and recurse into this suite.
    """
    source = (HOOKS / "verify-on-stop.sh").read_text(encoding="utf-8")
    assert "ls-files --others --exclude-standard" in source, (
        "verify-on-stop.sh no longer looks at untracked files"
    )


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


def test_the_spec_gate_survives_a_base_that_no_longer_exists() -> None:
    """github.event.before names a commit that a force push has removed, and
    a history rewrite makes that certain rather than theoretical. Falling back
    to the head's parent checks the pushed commit instead of skipping."""
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
    ).stdout.strip()

    # Run the script the way CI runs it, rather than importing a helper out of
    # it: the gate's behaviour is what matters, and an import would keep
    # passing if the workflow stopped calling it.
    for unusable in ("0" * 40, "deadbeef" * 5):
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "spec_gate.py"), str(ROOT), unusable, head],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert "could not run" not in result.stderr, (
            f"the gate died on an unusable base {unusable[:8]}: {result.stderr.strip()}"
        )
        assert result.stdout.strip(), "the gate checked nothing and said nothing"
