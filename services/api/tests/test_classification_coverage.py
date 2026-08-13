"""Classification coverage (ADR-002 as revised by ADR-008, docs/privacy-plan.md).

Asserts that config/data-classification.json and .gitignore agree: never-in-git
paths are ignored and absent from the index. Personal data lives in the local
database (ADR-008); there is no encrypted-in-repo class.
"""

import fnmatch
import json
import subprocess
from pathlib import Path

import pytest

from harrier.db import data_dir

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


def test_every_example_config_has_its_real_name_classified() -> None:
    """The README tells a user to copy every config/*.example.* to its real
    name and says "either way they are gitignored". Six of the ten were not:
    the candidate profile, the resume content, the application narrative in
    both formats, the interview story seeds, and the outreach defaults all
    staged cleanly under `git add -A`.

    Derived from the tree rather than listed, so adding an example file with
    no classification entry fails here instead of leaking.
    """
    patterns = _never_in_git_patterns()
    missing: list[str] = []
    for example in sorted((REPO_ROOT / "config").rglob("*.example.*")):
        real = example.with_name(example.name.replace(".example.", ".", 1))
        rel = real.relative_to(REPO_ROOT).as_posix()
        if not _matches(rel, patterns):
            missing.append(rel)
    assert not missing, f"example configs whose real name is unclassified: {missing}"


def test_data_dir_is_inside_the_repository_whatever_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data_dir()` returned Path("data"), resolved against the process's
    working directory. `just export` cd's into services/api, so it opened a
    second database and wrote a header-only CSV, exit code 0 both times:
    two databases where ADR-003 says one, and a never-in-git directory sitting
    outside the root-anchored .gitignore patterns.

    Runs from a directory that is not the repository root, which is the
    condition the defect needed and which no existing test created.
    """
    monkeypatch.delenv("HARRIER_DATA_DIR", raising=False)
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.chdir(tmp_path)

    resolved = data_dir().resolve()

    assert resolved == (REPO_ROOT / "data").resolve(), (
        f"data_dir() followed the working directory to {resolved}"
    )
    probe = resolved.relative_to(REPO_ROOT).as_posix() + "/probe"
    result = subprocess.run(
        ["git", "check-ignore", "-q", probe], cwd=REPO_ROOT, capture_output=True
    )
    assert result.returncode == 0, f"{probe} is where data lands and it is not gitignored"


def test_no_encrypted_layer_remains() -> None:
    """ADR-008 removed the encrypted-in-repo class; its artifacts must not return."""
    assert not (REPO_ROOT / ".sops.yaml").exists(), ".sops.yaml returned; ADR-008 removed it"
    tracked = _tracked_files()
    offenders = [p for p in tracked if p.startswith("private/") or ".enc." in p]
    assert not offenders, f"encrypted-layer artifacts tracked again: {offenders}"
