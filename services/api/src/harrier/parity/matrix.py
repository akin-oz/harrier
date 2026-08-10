"""Reading docs/parity-matrix.md as data (spec 022).

The matrix is the inventory of every capability in the old system, marked
keep, change, or drop. It is prose for a human, and it is also the source
of the cutover checklist, so it is parsed rather than retyped: a row that
exists only in one of the two would be exactly the kind of silent gap the
cutover plan exists to prevent.

Two table shapes appear in the document. Sections 1 through 8 use
capability/source/verdict/rationale; section 9 sweeps remaining paths with
path/verdict/rationale. Both normalize to MatrixRow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harrier.demo import anchored_path

MATRIX_PATH = Path("docs") / "parity-matrix.md"

VERDICTS = ("keep", "change", "drop")

_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_COUNTS_RE = re.compile(r"^Keep\s+(\d+),\s*change\s+(\d+),\s*drop\s+(\d+)", re.IGNORECASE)
# A verdict cell may carry a qualifier, as in "keep (data)".
_VERDICT_RE = re.compile(r"^(?P<verdict>keep|change|drop)\b(?P<qualifier>.*)$", re.IGNORECASE)


class MatrixError(ValueError):
    """The matrix could not be read as the checklist's source of truth."""


@dataclass(frozen=True)
class MatrixRow:
    section: str
    capability: str
    source: str
    verdict: str
    qualifier: str
    rationale: str

    @property
    def slug(self) -> str:
        """A stable id for checklist round-tripping. Derived from the
        capability text, so editing a capability retires its old item
        rather than silently rebinding a tick to different wording."""
        lowered = re.sub(r"[^a-z0-9]+", "-", self.capability.lower()).strip("-")
        return lowered[:72] or "unnamed"


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return []
    # Trailing and leading pipes produce empty edge cells.
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(set(cell) <= {"-", ":", " "} and cell for cell in cells)


def parse_matrix(path: Path | None = None) -> list[MatrixRow]:
    """Every capability row in document order.

    Raises MatrixError rather than skipping a malformed row: a checklist
    quietly missing an item is worse than one that fails to generate.
    """
    target = anchored_path(path if path is not None else MATRIX_PATH)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise MatrixError(f"cannot read the parity matrix at {target}: {exc}") from exc

    rows: list[MatrixRow] = []
    section = ""
    header: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        heading = _SECTION_RE.match(line)
        if heading:
            section = heading.group("title")
            header = []
            continue
        cells = _split_row(line)
        if not cells:
            continue
        if _is_separator(cells):
            continue
        lowered = [cell.lower() for cell in cells]
        if "verdict" in lowered:
            header = lowered
            continue
        if not header:
            continue
        rows.append(_row_from_cells(cells, header, section, number))
    if not rows:
        raise MatrixError(f"{target} contained no capability rows")
    return rows


def _row_from_cells(cells: list[str], header: list[str], section: str, number: int) -> MatrixRow:
    if len(cells) != len(header):
        raise MatrixError(
            f"parity matrix line {number}: expected {len(header)} cells, got {len(cells)}"
        )
    values = dict(zip(header, cells, strict=True))
    raw_verdict = values.get("verdict", "")
    match = _VERDICT_RE.match(raw_verdict)
    if match is None:
        raise MatrixError(
            f"parity matrix line {number}: verdict must be one of {VERDICTS}, got {raw_verdict!r}"
        )
    # Section 9 keys its rows by path rather than capability.
    capability = values.get("capability") or values.get("path") or ""
    if not capability:
        raise MatrixError(f"parity matrix line {number}: row has no capability or path")
    return MatrixRow(
        section=section,
        capability=capability,
        source=values.get("source", ""),
        verdict=match.group("verdict").lower(),
        qualifier=match.group("qualifier").strip(),
        rationale=values.get("rationale", ""),
    )


def verdict_counts(rows: list[MatrixRow]) -> dict[str, int]:
    counts: dict[str, int] = {verdict: 0 for verdict in VERDICTS}
    for row in rows:
        counts[row.verdict] += 1
    return counts


def stated_counts(path: Path | None = None) -> dict[str, int] | None:
    """The Keep/change/drop totals the document states about itself, or None
    when it states none. Parsed so a test can catch the two drifting apart."""
    target = anchored_path(path if path is not None else MATRIX_PATH)
    for line in target.read_text(encoding="utf-8").splitlines():
        match = _COUNTS_RE.match(line.strip())
        if match:
            return {
                "keep": int(match.group(1)),
                "change": int(match.group(2)),
                "drop": int(match.group(3)),
            }
    return None
