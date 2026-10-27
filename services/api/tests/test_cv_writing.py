"""How the CV's own text reads: advisory findings W1 to W8 (spec 164).

Synthetic content only: the committed examples and invented bullet lines.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from resume_support import example_bundle_raw, example_documents, store_documents

from harrier.connection import Connection
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume.content import ResumeBundle, parse_bundle
from harrier.resume.writing import ADVISORY_HEADER, writing_findings
from harrier_cli.main import main

# Every line passes every rule: an active opener, a figure where one is
# asked for, no first person, no repeated opener.
CLEAN_POOL = {
    "r1_b1": "Shipped a billing service that cut invoice errors by 40%.",
    "r1_b2": "Designed the release process for 3 product teams.",
    "r2_b1": "Migrated 12 services to a shared queue.",
    "ach_1": "Reduced page load time from 4 to 1 seconds.",
    "ach_2": "Grew weekly active users by 30%.",
}
CLEAN_SUMMARY = "Builds reliable payment systems for small teams."

LONG_LINE = (
    "Migrated 12 services to a shared queue, rewrote the consumers, documented "
    "every topic, moved the dead letter handling into one place, trained two "
    "teams on the new runbook, and retired the old broker after a quiet month "
    "of parallel running."
)


def bundle(pool: dict[str, str] | None = None, /, **framing: str) -> ResumeBundle:
    """The example bundle with a synthetic pool and framing strings."""
    base = parse_bundle(example_bundle_raw())
    fields = {"profile_summary": CLEAN_SUMMARY, "profile_lead": "", "experience_statement": ""}
    return replace(base, bullet_pool=dict(CLEAN_POOL if pool is None else pool), **fields | framing)


def advice(found: list[str]) -> list[str]:
    """Every finding but W4's information lines."""
    return [line for line in found if "bullets state a figure" not in line]


# (where the bad line goes, the bad text, the finding it prints)
BROKEN: list[tuple[str, str, str]] = [
    (
        "r1_b1",
        "Responsible for the billing service, cutting invoice errors by 40%.",
        'bullet r1_b1: opens with "responsible for"; lead with what was done',
    ),
    (
        "r2_b1",
        "Migrated our 12 services to a shared queue.",
        'bullet r2_b1: first person ("our"); use the implied first person',
    ),
    (
        "profile_summary",
        "I build reliable payment systems for small teams.",
        'profile_summary: first person ("I"); use the implied first person',
    ),
    (
        "ach_2",
        "Grew weekly active users across the product.",
        "bullet ach_2: an achievement with no figure",
    ),
    (
        "r1_b2",
        "Shipped the release process for 3 product teams.",
        'role r1: 2 bullets open with "Shipped"',
    ),
    ("ach_2", "Reduced support tickets by 30%.", 'achievements: 2 bullets open with "Reduced"'),
    (
        "r2_b1",
        "Had to migrate 12 services to a shared queue.",
        'bullet r2_b1: negative or passive framing ("Had to")',
    ),
    (
        "profile_summary",
        "Payment skills were strengthened by 5 years of production work.",
        'profile_summary: negative or passive framing ("were strengthened by")',
    ),
    (
        "profile_summary",
        "A passionate engineer building reliable payment systems.",
        'profile_summary: self-description ("passionate"); show it with a fact instead',
    ),
    ("r2_b1", LONG_LINE, "bullet r2_b1: 40 words; keep a bullet under 35"),
    (
        "r2_b1",
        "Migrated 12 services. Rewrote the consumers. Retired the old broker.",
        "bullet r2_b1: 3 sentences; keep a bullet to 2",
    ),
]


def with_line(where: str, text: str) -> ResumeBundle:
    if where in CLEAN_POOL:
        return bundle(CLEAN_POOL | {where: text})
    return bundle(**{where: text})


def test_each_writing_rule_finds_and_its_fix_clears() -> None:
    assert advice(writing_findings(bundle())) == []
    for where, text, finding in BROKEN:
        assert advice(writing_findings(with_line(where, text))) == [finding], where
        # The fix is the clean line back in its place.
        fixed = CLEAN_POOL.get(where, CLEAN_SUMMARY)
        assert advice(writing_findings(with_line(where, fixed))) == [], where


def test_each_weak_opener_and_self_description_is_found() -> None:
    for opener in ("Worked on", "helped", "Was", "In charge of", "duties included"):
        found = advice(writing_findings(with_line("r2_b1", f"{opener} 12 services.")))
        assert found == [f'bullet r2_b1: opens with "{opener.lower()}"; lead with what was done']
    # "Wasted" opens with "was" only as letters, not as the word.
    assert advice(writing_findings(with_line("r2_b1", "Wasted no time on 12 services."))) == []
    found = advice(writing_findings(with_line("profile_lead", "A hard-working team player.")))
    assert found == ['profile_lead: self-description ("hard-working"); show it with a fact instead']
    # W7 reads the framing strings only, never a pool line.
    assert advice(writing_findings(with_line("r2_b1", "Mentored 3 passionate juniors."))) == []


