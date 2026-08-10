"""Shadow-run diff: harrier's screening decisions against the old system's.

Cutover plan phase 2. Both systems write the same run-summary shape (harrier
ported it), so a run can be compared per source on the counts that encode a
screening decision, and item by item on what was accepted.

What this can and cannot prove. The two systems fetch at different moments,
so their input sets differ: a posting present in one run and absent from the
other is usually the board changing between fetches, not a screening
divergence. The report therefore separates divergences that are decidable
from that noise floor:

- Both systems saw a posting and disagreed about accepting it, or scored it
  differently. Decidable, and the thing parity is about.
- One system never saw the posting. Not decidable from summaries alone, and
  reported separately so it cannot be mistaken for the first kind.

The stronger proof, replaying one recorded input through both screeners, is
out of scope here: it would mean importing the old repo's modules, and the
old repo is read-only by rule (docs/cutover-plan.md phase 0).

Nothing here writes to the old system. Paths from its summaries are dropped
rather than carried into the report, because they are absolute paths in a
home directory (ADR-008, test_parity.py::test_report_carries_no_filesystem_paths).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

# Keys in a run summary that name a location on the machine. Dropped from
# every report: a parity report is a document a human pastes into an issue.
PATH_KEYS = frozenset({"tracker_path", "state_path", "dataset_files", "board_urls"})

# Boards change between two fetches minutes apart, so identical input
# counts are not achievable; these are the bands within which two runs
# count as having seen the same thing.
FETCH_TOLERANCE = 0.02
SEEN_TOLERANCE = 0.05


class RunSummaryError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    key: str
    company: str
    title: str
    score: str


@dataclass
class SourceDiff:
    source: str
    fetched: tuple[int, int] = (0, 0)
    seen_suppressed: tuple[int, int] = (0, 0)
    incomparable: str = ""
    counts: dict[str, tuple[object, object]] = field(
        default_factory=dict[str, tuple[object, object]]
    )
    disagreed: list[tuple[Item, Item]] = field(default_factory=list[tuple[Item, Item]])
    rescored: list[tuple[Item, Item]] = field(default_factory=list[tuple[Item, Item]])
    only_old: list[Item] = field(default_factory=list[Item])
    only_new: list[Item] = field(default_factory=list[Item])

    @property
    def decidable_divergences(self) -> int:
        if self.incomparable:
            # Counting divergences from runs with different effective inputs
            # would report noise as findings. Nothing here is decidable.
            return 0
        return len(self.disagreed) + len(self.rescored) + len(self.counts)

    @property
    def fetch_delta(self) -> int:
        return abs(self.fetched[0] - self.fetched[1])

    @property
    def inputs_agree(self) -> bool:
        """Both runs pulled essentially the same postings. Independent of the
        screening comparison, and available even when that one is blocked:
        it is what says the importers themselves are at parity."""
        largest = max(self.fetched)
        if largest == 0:
            return self.fetched == (0, 0)
        return self.fetch_delta / largest <= FETCH_TOLERANCE


@dataclass
class DiffReport:
    sources: list[SourceDiff]
    only_old_sources: list[str]
    only_new_sources: list[str]

    @property
    def decidable_divergences(self) -> int:
        return sum(source.decidable_divergences for source in self.sources)

    @property
    def blocked(self) -> list[SourceDiff]:
        return [source for source in self.sources if source.incomparable]

    @property
    def clean(self) -> bool:
        """A clean diff means the screening comparison ran and found nothing.
        A blocked source is not clean: it was never compared."""
        return self.decidable_divergences == 0 and not self.only_old_sources and not self.blocked


def load_run_summary(path: Path) -> dict[str, object]:
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunSummaryError(f"cannot read a run summary at {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RunSummaryError(f"{path} is not a run summary object")
    return cast("dict[str, object]", parsed)


def source_summaries(summary: dict[str, object]) -> dict[str, dict[str, object]]:
    """Per-source sections of an aggregate summary, or the summary itself when
    handed a single per-source file (`incoming/<source>_latest.json`)."""
    raw = summary.get("source_summaries")
    if raw is None and isinstance(summary.get("source"), str):
        return {str(summary["source"]): summary}
    if not isinstance(raw, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for entry in cast("list[object]", raw):
        if isinstance(entry, dict):
            typed = cast("dict[str, object]", entry)
            name = typed.get("source")
            if isinstance(name, str):
                result[name] = typed
    return result


def _items(summary: dict[str, object]) -> dict[str, Item]:
    raw = summary.get("items")
    if not isinstance(raw, list):
        return {}
    items: dict[str, Item] = {}
    for entry in cast("list[object]", raw):
        if not isinstance(entry, dict):
            continue
        typed = cast("dict[str, object]", entry)
        # external_id is the stable identity where a source provides one; the
        # posting URL is the fallback, and matches how the tracker dedupes.
        key = str(typed.get("external_id") or "").strip() or str(typed.get("url") or "").strip()
        if not key:
            continue
        items[key] = Item(
            key=key,
            company=str(typed.get("company") or ""),
            title=str(typed.get("title") or ""),
            score=str(typed.get("fit_score") or ""),
        )
    return items


COMPARED_COUNTS = ("new_prospects", "rejected_counts", "skipped_hold", "skipped_rejected")


def _int(summary: dict[str, object], key: str) -> int:
    value = summary.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _seen_asymmetry(old: dict[str, object], new: dict[str, object]) -> str:
    """Why these two runs cannot be compared on screening, or "".

    Discovered by running this against production: the old system had seen
    almost every posting before, so it screened 11 of 7,416 greenhouse
    results, while a fresh harrier store screened all 6,531 of them. Every
    downstream count then differs for a reason that has nothing to do with
    screening. Comparing them is only meaningful once harrier's seen-state
    has been migrated (cutover plan phase 3 step 3), so the diff refuses
    rather than presenting the artifact as findings.
    """
    fetched = max(_int(old, "fetched_count"), _int(new, "fetched_count"))
    if fetched == 0:
        return ""
    old_seen = _int(old, "skipped_seen")
    new_seen = _int(new, "skipped_seen")
    if abs(old_seen - new_seen) / fetched <= SEEN_TOLERANCE:
        return ""
    return (
        f"seen-state differs: the old run suppressed {old_seen} of {fetched} postings as "
        f"already seen and this one suppressed {new_seen}. The runs screened different "
        "inputs, so their counts are not comparable. Migrate the discovery seen-state "
        "into harrier before reading this section."
    )


def diff_sources(old: dict[str, object], new: dict[str, object], source: str) -> SourceDiff:
    diff = SourceDiff(
        source=source,
        fetched=(_int(old, "fetched_count"), _int(new, "fetched_count")),
        seen_suppressed=(_int(old, "skipped_seen"), _int(new, "skipped_seen")),
        incomparable=_seen_asymmetry(old, new),
    )
    for key in COMPARED_COUNTS:
        if key in PATH_KEYS:
            continue
        before = old.get(key)
        after = new.get(key)
        # An absent key on one side is not a divergence: the old system did
        # not always write every counter.
        if before is None or after is None:
            continue
        if before != after:
            diff.counts[key] = (before, after)

    old_items = _items(old)
    new_items = _items(new)
    for key, item in old_items.items():
        other = new_items.get(key)
        if other is None:
            diff.only_old.append(item)
        elif item.score != other.score:
            diff.rescored.append((item, other))
    for key, item in new_items.items():
        if key not in old_items:
            diff.only_new.append(item)
    return diff


def diff_runs(old: dict[str, object], new: dict[str, object]) -> DiffReport:
    old_sources = source_summaries(old)
    new_sources = source_summaries(new)
    shared = sorted(set(old_sources) & set(new_sources))
    return DiffReport(
        sources=[diff_sources(old_sources[name], new_sources[name], name) for name in shared],
        only_old_sources=sorted(set(old_sources) - set(new_sources)),
        only_new_sources=sorted(set(new_sources) - set(old_sources)),
    )


def _describe(item: Item) -> str:
    return f"{item.company} - {item.title} (score {item.score})"


def render_diff(report: DiffReport) -> str:
    lines = ["# Shadow-run diff", ""]
    if report.clean:
        lines.append("No decidable divergences. Every shared source agreed on counts and scores.")
    elif report.decidable_divergences:
        lines.append(
            f"{report.decidable_divergences} decidable divergence(s). "
            "Each must be a bug in harrier or a matrix change row."
        )
    if report.blocked:
        lines.append(
            f"{len(report.blocked)} source(s) could not be compared on screening; "
            "see the reason under each."
        )
    if report.only_old_sources:
        lines.append(
            f"Sources the old run had and this one did not: {', '.join(report.only_old_sources)}"
        )
    if report.only_new_sources:
        lines.append(f"Sources only this run had: {', '.join(report.only_new_sources)}")

    for source in report.sources:
        lines.extend(["", f"## {source.source}", ""])
        # Reported first and always: it is the one comparison that survives a
        # seen-state mismatch, and it is what says the importers agree.
        verdict = "agree" if source.inputs_agree else "DIFFER"
        lines.append(
            f"- inputs {verdict}: fetched {source.fetched[0]} then {source.fetched[1]} "
            f"(delta {source.fetch_delta})"
        )
        if source.incomparable:
            lines.append(f"- screening not compared: {source.incomparable}")
            continue
        if source.decidable_divergences == 0 and not source.only_old and not source.only_new:
            lines.append("- screening identical")
            continue
        for key, (before, after) in sorted(source.counts.items()):
            lines.append(f"- {key}: old {before!r}, new {after!r}")
        for before_item, after_item in source.rescored:
            lines.append(
                f"- rescored: {before_item.company} - {before_item.title}: "
                f"{before_item.score} then {after_item.score}"
            )
        if source.only_old or source.only_new:
            lines.extend(
                [
                    "",
                    "Not decidable from summaries (the runs fetched at different times, "
                    "so these may be board changes rather than screening differences):",
                ]
            )
            lines.extend(f"- only in the old run: {_describe(item)}" for item in source.only_old)
            lines.extend(f"- only in this run: {_describe(item)}" for item in source.only_new)
    return "\n".join(lines) + "\n"
