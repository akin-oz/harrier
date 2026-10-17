"""The letter's shape, evidence and draft sections (spec 158).

Every test goes through `generate_cover_letter`, `cover_letter_violations`
or `write_cover_letter_artifacts`, the decisions, with the provider stubbed.
The fixtures are test_apply_claims.py's. Everything is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_apply_claims import (
    CHECKOUT,
    COMPANY,
    GROUNDED_CLAIMS,
    INVOICES,
    POSTING,
    ROLE,
    candidate,
    generate,
    letter_json,
    refusal,
    seed,
    stub_letter,
)

import harrier.apply.letters as letters_module
from harrier.apply import generate_cover_letter
from harrier.connection import Connection
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.tracks import Scope, add_track, resolve_scope

REFUND = "I cut the refund queue to 40 tickets."
REFUND_RESULT = "Cut the refund queue from 300 tickets to 40 tickets"
REFUND_OPENER = "I once rebuilt the refund queue for the whole company"

STORY_BANK = [
    {
        "story_id": "refunds",
        "theme": "Support load",
        "jd_hook": "reduce support load",
        "opening_line": REFUND_OPENER,
        "star_r": {
            "situation": "The refund queue grew every week.",
            "task": "Bring the queue down.",
            "action": "Rebuilt the refund form.",
            "result": f"{REFUND_RESULT}.",
            "reflection": "Not evidence.",
        },
    }
]


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def _academic_scope(conn: Connection) -> Scope:
    """A new academic track holding a copy of the default track's profile."""
    add_track(conn, "lab", "academic", "Example lab search")
    scope = resolve_scope(conn, "lab")
    rows = conn.execute(
        "SELECT name, format, content FROM profile_documents "
        "WHERE kind = 'application_profile' AND track_id = 1"
    ).fetchall()
    for name, fmt, content in rows:
        put_document(conn, "application_profile", name, fmt, content, track_id=scope.track.id)
    return scope


def _store_story_bank(db: Connection) -> None:
    put_document(db, "story_bank", "story-bank.json", "json", json.dumps(STORY_BANK))


def _fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


# --- the block range ------------------------------------------------------------


