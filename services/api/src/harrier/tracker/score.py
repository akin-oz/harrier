"""One score, written one way (spec 033).

The tracker carried two score fields and they drifted. `fit_score` is a real
column and is what the digest, the notifications, the resume tailoring, the
letters, the answers and the API all read. `score` is a promoted note key and
is what the queue read first. Import wrote only `fit_score`; `reevaluate`
wrote only `score`. So after one rescore the command line and the nightly
digest ranked the same tracker by two different numbers, and neither was
labelled as the stale one.

The fix is not to pick a winner and leave the other writable. It is to make
writing a score a single operation that fills every field a reader might use,
which is what `score_fields()` is.
`tests/test_scoring.py::test_every_score_field_is_written_together` enumerates
the readers found in the tree and fails if a field gains a reader this
function does not fill.

The score also now carries the version of the policy that produced it. A bare
integer told you nothing about whether it was comparable with the row above
it, while the weights are read live at call time and ranking sorts across
months of stored values. Rows written before this change read as `unknown`
rather than being recomputed: recomputing history under today's rules would
destroy the record of what was actually decided at the time.
"""

from __future__ import annotations

# Every field in the tracker that holds a score or describes one. Writing a
# score means writing all of them, and a reader may use any.
SCORE_FIELDS: tuple[str, ...] = ("fit_score", "score", "signals", "scoring_version")

# What a row scored before versions were stored. Not a version anyone can
# reproduce, which is the point: it says "do not compare this" rather than
# implying a policy that may never have applied.
UNKNOWN_VERSION = "unknown"


def score_fields(score: int, reasons: list[str], version: str) -> dict[str, str]:
    """The complete set of tracker fields that a score writes.

    Callers pass this to `update_fields` or merge it into a new row. Nothing
    should build these keys by hand: the reason the two columns diverged is
    that two call sites each wrote the subset they happened to care about.
    """
    return {
        "fit_score": str(score),
        "score": str(score),
        "signals": "|".join(reasons),
        "scoring_version": version or UNKNOWN_VERSION,
    }


def stored_version(job: dict[str, str]) -> str:
    """The policy version a stored row was scored under.

    Normalizing on read, not only on write. `score_fields` substitutes
    `unknown` for a blank version, but a row written before this change was
    never passed through it: migration 3 defaults the column to the empty
    string, so those rows read as blank and the promise that history says
    `unknown` was kept only for rows that never needed it (review finding on
    PR #42).

    Proved by `tests/test_scoring.py::test_a_row_written_before_versions_reads_as_unknown`.
    """
    return (job.get("scoring_version") or "").strip() or UNKNOWN_VERSION


def stored_score(job: dict[str, str]) -> int:
    """The score of a stored row, from the field that is authoritative.

    `fit_score` and not `score or fit_score`: the fallback was how the queue
    silently preferred whichever field had been written most recently, which
    is exactly the disagreement this module exists to end.
    """
    raw = (job.get("fit_score") or "").strip()
    try:
        return int(float(raw))
    except ValueError:
        return 0