def test_role_figure_count() -> None:
    pool = CLEAN_POOL | {"r1_b3": "Wrote the onboarding guide for new engineers."}
    found = writing_findings(bundle(pool))
    assert "role r1: 2 of 3 bullets state a figure" in found
    assert "role r2: 1 of 1 bullets state a figure" in found
    assert "role r3: 0 of 0 bullets state a figure" in found
    # Printed after every other finding.
    broken = writing_findings(bundle(pool | {"r1_b1": "Helped 3 teams ship."}))
    assert broken[0].startswith("bullet r1_b1: opens with")
    assert all("bullets state a figure" in line for line in broken[1:])


def test_first_person_matches_whole_words() -> None:
    passes = [
        "Documented the API for myself and others, i.e. the platform team.",
        "Cut US payroll processing time by 20%.",
        "Profiled I/O paths in 4 storage services.",
        "Built a mineral survey app for 2 teams (I.e. field crews).",
    ]
    for line in passes:
        assert advice(writing_findings(with_line("r2_b1", line))) == [], line
    found_words = {
        "Led my team of 4.": "my",
        "Gave us 3 extra weeks.": "us",
        "Grew Us revenue by 5%.": "Us",
        "Shipped what we promised in 2 weeks.": "we",
        "Since 2019 I've led 3 teams.": "I",
    }
    for line, word in found_words.items():
        found = advice(writing_findings(with_line("r2_b1", line)))
        assert found == [f'bullet r2_b1: first person ("{word}"); use the implied first person']


def test_findings_name_ids_never_text() -> None:
    pool = {where: text for where, text, _ in BROKEN if where in CLEAN_POOL}
    framing = {where: text for where, text, _ in BROKEN if where not in CLEAN_POOL}
    found = writing_findings(bundle(CLEAN_POOL | pool, **framing))
    assert len(advice(found)) >= 4
    for text in [*CLEAN_POOL.values(), *pool.values(), *framing.values()]:
        assert all(text not in line for line in found), text


# --- the command ----------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    yield conn
    conn.close()


def weak_framing() -> dict[str, Any]:
    """The example framing with one pool line that opens weakly."""
    _, framing = example_documents()
    framing = copy.deepcopy(framing)
    framing["bullet_pool"]["r2_b4"] = "Responsible for typed API contracts with product teams."
    return framing


def store_truth(conn: Connection, framing: dict[str, Any], extra: str = "") -> None:
    lines = [*framing["bullet_pool"].values(), extra]
    put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(lines))


def test_profile_check_exit_ignores_writing(
    store: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["profile", "check"]) == 1
    assert ADVISORY_HEADER not in capsys.readouterr().out

    facts, _ = example_documents()
    framing = weak_framing()
    store_documents(store, facts, framing)
    store_truth(store, framing)
    assert main(["profile", "check"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "resume content for track job: valid"
    assert out[1] == ADVISORY_HEADER
    assert 'bullet r2_b4: opens with "responsible for"; lead with what was done' in out[2:]

    # A bullet the truth does not hold still fails the gate, and the advice
    # still prints after it.
    put_document(store, "resume_truth", "truth.md", "markdown", "- Nothing relevant.")
    assert main(["profile", "check"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out.index(ADVISORY_HEADER) > out.index(
        "bullet r2_b4: not supported by the truth documents"
    )


def test_a_writing_finding_does_not_refuse_a_put(
    store: Connection, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    facts, framing = example_documents()
    store_documents(store, facts, framing)
    weak = weak_framing()
    store_truth(store, weak)

    framing_file = tmp_path / "framing.json"
    framing_file.write_text(json.dumps(weak), encoding="utf-8")
    assert main(["profile", "put", "resume_framing", "--file", str(framing_file)]) == 0
    stored = store.execute(
        "SELECT content FROM profile_documents WHERE kind = 'resume_framing' AND track_id = 1"
    ).fetchone()
    assert json.loads(str(stored[0])) == weak

    # The shared put compares the truth gate's lines before and after; the
    # weak line's finding is not one of them, so a new truth is stored too.
    truth_file = tmp_path / "truth.md"
    truth_file.write_text("\n".join([*weak["bullet_pool"].values(), "- One more fact."]), "utf-8")
    assert main(["profile", "put", "resume_truth", "--file", str(truth_file)]) == 0
    capsys.readouterr()
    assert main(["profile", "check"]) == 0
    assert 'bullet r2_b4: opens with "responsible for"' in capsys.readouterr().out
