"""Encryption and classification coverage (ADR-002, docs/privacy-plan.md section 4).

Asserts that config/data-classification.json, .sops.yaml, and .gitignore agree:
encrypted-in-repo paths are covered by SOPS rules and are actually ciphertext in
the index; never-in-git paths are ignored and absent from the index.
"""

import fnmatch
import json
import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line]


def _classification() -> dict[str, list[str]]:
    raw = json.loads((REPO_ROOT / "config" / "data-classification.json").read_text())
    return {
        "encrypted": raw["encrypted_in_repo"]["patterns"],
        "never": raw["never_in_git"]["patterns"],
        "plaintext_forbidden": raw["plaintext_forbidden_under"]["patterns"],
    }


def _sops_regexes() -> list[re.Pattern[str]]:
    doc = yaml.safe_load((REPO_ROOT / ".sops.yaml").read_text())
    return [re.compile(rule["path_regex"]) for rule in doc["creation_rules"]]


def _matches(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # fnmatch's ** does not cross into "no directory"; also match the bare prefix.
        if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
            return True
    return False


def test_every_encrypted_pattern_has_a_sops_rule() -> None:
    classes = _classification()
    regexes = _sops_regexes()
    for pattern in classes["encrypted"]:
        probe = pattern.replace("**/", "sample/").replace("*", "sample")
        assert any(r.search(probe) for r in regexes), (
            f"classification pattern {pattern!r} (probe {probe!r}) matches no .sops.yaml rule"
        )


def test_tracked_private_files_are_ciphertext() -> None:
    classes = _classification()
    for path in _tracked_files():
        if not _matches(path, classes["plaintext_forbidden"]):
            continue
        assert _matches(path, classes["encrypted"]), (
            f"{path} is tracked under private/ but does not match an encrypted-in-repo "
            f"pattern; plaintext is forbidden there"
        )
        content = (REPO_ROOT / path).read_text(errors="replace")
        has_envelope = "ENC[" in content or '"mac"' in content or "mac:" in content
        assert "sops" in content and has_envelope, f"{path} does not look like SOPS ciphertext"


def test_never_in_git_paths_are_absent_and_ignored() -> None:
    classes = _classification()
    tracked = _tracked_files()
    for path in tracked:
        assert not _matches(path, classes["never"]), (
            f"{path} is tracked but classified never-in-git"
        )
    for pattern in classes["never"]:
        probe = pattern[:-3] + "/probe" if pattern.endswith("/**") else pattern
        result = subprocess.run(
            ["git", "check-ignore", "-q", probe], cwd=REPO_ROOT, capture_output=True
        )
        assert result.returncode == 0, (
            f"probe {probe!r} for never-in-git pattern {pattern!r} is not gitignored"
        )
