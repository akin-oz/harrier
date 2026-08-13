"""A spec that loses a section fails rather than merging (spec 041).

Spec 006 shipped with its `## Problem` gone, and both of its claims went with
it. The spec gate reads frontmatter and the artifact check reads compiled
output; neither reads structure, so the damage merged unnoticed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK = REPO_ROOT / "scripts" / "check_spec_structure.py"
SPECS = Path(__file__).resolve().parents[3] / "specs"

WHOLE = """---
spec: 099
title: A whole spec
approved: yes
---

# Spec 099

## Problem

Something.

## Scope

Something.

## Acceptance criteria

- [ ] something

## Proof / origin

Somewhere.

## Out of scope

Something else.
"""


def run(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), str(directory)], capture_output=True, text=True, check=False
    )


def test_a_damaged_heading_fails(tmp_path: Path) -> None:
    """The fixture is spec 006's actual damage: the heading collapsed into
    the prose below it, so the section is gone but the words remain."""
    (tmp_path / "099-damaged.md").write_text(
        WHOLE.replace("## Problem\n\nSomething.", "Something."), encoding="utf-8"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "missing '## Problem'" in result.stdout


def test_a_whole_spec_passes(tmp_path: Path) -> None:
    (tmp_path / "099-whole.md").write_text(WHOLE, encoding="utf-8")
    assert run(tmp_path).returncode == 0


# Declared here rather than imported from the implementation. The previous
# version looped over `module.REQUIRED`, so removing a heading from the
# required set removed it from the test too and the suite stayed green: a test
# that could not fail, whose docstring claimed the opposite (spec 045).
EXPECTED_HEADINGS = (
    "## Problem",
    "## Scope",
    "## Acceptance criteria",
    "## Proof / origin",
    "## Out of scope",
)


def test_the_required_set_is_the_one_this_suite_expects() -> None:
    """Shrinking REQUIRED is now a failure here rather than a silently
    smaller test.

    Read out of the source rather than imported, because importing it is what
    made the old test unable to fail.
    """
    source = CHECK.read_text(encoding="utf-8")
    block = re.search(r"REQUIRED[^=]*=\s*\((.*?)\)", source, re.DOTALL)
    assert block is not None, "check_spec_structure.py no longer declares REQUIRED"
    declared = tuple(re.findall(r'"([^"]+)"', block.group(1)))
    assert declared == EXPECTED_HEADINGS


@pytest.mark.parametrize("heading", EXPECTED_HEADINGS)
def test_every_required_heading_is_checked(heading: str, tmp_path: Path) -> None:
    """Each one individually, so a heading cannot be quietly dropped from the
    required set without a test noticing."""
    damaged = WHOLE.replace(f"{heading}\n", "")
    (tmp_path / "099-one.md").write_text(damaged, encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1, f"removing {heading} did not fail the check"
    assert f"missing '{heading}'" in result.stdout


def test_a_spec_may_add_sections(tmp_path: Path) -> None:
    """The check asserts the required headings are present, not that no
    others are. A rigid version would forbid the amendment sections this
    repository uses to record decisions."""
    (tmp_path / "099-extra.md").write_text(
        WHOLE + "\n## What the implementation decided\n\nThings.\n", encoding="utf-8"
    )
    assert run(tmp_path).returncode == 0


def test_a_heading_inside_prose_does_not_count(tmp_path: Path) -> None:
    """Matched at the start of a line. Mentioning `## Problem` in a sentence
    is not having the section."""
    (tmp_path / "099-mention.md").write_text(
        WHOLE.replace("## Problem\n\nSomething.", "The spec had no ## Problem section."),
        encoding="utf-8",
    )
    assert run(tmp_path).returncode == 1


def test_the_committed_specs_all_carry_their_headings() -> None:
    """Including spec 006, which is what this spec repaired."""
    result = run(SPECS)
    assert result.returncode == 0, result.stdout


def test_every_test_a_spec_names_actually_exists() -> None:
    """A spec's acceptance table is only worth what its proofs are worth.

    Three specs cited five test symbols that did not exist, and one of them
    was the sole support for a criterion whose whole argument was "asserted by
    the two sink tests above" (spec 045). A renamed test breaks the proof
    silently, because nothing reads these names but a person.
    """
    defined: set[str] = set()
    for path in (REPO_ROOT / "services" / "api" / "tests").rglob("*.py"):
        defined.update(re.findall(r"def (test_[A-Za-z0-9_]+)", path.read_text(encoding="utf-8")))
    for path in (REPO_ROOT / "apps" / "web" / "src").rglob("*.test.tsx"):
        text = path.read_text(encoding="utf-8")
        defined.update(re.findall(r"(?:it|test)\(\s*[\"'`]([^\"'`]+)", text))

    # Both forms: a bare `test_x` and a qualified `path/to/file.py::test_x`.
    # Qualifying the references in specs 044 and 045 made them invisible to the
    # bare-symbol pattern, so the two specs this check exists for became the two
    # it no longer read (review of PR #49).
    reference = re.compile(r"`(?:(?P<path>[A-Za-z0-9_./-]+\.py)::)?(?P<symbol>test_[A-Za-z0-9_]+)`")

    missing: list[str] = []
    for spec in sorted((REPO_ROOT / "specs").glob("*.md")):
        seen: set[tuple[str, str]] = set()
        for match in reference.finditer(spec.read_text(encoding="utf-8")):
            seen.add((match.group("path") or "", match.group("symbol")))
        for path, symbol in sorted(seen):
            if symbol not in defined:
                missing.append(f"{spec.name}: {symbol}")
                continue
            if not path:
                continue
            # Specs write these both ways: from the repository root, and from
            # services/api, which is where pytest runs. Both resolve.
            candidates = [REPO_ROOT / path, REPO_ROOT / "services" / "api" / path]
            target = next((c for c in candidates if c.is_file()), None)
            if target is None:
                missing.append(f"{spec.name}: {path} does not exist")
            elif f"def {symbol}" not in target.read_text(encoding="utf-8"):
                missing.append(f"{spec.name}: {symbol} is not in {path}")
    assert not missing, "specs name tests that do not exist: " + "; ".join(missing)
