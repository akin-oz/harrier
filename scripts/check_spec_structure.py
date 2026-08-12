#!/usr/bin/env python3
"""Every spec carries its required headings (spec 041).

Spec 006 shipped with its `## Problem` section gone, taking both of its
claims with it, and nothing noticed. The spec gate reads the frontmatter and
the generated-artifact check reads the compiled output; neither reads a
spec's structure, so a spec can lose a whole section and still merge.

The check asserts the required headings are **present**, not that no others
are. A spec that adds `## Amendment` or `## What the implementation decided`
is doing the right thing, and a check rigid enough to forbid that would be
worse than the gap it closes.

Run by CI and by `just check`. Proved by
`services/api/tests/test_spec_structure.py`, including against a fixture with
a damaged heading, because a structural check that has never seen a damaged
file is a check nobody has watched fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

REQUIRED: tuple[str, ...] = (
    "## Problem",
    "## Scope",
    "## Acceptance criteria",
    "## Proof / origin",
    "## Out of scope",
)

# `specs/README.md` explains the format rather than following it, and the
# template is a template. Neither is a spec.
EXEMPT: frozenset[str] = frozenset({"README.md", "TEMPLATE.md"})


def missing_headings(text: str) -> list[str]:
    """Which required headings a spec does not carry, in order.

    Matched at the start of a line, so a heading mentioned inside a sentence
    or a code block does not count as the section existing.
    """
    lines = {line.rstrip() for line in text.splitlines()}
    return [heading for heading in REQUIRED if heading not in lines]


def check_directory(specs: Path) -> dict[str, list[str]]:
    """Every spec that is missing something, mapped to what it is missing."""
    problems: dict[str, list[str]] = {}
    for path in sorted(specs.glob("*.md")):
        if path.name in EXEMPT:
            continue
        gaps = missing_headings(path.read_text(encoding="utf-8"))
        if gaps:
            problems[path.name] = gaps
    return problems


def main(argv: list[str]) -> int:
    specs = Path(argv[0]) if argv else Path("specs")
    if not specs.is_dir():
        print(f"no specs directory at {specs}", file=sys.stderr)
        return 2
    problems = check_directory(specs)
    for name, gaps in problems.items():
        for heading in gaps:
            print(f"::error::{name} is missing '{heading}'")
    if problems:
        print()
        print("A spec that loses a section loses the findings it held.")
        return 1
    print(f"every spec in {specs} carries its required headings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