def test_the_industry_letter_has_five_to_eight_blocks(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role line, hook, three body paragraphs and the close pass; the role
    line, hook and close alone are refused naming the range."""
    stub_letter(monkeypatch, letter_json())
    assert generate(db).full_version.count("\n\n") == 5

    stub_letter(monkeypatch, letter_json("", [], first="", last=""))
    assert "should contain 5 to 8 blocks, role line and close included: it has 3" in refusal(db)


def test_the_focused_letter_has_a_logistics_block() -> None:
    """Without the logistics block a focused letter has one block too few."""
    body = "I build accessible interfaces in React and TypeScript for product teams."
    close = "I can walk your team through the demo on a call."
    logistics = "I work from Exampleland in UTC+2 and invoice as a registered business."
    blocks = ["Frontend Developer, Example Studio", body, body, body, close]
    without = {"short_version": body, "full_version": "\n\n".join(blocks)}
    violations = letters_module.cover_letter_violations(without, kind="focused")
    assert violations == [
        "cover letter should contain 6 to 8 blocks, role line and close included: it has 5"
    ]

    blocks.insert(-1, logistics)
    with_logistics = {"short_version": body, "full_version": "\n\n".join(blocks)}
    assert letters_module.cover_letter_violations(with_logistics, kind="focused") == []


# --- the evidence ------------------------------------------------------------------


def test_story_bank_results_are_evidence_and_openers_are_not(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_story_bank(db)
    claims = [*GROUNDED_CLAIMS, candidate(REFUND, REFUND_RESULT)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {REFUND}", claims))
    letter = generate(db)
    assert any(
        fact.evidence == REFUND_RESULT and fact.source == "story_bank" for fact in letter.facts_used
    )

    opener = "I once rebuilt the refund queue for the whole company."
    claims = [*GROUNDED_CLAIMS, candidate(opener, REFUND_OPENER)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {opener}", claims))
    assert f"unverified evidence: {REFUND_OPENER}" in refusal(db)


def test_candidate_document_values_are_evidence(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the allowlisted fields are evidence (amendment 10). A value under
    any other key, such as a title the search includes, is configuration,
    not a fact (review of PR #405)."""
    status = "Can contract through a company in Exampleland on request"
    document = {
        "candidate": {"name": "Deniz Örnek", "location": "Exampleland", "eu_entity_status": status},
        "targets": {"titles": ["Led a platform team of engineers"]},
    }
    put_document(db, "candidate", "candidate.json", "json", json.dumps(document))
    sentence = "I can contract through a company in Exampleland on request."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, status)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    letter = generate(db)
    assert any(fact.source == "candidate" for fact in letter.facts_used)

    title = "I led a platform team of engineers."
    claims = [*GROUNDED_CLAIMS, candidate(title, "Led a platform team of engineers")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {title}", claims))
    assert "unverified evidence: Led a platform team of engineers" in refusal(db)


# --- gaps and facts used --------------------------------------------------------------


def test_gaps_and_facts_used_are_in_the_draft_only(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gap = {"requirement": "Code review practice", "posting_evidence": "We enforce code review"}
    stub_letter(monkeypatch, letter_json(gaps=[gap]))
    letter = generate(db)
    assert [(g.requirement, g.posting_evidence) for g in letter.gaps] == [
        ("Code review practice", "We enforce code review")
    ]
    paths = letters_module.write_cover_letter_artifacts(
        db,
        COMPANY,
        ROLE,
        None,
        letter.short_version,
        letter.full_version,
        output_dir=tmp_path / "letters",
        render=_fake_render,
        validate=lambda pdf_path, html_text: [],
        kind="industry",
        gaps=letter.gaps,
        facts=letter.facts_used,
    )
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert '## Gaps\n\n- Code review practice: "We enforce code review"' in markdown
    assert f'- "{CHECKOUT}" rests on "Built the checkout flow in TypeScript and React"' in markdown
    assert "(resume_truth)" in markdown
    assert markdown.index("## Full Version") < markdown.index("## Gaps")
    assert markdown.index("## Gaps") < markdown.index("## Facts used")
    html_text = paths["html"].read_text(encoding="utf-8")
    assert "Gaps" not in html_text
    assert "Facts used" not in html_text
    assert "rests on" not in html_text


def test_a_draft_with_no_gaps_says_none() -> None:
    markdown = letters_module.render_cover_letter_markdown(
        COMPANY, ROLE, None, "Short.", "Full.", gaps=(), facts=()
    )
    assert "## Gaps\n\nNone." in markdown
    assert "## Facts used\n\nNone." in markdown


def test_a_gap_must_quote_the_posting(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    gap = {"requirement": "Payments", "posting_evidence": "You will build payment rails"}
    stub_letter(monkeypatch, letter_json(gaps=[gap]))
    assert "gap evidence not in posting: You will build payment rails" in refusal(db)


def test_a_null_gap_field_is_empty_not_none(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON null is no text: a gap with no requirement is dropped, and one
    with no evidence is refused by its requirement (review of PR #405)."""
    nameless = {"requirement": None, "posting_evidence": None}
    stub_letter(monkeypatch, letter_json(gaps=[nameless]))
    assert generate(db).gaps == ()

    unquoted = {"requirement": "Payments", "posting_evidence": None}
    stub_letter(monkeypatch, letter_json(gaps=[unquoted]))
    message = refusal(db)
    assert "gap evidence not in posting: Payments" in message
    assert "None" not in message


# --- the academic kind --------------------------------------------------------------


def test_the_academic_letter_is_unchanged(db: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    """No gaps key, no role line, a question in the close: the academic
    letter parses and passes, because it is not linted. The same letter on
    the industry kind is refused by the linter."""
    full = "\n\n".join(
        [
            "I am applying for the doctoral position the group has advertised this term.",
            f"{CHECKOUT} {INVOICES}",
            "Would the group be open to a conversation about the billing research?",
        ]
    )
    response = json.dumps(
        {"short_version": "I would like to join.", "full_version": full, "claims": GROUNDED_CLAIMS}
    )
    stub_letter(monkeypatch, response)
    academic = _academic_scope(db)
    letter = generate_cover_letter(db, COMPANY, ROLE, jd_text=POSTING, scope=academic)
    assert letter.gaps == ()
    assert letter.full_version == full

    violations = refusal_on(db, full)
    assert any(v.startswith("missing role line") for v in violations)
    assert "the close asks a question" in violations


def refusal_on(db: Connection, full: str) -> list[str]:
    letter = {"short_version": "I would like to join.", "full_version": full}
    lint = letters_module.LintContext(
        evidence="", posting=POSTING, company=COMPANY, role=ROLE, professional_years=None
    )
    return letters_module.cover_letter_violations(letter, kind="industry", lint=lint)
