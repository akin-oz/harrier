"""A spec that loses a section fails rather than merging (spec 041).

Spec 006 shipped with its `## Problem` gone, and both of its claims went with
it. The spec gate reads frontmatter and the artifact check reads compiled
output; neither reads structure, so the damage merged unnoticed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CHECK = Path(__file__).resolve().parents[3] / "scripts" / "check_spec_structure.py"
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


def test_every_required_heading_is_checked(tmp_path: Path) -> None:
    """Each one individually, so a heading cannot be quietly dropped from the
    required set without a test noticing."""
    from importlib.util import module_from_spec, spec_from_file_location

    loader = spec_from_file_location("check_spec_structure", CHECK)
    assert loader is not None and loader.loader is not None
    module = module_from_spec(loader)
    loader.loader.exec_module(module)

    for heading in module.REQUIRED:
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
