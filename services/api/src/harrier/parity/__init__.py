"""Parity verification between the old system and harrier (spec 022).

Two independent halves, both read-only with respect to the old repo:
the checklist generated from docs/parity-matrix.md, and the shadow-run
diff over run summaries. The cutover event itself is spec 024.
"""

from harrier.parity.checklist import (
    CHECKLIST_PATH,
    ChecklistStatus,
    checklist_status,
    render_checklist,
    write_checklist,
)
from harrier.parity.diff import (
    DiffReport,
    RunSummaryError,
    diff_runs,
    load_run_summary,
    render_diff,
)
from harrier.parity.matrix import (
    MATRIX_PATH,
    MatrixError,
    MatrixRow,
    parse_matrix,
    stated_counts,
    verdict_counts,
)

__all__ = [
    "CHECKLIST_PATH",
    "MATRIX_PATH",
    "ChecklistStatus",
    "DiffReport",
    "MatrixError",
    "MatrixRow",
    "RunSummaryError",
    "checklist_status",
    "diff_runs",
    "load_run_summary",
    "parse_matrix",
    "render_checklist",
    "render_diff",
    "stated_counts",
    "verdict_counts",
    "write_checklist",
]
