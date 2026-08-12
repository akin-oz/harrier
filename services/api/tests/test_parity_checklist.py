"""The checklist can distinguish progress from stasis (spec 039).

Ninety-seven items, none ticked, none waived, since the day it shipped. A
number that has not moved since it shipped carries no information, and
cutover has four preconditions of which this is one.

The failure this must not introduce is worse than the state it replaces:
items auto-ticked by a check that asserts nothing, converting an honest zero
into a dishonest ninety-seven. `test_no_check_can_pass_when_its_subject_is_absent`
is the test that stops that, and it is the reason this file exists.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from harrier.parity.checklist import checklist_status, parse_decisions, waiver_problems
from harrier.parity.checks import CHECKS, Check, CheckResult
from harrier.parity.matrix import MatrixRow, parse_matrix


@pytest.fixture
def rows() -> list[MatrixRow]:
    return parse_matrix()


# --- the classification is derived --------------------------------------------


def test_every_item_is_classified_exactly_once(rows: list[MatrixRow]) -> None:
    """Three populations that partition the matrix. An item in none of them
    is an item the count silently omits; an item in two is counted twice."""
    status = checklist_status("", rows)
    classified = (
        set(status.verified)
        | {slug for slug, _ in status.failing}
        | set(status.waived_items)
        | set(status.manual)
    )
    assert classified == {row.slug for row in rows}
    assert len(status.verified) + len(status.failing) + len(status.waived_items) + len(
        status.manual
    ) == len(rows)


def test_every_registered_check_names_a_real_matrix_item(rows: list[MatrixRow]) -> None:
    """A registry that drifts from the matrix classifies items that do not
    exist, and the drift is invisible because both sides look plausible."""
    slugs = {row.slug for row in rows}
    unknown = sorted(set(CHECKS) - slugs)
    assert not unknown, f"checks registered against slugs no matrix row carries: {unknown}"


def test_an_automated_item_names_the_check_it_runs() -> None:
    """ "Verified" with no name is a claim the reader cannot go and confirm."""
    for slug, check in CHECKS.items():
        assert check.name.strip(), f"{slug} has a check with no name"


def test_the_classification_is_not_read_from_the_checklist(rows: list[MatrixRow]) -> None:
    """Ticking an item by hand does not make it verified.

    The classification comes from the registry and the waivers, so a hand
    edit cannot promote a manual item into the automated population, which is
    the whole point of deriving it.
    """
    manual = checklist_status("", rows).manual[0]
    ticked = f"- [x] `{manual}` capability\n"
    status = checklist_status(ticked, rows)
    assert manual not in status.verified
    assert manual in status.manual


# --- the guard that matters ---------------------------------------------------


def _a_job_missing_its_fields(**_: object) -> dict[str, str]:
    return {"company": "x"}


def _a_gate_that_allows_everything(*_: object) -> tuple[bool, str]:
    return True, "gate removed"


def _a_command_that_prints_nothing(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


def test_no_check_can_pass_when_its_subject_is_absent() -> None:
    """Every check has to be able to say no.

    A check that returns ok whatever it finds would convert an honest zero
    into a dishonest ninety-seven, which is worse than the state it replaced.
    Each is run against a tree with its subject removed, and has to fail.

    Removing the subject is done by patching what the check reads, which is
    the closest thing to a mutation test that can live in the suite: if a
    check ignores its input, this passes it broken and the assertion catches
    it.
    """
    absent: dict[str, tuple[str, object]] = {
        "shared-normalized-job-schema": (
            "harrier.screening.normalized.make_normalized_job",
            _a_job_missing_its_fields,
        ),
        "remote-only-and-emea-enforcement-incl-location-only-negative-hints-and-t": (
            "harrier.screening.rules.remote_region_allowed",
            _a_gate_that_allows_everything,
        ),
        "eu-permit-and-eu-entity-phrases-as-positive-signals-never-filters": (
            "harrier.screening.rules.PREFERRED_SIGNAL_WEIGHTS",
            {},
        ),
        "20-column-tracker-schema": ("harrier.tracker.schema.TRACKER_FIELDS", ("only", "two")),
        # This one runs the command as a subprocess, so its subject is
        # removed by making the subprocess produce nothing.
        "tracker-cli-verbs-shortlist-tailor-applied-interviewing-reject-track-add": (
            "subprocess.run",
            _a_command_that_prints_nothing,
        ),
    }
    assert set(absent) == set(CHECKS), "a check was added with no absence case"

    for slug, (target, replacement) in absent.items():
        with patch(target, replacement):
            result = CHECKS[slug].run()
        assert not result.ok, (
            f"{slug} passed with its subject removed: it asserts nothing, "
            "and would tick an item it has not verified"
        )
        assert result.evidence.strip(), f"{slug} failed without saying what it found"


def test_a_check_that_fails_is_reported_and_not_counted_as_verified(rows: list[MatrixRow]) -> None:
    slug = next(iter(CHECKS))
    failing: dict[str, Check] = {
        **CHECKS,
        slug: Check(CHECKS[slug].name, lambda: CheckResult(False, "the subject is gone")),
    }
    with patch("harrier.parity.checks.CHECKS", failing):
        status = checklist_status("", rows)
    assert slug not in status.verified
    assert any(name == slug for name, _ in status.failing)


# --- waivers ------------------------------------------------------------------


def test_a_waiver_without_a_reason_is_refused(rows: list[MatrixRow]) -> None:
    """A tick with no reason looks like a decision and records none."""
    slug = rows[0].slug
    assert waiver_problems(f"- [x] `{slug}` capability\n") == [slug]
    assert waiver_problems(f"- [x] `{slug}` capability (waived: not in scope)\n") == []


def test_an_unticked_item_is_not_a_missing_waiver(rows: list[MatrixRow]) -> None:
    """Only a tick claims a decision was made."""
    assert waiver_problems(f"- [ ] `{rows[0].slug}` capability\n") == []


def test_a_waived_item_is_its_own_population(rows: list[MatrixRow]) -> None:
    manual = checklist_status("", rows).manual[0]
    status = checklist_status(f"- [x] `{manual}` capability (waived: superseded)\n", rows)
    assert manual in status.waived_items
    assert manual not in status.manual


# --- the committed checklist --------------------------------------------------


def test_the_committed_checklist_records_no_unreasoned_tick() -> None:
    path = Path(__file__).resolve().parents[3] / "docs" / "parity-checklist.md"
    assert waiver_problems(path.read_text(encoding="utf-8")) == []


def test_the_committed_checklist_parses(rows: list[MatrixRow]) -> None:
    path = Path(__file__).resolve().parents[3] / "docs" / "parity-checklist.md"
    decisions = parse_decisions(path.read_text(encoding="utf-8"))
    assert set(decisions) <= {row.slug for row in rows}
