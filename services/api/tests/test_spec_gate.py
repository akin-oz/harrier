"""The spec gate can say no, including to the branch asking (spec 039).

Every test here builds a real git repository and runs the real script against
it. A gate tested against a mock of the thing it gates proves nothing about
the gate: the defect being closed was precisely that the resolution read the
wrong tree, and no amount of stubbing `git` would have shown that.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[3] / "scripts" / "spec_gate.py"

APPROVED_SPEC = """---
spec: 099
title: A spec for the gate's own tests
status: accepted
approved: yes
---

# Spec 099
"""

UNAPPROVED_SPEC = APPROVED_SPEC.replace("approved: yes", "approved: no")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, body in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    place = tmp_path / "repo"
    place.mkdir()
    git(place, "init", "-q", "-b", "main")
    git(place, "config", "user.email", "gate@example.com")
    git(place, "config", "user.name", "Gate Test")
    commit(place, "chore: root", {"README.md": "root\n"})
    return place


def run_gate(repo: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), str(repo), base, head],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the defect this closes ---------------------------------------------------


def test_a_branch_cannot_approve_the_spec_it_is_implementing(repo: Path) -> None:
    """The shape the old gate passed: approve in one commit, implement in the
    next, inside one pull request. It read the spec from the branch asking for
    approval, so the answer was always yes."""
    base = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "docs(spec): approve 099\n\nSpec: 099", {"specs/099-gate.md": APPROVED_SPEC})
    head = commit(repo, "feat: the work\n\nSpec: 099", {"src/thing.py": "x = 1\n"})

    result = run_gate(repo, base, head)
    assert result.returncode == 1
    assert "not 'approved: yes' on the base branch" in result.stdout
    assert head[:9] in result.stdout


def test_a_spec_approved_on_the_base_passes(repo: Path) -> None:
    commit(repo, "docs(spec): approve 099\n\nSpec: 099", {"specs/099-gate.md": APPROVED_SPEC})
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(repo, "feat: the work\n\nSpec: 099", {"src/thing.py": "x = 1\n"})

    result = run_gate(repo, base, head)
    assert result.returncode == 0, result.stdout


def test_flipping_approval_inside_the_pull_request_is_refused(repo: Path) -> None:
    """Not only adding a spec: flipping an existing `approved: no` is the
    same move and has to fail the same way."""
    commit(repo, "docs(spec): draft 099\n\nSpec: 099", {"specs/099-gate.md": UNAPPROVED_SPEC})
    base = git(repo, "rev-parse", "HEAD").strip()
    commit(repo, "docs(spec): approve 099\n\nSpec: 099", {"specs/099-gate.md": APPROVED_SPEC})
    head = commit(repo, "feat: the work\n\nSpec: 099", {"src/thing.py": "x = 1\n"})

    result = run_gate(repo, base, head)
    assert result.returncode == 1


def test_the_message_says_which_spec_and_where_approval_must_live(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(repo, "feat: the work\n\nSpec: 099", {"src/thing.py": "x = 1\n"})

    result = run_gate(repo, base, head)
    assert "spec 099" in result.stdout
    assert "specs/099-*.md" in result.stdout
    assert "base" in result.stdout


# --- the exemption that makes the rule usable ---------------------------------


def test_a_governance_commit_may_carry_its_own_approval(repo: Path) -> None:
    """Otherwise approval could never reach the base: the approving commit
    would need its own spec approved first."""
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(
        repo, "docs(spec): approve 099\n\nSpec: 099", {"specs/099-gate.md": APPROVED_SPEC}
    )

    result = run_gate(repo, base, head)
    assert result.returncode == 0, result.stdout
    assert "governance commit" in result.stdout


def test_approving_and_implementing_in_one_commit_is_refused(repo: Path) -> None:
    """The exemption is for commits that touch only specs/. One commit that
    approves and implements is a proposal granting itself approval, whatever
    its message says."""
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(
        repo,
        "feat: approve and do it\n\nSpec: 099",
        {"specs/099-gate.md": APPROVED_SPEC, "src/thing.py": "x = 1\n"},
    )

    result = run_gate(repo, base, head)
    assert result.returncode == 1


# --- the checks the old gate already made, kept -------------------------------


def test_a_commit_with_no_trailer_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(repo, "feat: untraceable", {"src/thing.py": "x = 1\n"})
    result = run_gate(repo, base, head)
    assert result.returncode == 1
    assert "no 'Spec: NNN' trailer" in result.stdout


def test_a_trailer_naming_no_spec_file_fails(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    head = commit(repo, "feat: the work\n\nSpec: 404", {"src/thing.py": "x = 1\n"})
    result = run_gate(repo, base, head)
    assert result.returncode == 1


def test_a_pull_request_with_no_commits_passes(repo: Path) -> None:
    head = git(repo, "rev-parse", "HEAD").strip()
    result = run_gate(repo, head, head)
    assert result.returncode == 0
    assert "No commits to check" in result.stdout


def test_merge_commits_are_not_checked(repo: Path) -> None:
    """A merge carries no trailer of its own and inherits its parents'."""
    commit(repo, "docs(spec): approve 099\n\nSpec: 099", {"specs/099-gate.md": APPROVED_SPEC})
    base = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "feat: side work\n\nSpec: 099", {"src/side.py": "x = 1\n"})
    git(repo, "checkout", "-q", "main")
    commit(repo, "feat: main work\n\nSpec: 099", {"src/main.py": "x = 1\n"})
    git(repo, "merge", "--no-ff", "-q", "-m", "Merge side", "side")
    head = git(repo, "rev-parse", "HEAD").strip()

    result = run_gate(repo, base, head)
    assert result.returncode == 0, result.stdout
