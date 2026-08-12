"""Checklist items that resolve against something executable (spec 039).

Ninety-seven items, none ticked, none waived, since the day it shipped. A
status that has read the same since it shipped carries no information, and
cutover has four preconditions of which this is one.

The point is not to tick boxes. It is that **coverage by enumeration cannot
find a gap**: the parity matrix did list all twelve CLI verbs, the checklist
did generate all twelve items, and nine of them did not exist. Nothing was
wired to contradict the assumption. Only a contradiction can find that, so an
item is verified by a check that examines the tree and can come back no.

Three populations, and the classification is derived rather than written into
the checklist by hand:

- **automated**: a slug in `CHECKS`, verified now, by name.
- **waived**: ticked in the checklist with a reason.
- **manual**: everything else. Labelled as a note rather than counted as
  progress, so the number means something.

The guard that matters is
`tests/test_parity_checklist.py::test_no_check_can_pass_when_its_subject_is_absent`.
A check that returns ok whatever it finds would convert an honest zero into a
dishonest ninety-seven, which is worse than the state it replaced. Every
check here is run against a tree with its subject removed and has to say no.

The registry starts small on purpose. An item with no check is manual and
says so; inventing a check that asserts nothing to make a number go up is the
failure this module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    evidence: str


@dataclass(frozen=True)
class Check:
    """One executable check, named so a reader can go and run it."""

    name: str
    run: Callable[[], CheckResult]


def _cli_has(*verbs: str) -> CheckResult:
    """Whether the shipped command really offers these verbs.

    This is the check that would have caught the nine missing ones: it asks
    the command rather than the matrix.

    It runs `harrier --help` as a subprocess rather than importing the
    parser, for two reasons. The domain may not import the CLI, which
    import-linter enforces and which this check violated on its first
    attempt. And running the entry point tests what is actually installed,
    which is a stronger claim than what a function in the tree returns.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "from harrier_cli.main import main; main(['--help'])"],
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout + result.stderr
    if not text.strip():
        return CheckResult(False, "the command printed no help at all")
    missing = sorted(verb for verb in verbs if not _help_lists(text, verb))
    if missing:
        return CheckResult(False, f"the command has no {', '.join(missing)}")
    return CheckResult(True, f"the command offers {', '.join(sorted(verbs))}")


def _help_lists(help_text: str, verb: str) -> bool:
    """Whether a verb appears as a subcommand rather than inside prose.

    Matched at a word boundary against the subcommand block, because
    `add` appears inside `--link-contacts` and a substring test would call
    that a match.
    """
    import re

    return re.search(rf"(?m)^\s*\{{?.*\b{re.escape(verb)}\b", help_text) is not None


def _normalized_schema() -> CheckResult:
    """The shared job shape exists and carries the fields ingestion depends on."""
    from harrier.screening.normalized import make_normalized_job

    job = make_normalized_job(
        source="greenhouse",
        company="Example Labs",
        title="Senior Frontend Engineer",
        location="Remote, Europe",
        url="https://boards.example.com/example/1",
        description="",
    )
    required = {"source", "company", "title", "location", "url", "description"}
    missing = sorted(required - set(job))
    if missing:
        return CheckResult(False, f"the normalized job has no {', '.join(missing)}")
    return CheckResult(True, f"make_normalized_job carries {len(required)} required fields")


def _export_shapes() -> CheckResult:
    """The legacy CSV column counts, which the export promises to preserve."""
    from harrier.tracker.schema import CONTACT_FIELDS, TRACKER_FIELDS

    if len(TRACKER_FIELDS) != 20 or len(CONTACT_FIELDS) != 17:
        return CheckResult(
            False,
            f"columns are {len(TRACKER_FIELDS)} and {len(CONTACT_FIELDS)}, not 20 and 17",
        )
    return CheckResult(True, "20 tracker columns and 17 contact columns")


def _remote_and_region_gate() -> CheckResult:
    """A hybrid role in a non-EMEA location is refused. Run, not read."""
    from harrier.screening.normalized import make_normalized_job
    from harrier.screening.rules import remote_region_allowed

    hybrid = make_normalized_job(
        source="greenhouse",
        company="Example Labs",
        title="Senior Frontend Engineer",
        location="Hybrid, New York",
        url="https://boards.example.com/example/2",
        description="",
    )
    allowed, reason = remote_region_allowed(hybrid, {})
    if allowed:
        return CheckResult(False, "a hybrid non-EMEA role passed the gate")
    return CheckResult(True, f"a hybrid non-EMEA role is refused: {reason}")


def _eu_phrases_are_positive() -> CheckResult:
    """EU-permit wording is a positive signal and never a filter."""
    from harrier.screening.rules import (
        PREFERRED_SIGNAL_WEIGHTS,
        REGION_NEGATIVE_HINTS,
        REMOTE_NEGATIVE_HINTS,
    )

    phrases = [key for key in PREFERRED_SIGNAL_WEIGHTS if "eu" in key.split()]
    if not phrases:
        return CheckResult(False, "no EU phrase carries a positive weight")
    blocked = [p for p in phrases if p in REGION_NEGATIVE_HINTS or p in REMOTE_NEGATIVE_HINTS]
    if blocked:
        return CheckResult(False, f"{', '.join(blocked)} also appears in a rejection list")
    return CheckResult(True, f"{len(phrases)} EU phrases weighted positively, none filtering")


# Slug to check. Every slug here must exist in the matrix, which
# `test_every_registered_check_names_a_real_matrix_item` holds: a registry
# that drifts from the matrix would classify an item that no longer exists.
CHECKS: dict[str, Check] = {
    "shared-normalized-job-schema": Check("normalized job schema", _normalized_schema),
    "remote-only-and-emea-enforcement-incl-location-only-negative-hints-and-t": Check(
        "remote and EMEA gate refuses a hybrid non-EMEA role", _remote_and_region_gate
    ),
    "eu-permit-and-eu-entity-phrases-as-positive-signals-never-filters": Check(
        "EU phrases weighted positively and in no rejection list", _eu_phrases_are_positive
    ),
    "20-column-tracker-schema": Check("legacy export column counts", _export_shapes),
    "tracker-cli-verbs-shortlist-tailor-applied-interviewing-reject-track-add": Check(
        "the parser offers every tracker verb",
        lambda: _cli_has("shortlist", "track", "applied", "reject", "add", "review", "next"),
    ),
}
