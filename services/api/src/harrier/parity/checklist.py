"""The cutover checklist: generated from the matrix, ticked by a human.

Every keep row asks whether behavior is identical and where the proof is.
Every change row asks whether the difference is the intended one rather
than an accident. Every drop row asks for confirmation that the capability
is gone on purpose. Cutover plan phase 1 (docs/cutover-plan.md).

The generated file is round-tripped, not overwritten: regenerating after
the matrix changes preserves every tick and waiver whose item still
exists, and reports the ones that no longer do. Losing a human's review
decisions to a regeneration would make the checklist untrustworthy, which
is the only property it has.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harrier.parity.matrix import MatrixRow, parse_matrix, verdict_counts

CHECKLIST_PATH = Path("docs") / "parity-checklist.md"

PROMPTS = {
    "keep": "behavior identical, proof named",
    "change": "difference verified intentional",
    "drop": "confirmed dropped on purpose",
}

_ITEM_RE = re.compile(
    r"^- \[(?P<mark>[ xX])\] `(?P<slug>[a-z0-9-]+)` (?P<rest>.*)$",
)
_WAIVED_RE = re.compile(r"\(waived:\s*(?P<reason>[^)]*)\)\s*$")

HEADER = """# Parity checklist

Generated from `docs/parity-matrix.md` by `harrier parity checklist`, and
committed on purpose. The skeleton is derived, but the ticks and waivers are
not: they are a human's review record, reproducible from nothing, and losing
them means redoing every item. That is the same reason
`packages/contract/openapi.json` is committed rather than ignored, and like
that file this one is drift-checked, by
`test_parity.py::test_the_committed_checklist_matches_the_matrix`.

Do not edit the item text. Tick an item when you have verified it, or waive it
with a reason:

    - [x] `slug` capability ... (waived: reason)

Regenerating preserves ticks and waivers for items that still exist and lists
any that the matrix no longer carries. Waiver reasons are committed to a public
repository: keep them about the capability, never about a company or a person.
"""


@dataclass(frozen=True)
class Decision:
    slug: str
    checked: bool
    waiver: str


@dataclass
class ChecklistStatus:
    total: int
    checked: int
    waived: int
    open_items: list[str]
    orphaned: list[str]

    @property
    def complete(self) -> bool:
        # A retired item is an open question, not a closed one: someone
        # reviewed a capability the matrix no longer lists, and whether it
        # was removed on purpose is unanswered until a human says so
        # (review finding on PR #19).
        return not self.open_items and not self.orphaned


def parse_decisions(text: str) -> dict[str, Decision]:
    decisions: dict[str, Decision] = {}
    for line in text.splitlines():
        # Column zero only. Stripping first made the indented example in this
        # file's own header parse as a decision, which added a phantom
        # `slug` item that no matrix row could ever retire, so the checklist
        # could never report complete (found by the review-fix tests).
        match = _ITEM_RE.match(line.rstrip())
        if match is None:
            continue
        waived = _WAIVED_RE.search(match.group("rest"))
        decisions[match.group("slug")] = Decision(
            slug=match.group("slug"),
            checked=match.group("mark").lower() == "x",
            waiver=(waived.group("reason").strip() if waived else ""),
        )
    return decisions


def _retired_row(slug: str) -> MatrixRow:
    """A stand-in row so a retired decision renders in the item format.

    Its slug property derives from the capability, so the capability is set
    to the recorded slug to keep the round trip stable.
    """
    return MatrixRow(
        section="Retired items",
        capability=slug,
        source="",
        verdict="drop",
        qualifier="",
        rationale="",
    )


def _item_line(row: MatrixRow, decision: Decision | None) -> str:
    mark = "x" if decision is not None and decision.checked else " "
    prompt = PROMPTS[row.verdict]
    verdict = f"{row.verdict}{f' {row.qualifier}' if row.qualifier else ''}"
    line = f"- [{mark}] `{row.slug}` **{row.capability}** ({verdict}: {prompt})"
    if row.source:
        line += f" source: {row.source}"
    if decision is not None and decision.waiver:
        line += f" (waived: {decision.waiver})"
    return line


def render_checklist(rows: list[MatrixRow], existing: str = "") -> str:
    decisions = parse_decisions(existing)
    counts = verdict_counts(rows)
    lines = [HEADER, ""]
    lines.append(
        f"{len(rows)} items: {counts['keep']} keep, {counts['change']} change, "
        f"{counts['drop']} drop."
    )
    section = ""
    for row in rows:
        if row.section != section:
            section = row.section
            lines.extend(["", f"## {section}", ""])
        lines.append(_item_line(row, decisions.get(row.slug)))

    orphans = sorted(set(decisions) - {row.slug for row in rows})
    if orphans:
        lines.extend(
            [
                "",
                "## Retired items",
                "",
                "Decisions recorded against capabilities the matrix no longer lists.",
                "Confirm each was removed on purpose, then delete the line. Until then",
                "`harrier parity status` reports the checklist as incomplete.",
                "",
            ]
        )
        # Rendered in the same parseable shape as a live item, so a later
        # regeneration carries the tick and waiver forward instead of
        # discarding a review decision unread (review finding on PR #19).
        lines.extend(_item_line(_retired_row(slug), decisions[slug]) for slug in orphans)
    return "\n".join(lines) + "\n"


def write_checklist(path: Path | None = None, rows: list[MatrixRow] | None = None) -> Path:
    target = path if path is not None else CHECKLIST_PATH
    matrix_rows = rows if rows is not None else parse_matrix()
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_checklist(matrix_rows, existing), encoding="utf-8")
    return target


def checklist_status(text: str, rows: list[MatrixRow]) -> ChecklistStatus:
    decisions = parse_decisions(text)
    slugs = [row.slug for row in rows]
    checked = 0
    waived = 0
    open_items: list[str] = []
    for row in rows:
        decision = decisions.get(row.slug)
        if decision is None or not decision.checked:
            open_items.append(row.slug)
            continue
        checked += 1
        if decision.waiver:
            waived += 1
    return ChecklistStatus(
        total=len(rows),
        checked=checked,
        waived=waived,
        open_items=open_items,
        orphaned=sorted(set(decisions) - set(slugs)),
    )
