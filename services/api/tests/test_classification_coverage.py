"""Classification coverage (ADR-002 as revised by ADR-008, docs/privacy-plan.md).

Asserts that config/data-classification.json and .gitignore agree: never-in-git
paths are ignored and absent from the index. Personal data lives in the local
database (ADR-008); there is no encrypted-in-repo class.
"""

import fnmatch
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def _never_in_git_patterns() -> list[str]:
    raw = json.loads((REPO_ROOT / "config" / "data-classification.json").read_text())
    return raw["never_in_git"]["patterns"]


def _matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
            return True
    return False


def test_never_in_git_paths_are_absent_from_index() -> None:
    patterns = _never_in_git_patterns()
    for path in _tracked_files():
        assert not _matches(path, patterns), f"{path} is tracked but classified never-in-git"


def test_never_in_git_paths_are_gitignored() -> None:
    for pattern in _never_in_git_patterns():
        probe = pattern[:-3] + "/probe" if pattern.endswith("/**") else pattern
        result = subprocess.run(
            ["git", "check-ignore", "-q", probe], cwd=REPO_ROOT, capture_output=True
        )
        assert result.returncode == 0, (
            f"probe {probe!r} for never-in-git pattern {pattern!r} is not gitignored"
        )


def test_no_encrypted_layer_remains() -> None:
    """ADR-008 removed the encrypted-in-repo class; its artifacts must not return."""
    assert not (REPO_ROOT / ".sops.yaml").exists(), ".sops.yaml returned; ADR-008 removed it"
    tracked = _tracked_files()
    offenders = [p for p in tracked if p.startswith("private/") or ".enc." in p]
    assert not offenders, f"encrypted-layer artifacts tracked again: {offenders}"
