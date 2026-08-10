"""Parity verification against the old system (spec 022).

Every test here is new: the old repo had no parity tooling, because it was
the thing being compared against.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from harrier.db import connect
from harrier.discovery import DiscoveryOptions, run_discovery
from harrier.parity import (
    ChecklistStatus,
    MatrixError,
    checklist_status,
    diff_runs,
    load_run_summary,
    parse_matrix,
    render_checklist,
    render_diff,
    stated_counts,
    verdict_counts,
)
from harrier.parity.checklist import parse_decisions, write_checklist
from harrier.parity.diff import PATH_KEYS, RunSummaryError, source_summaries

MATRIX = """# Matrix

## 1. First section

| Capability | Source | Verdict | Rationale |
|---|---|---|---|
| Alpha thing | `a.py` | keep | Behavior is load-bearing. |
| Beta thing | `b.py` | change | Shape changes on purpose. |

## 2. Sweep

| Path | Verdict | Rationale |
|---|---|---|
| `junk/` | drop | Nothing reads it. |
| `data/` | keep (data) | Migrates as private data. |

## Counts

Keep 2, change 1, drop 1.
"""


@pytest.fixture()
def matrix_file(tmp_path: Path) -> Path:
    path = tmp_path / "parity-matrix.md"
    path.write_text(MATRIX, encoding="utf-8")
    return path


# --- the matrix as data ------------------------------------------------------


def test_parses_both_table_shapes(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    assert [row.capability for row in rows] == ["Alpha thing", "Beta thing", "`junk/`", "`data/`"]
    assert verdict_counts(rows) == {"keep": 2, "change": 1, "drop": 1}
    # A qualifier stays attached without changing the verdict it qualifies.
    assert rows[3].verdict == "keep"
    assert rows[3].qualifier == "(data)"


def test_sections_are_carried_onto_their_rows(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    assert rows[0].section == "1. First section"
    assert rows[3].section == "2. Sweep"


def test_an_unreadable_verdict_fails_rather_than_dropping_the_row(tmp_path: Path) -> None:
    # A checklist quietly missing an item is worse than one that fails loudly:
    # the whole point is that nothing is dropped by accident at cutover.
    path = tmp_path / "m.md"
    path.write_text(
        MATRIX.replace("| Alpha thing | `a.py` | keep |", "| Alpha thing | `a.py` | maybe |"),
        encoding="utf-8",
    )
    with pytest.raises(MatrixError, match="verdict must be one of"):
        parse_matrix(path)


def test_a_short_row_fails_rather_than_being_guessed(tmp_path: Path) -> None:
    path = tmp_path / "m.md"
    path.write_text(MATRIX.replace("| Beta thing | `b.py` | change |", "| Beta thing |"), "utf-8")
    with pytest.raises(MatrixError, match="expected 4 cells"):
        parse_matrix(path)


def test_a_missing_matrix_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(MatrixError, match="cannot read the parity matrix"):
        parse_matrix(tmp_path / "absent.md")


def test_stated_counts_match_the_table() -> None:
    """The real matrix states its own totals in prose. They drifted: the
    document claimed 58/20/15 while its table held 60/20/16, which is the
    kind of gap that lets a capability be dropped unnoticed at cutover."""
    rows = parse_matrix()
    assert stated_counts() == verdict_counts(rows)


def test_the_real_matrix_has_unique_slugs() -> None:
    # Two rows sharing a slug would let one item's tick satisfy the other.
    slugs = [row.slug for row in parse_matrix()]
    assert len(slugs) == len(set(slugs))


# --- the checklist -----------------------------------------------------------


def test_every_row_becomes_an_item_with_its_verdict_prompt(matrix_file: Path) -> None:
    text = render_checklist(parse_matrix(matrix_file))
    assert text.count("- [ ] `") == 4
    assert "behavior identical, proof named" in text
    assert "difference verified intentional" in text
    assert "confirmed dropped on purpose" in text


def test_regenerating_preserves_ticks_and_waivers(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    first = render_checklist(rows)
    edited = first.replace("- [ ] `alpha-thing`", "- [x] `alpha-thing`")
    edited = edited.replace(
        "- [ ] `beta-thing` **Beta thing**",
        "- [x] `beta-thing` **Beta thing**",
    ).replace("Shape changes on purpose", "")
    edited = edited.replace(
        "source: `b.py`", "source: `b.py` (waived: covered by the archetype spec)"
    )
    again = render_checklist(rows, edited)
    decisions = parse_decisions(again)
    assert decisions["alpha-thing"].checked
    assert decisions["beta-thing"].waiver == "covered by the archetype spec"


def test_a_retired_item_is_reported_not_silently_dropped(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    edited = render_checklist(rows) + "- [x] `gone-capability` **Gone** (keep: x)\n"
    again = render_checklist(rows, edited)
    assert "## Retired items" in again
    assert "`gone-capability`" in again


def test_status_counts_checked_waived_and_open(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    text = render_checklist(rows).replace("- [ ] `alpha-thing`", "- [x] `alpha-thing`")
    status = checklist_status(text, rows)
    assert (status.checked, status.total, status.complete) == (1, 4, False)
    assert "beta-thing" in status.open_items


def test_status_is_complete_only_when_every_item_is_decided(matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    text = render_checklist(rows)
    for row in rows:
        text = text.replace(f"- [ ] `{row.slug}`", f"- [x] `{row.slug}`")
    status = checklist_status(text, rows)
    assert isinstance(status, ChecklistStatus)
    assert status.complete
    assert status.open_items == []


def test_write_checklist_round_trips_through_the_file(tmp_path: Path, matrix_file: Path) -> None:
    rows = parse_matrix(matrix_file)
    target = tmp_path / "nested" / "checklist.md"
    write_checklist(target, rows)
    target.write_text(
        target.read_text(encoding="utf-8").replace("- [ ] `alpha-thing`", "- [x] `alpha-thing`"),
        encoding="utf-8",
    )
    write_checklist(target, rows)
    assert parse_decisions(target.read_text(encoding="utf-8"))["alpha-thing"].checked


# --- the shadow-run diff -----------------------------------------------------


def summary(source: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": source,
        "fetched_count": 100,
        "skipped_seen": 50,
        "new_prospects": 2,
        "rejected_counts": {"title": 10},
        "skipped_rejected": 10,
        "items": [
            {"external_id": "1", "company": "Alpha", "title": "Engineer", "fit_score": "90"},
            {"external_id": "2", "company": "Beta", "title": "Engineer", "fit_score": "80"},
        ],
    }
    base.update(overrides)
    return base


def aggregate(*sources: dict[str, object]) -> dict[str, object]:
    return {"source_summaries": list(sources)}


def test_identical_runs_are_clean() -> None:
    report = diff_runs(aggregate(summary("greenhouse")), aggregate(summary("greenhouse")))
    assert report.clean
    assert "screening identical" in render_diff(report)


def test_a_score_change_on_the_same_posting_is_decidable() -> None:
    changed = summary("greenhouse")
    items = cast("list[dict[str, str]]", changed["items"])
    changed["items"] = [{**items[0], "fit_score": "70"}, *items[1:]]
    report = diff_runs(aggregate(summary("greenhouse")), aggregate(changed))
    assert report.decidable_divergences == 1
    assert "rescored: Alpha" in render_diff(report)


def test_a_posting_only_one_run_saw_is_not_counted_as_a_divergence() -> None:
    fewer = summary("greenhouse", items=[])
    report = diff_runs(aggregate(summary("greenhouse")), aggregate(fewer))
    assert report.sources[0].only_old and not report.sources[0].rescored
    rendered = render_diff(report)
    assert "Not decidable from summaries" in rendered


def test_seen_state_asymmetry_blocks_the_screening_comparison() -> None:
    """The finding that shaped this tool: against production, the old run had
    already seen almost everything and harrier's fresh store had seen nothing,
    so every count differed for a reason unrelated to screening."""
    old = summary("greenhouse", skipped_seen=95, new_prospects=0, skipped_rejected=5)
    new = summary("greenhouse", skipped_seen=0, new_prospects=8, skipped_rejected=92)
    report = diff_runs(aggregate(old), aggregate(new))
    assert report.decidable_divergences == 0
    assert not report.clean
    assert report.blocked
    rendered = render_diff(report)
    assert "screening not compared" in rendered
    assert "Migrate the discovery seen-state" in rendered


def test_matching_fetch_counts_are_reported_even_when_screening_is_blocked() -> None:
    # The importers agreeing is the one thing provable before migration.
    old = summary("greenhouse", fetched_count=7416, skipped_seen=6530)
    new = summary("greenhouse", fetched_count=7412, skipped_seen=0)
    report = diff_runs(aggregate(old), aggregate(new))
    assert report.sources[0].inputs_agree
    assert "inputs agree: fetched 7416 then 7412" in render_diff(report)


def test_a_real_input_difference_is_not_called_agreement() -> None:
    old = summary("greenhouse", fetched_count=7416)
    new = summary("greenhouse", fetched_count=12, skipped_seen=50)
    report = diff_runs(aggregate(old), aggregate(new))
    assert not report.sources[0].inputs_agree
    assert "inputs DIFFER" in render_diff(report)


def test_a_source_missing_from_the_new_run_is_not_clean() -> None:
    report = diff_runs(
        aggregate(summary("greenhouse"), summary("apify_linkedin")),
        aggregate(summary("greenhouse")),
    )
    assert not report.clean
    assert "apify_linkedin" in render_diff(report)


def test_a_per_source_file_is_accepted_as_well_as_an_aggregate() -> None:
    assert set(source_summaries(summary("lever"))) == {"lever"}


def test_report_carries_no_filesystem_paths() -> None:
    """Old-system summaries carry absolute paths in a home directory, and a
    parity report is a document a human pastes into an issue (ADR-008)."""
    old = summary(
        "greenhouse",
        tracker_path="/Users/someone/job-hunt-local/tracker/jobs.csv",
        state_path="/Users/someone/job-hunt-local/state",
        board_urls=["https://boards.greenhouse.io/private-watchlist"],
    )
    rendered = render_diff(diff_runs(aggregate(old), aggregate(summary("greenhouse"))))
    assert "/Users/" not in rendered
    for key in PATH_KEYS:
        assert key not in rendered


def test_an_unreadable_summary_names_the_path(tmp_path: Path) -> None:
    bad = tmp_path / "run.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(RunSummaryError, match="not a run summary object"):
        load_run_summary(bad)
    with pytest.raises(RunSummaryError, match="cannot read a run summary"):
        load_run_summary(tmp_path / "absent.json")


def test_load_run_summary_reads_a_real_shape(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(aggregate(summary("lever"))), encoding="utf-8")
    assert set(source_summaries(load_run_summary(path))) == {"lever"}


# --- shadow mode -------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return connect()


def test_shadow_implies_dry_run() -> None:
    assert DiscoveryOptions(shadow=True).dry_run
    # and does not leak the other way: --dry-run keeps its existing meaning.
    assert not DiscoveryOptions(dry_run=True).shadow


def test_a_shadow_run_never_reaches_the_paid_source(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dual-run period runs discovery on a schedule for a week. --dry-run
    alone still starts a billed Apify run and discards the result, so shadow
    mode exists to make that week free."""

    def explode(*args: object, **kwargs: object) -> object:
        raise AssertionError("a shadow run must not start a paid Apify run")

    monkeypatch.setattr("harrier.discovery.fetch_apify_linkedin_jobs", explode)
    monkeypatch.setenv("APIFY_TOKEN", "present-but-must-not-be-used")
    summary_out = run_discovery(db, DiscoveryOptions(shadow=True, notify=False))
    assert "apify_linkedin" not in cast("list[str]", summary_out["sources_run"])


def test_a_shadow_run_writes_nothing_to_the_tracker(db: sqlite3.Connection) -> None:
    run_discovery(db, DiscoveryOptions(shadow=True, notify=False))
    assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
