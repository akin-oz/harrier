"""The letter's shape, evidence and draft sections (spec 158).

Every test goes through `generate_cover_letter`, `cover_letter_violations`
or `write_cover_letter_artifacts`, the decisions, with the provider stubbed.
The fixtures are test_apply_claims.py's. Everything is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from resume_support import DEFAULT_SCOPE
from test_apply_claims import (
    CHECKOUT,
    CLOSE,
    COMPANY,
    GROUNDED_CLAIMS,
    INVOICES,
    POSTING,
    PROFILE_JSON_PATH,
    ROLE,
    ROLE_LINE,
    TRUTH,
    candidate,
    generate,
    letter_json,
    refusal,
    seed,
    stub_letter,
)

import harrier.apply.letters as letters_module
import harrier.apply.postingasks as postingasks
from harrier.apply import generate_cover_letter
from harrier.apply.claims import ClaimCheckError
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


def test_the_industry_letter_has_four_to_ten_blocks(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture's seven blocks pass; the role line, hook and close alone
    are refused naming the range (spec 160: four to ten)."""
    stub_letter(monkeypatch, letter_json())
    assert generate(db).full_version.count("\n\n") == 6

    stub_letter(monkeypatch, letter_json("", [], first="", last="", filler=""))
    assert "should contain 4 to 10 blocks, role line and close included: it has 3" in refusal(db)


def test_the_focused_letter_has_a_logistics_block() -> None:
    """Without the logistics block a shortest focused letter has one block
    too few (spec 160: five to ten)."""
    body = "I build accessible interfaces in React and TypeScript for product teams."
    close = "I can walk your team through the demo on a call."
    logistics = "I work from Exampleland in UTC+2 and invoice as a registered business."
    blocks = ["Frontend Developer, Example Studio", body, body, close]
    without = {"short_version": body, "full_version": "\n\n".join(blocks)}
    violations = letters_module.cover_letter_violations(without, kind="focused")
    assert violations == [
        "cover letter should contain 5 to 10 blocks, role line and close included: it has 4"
    ]

    blocks.insert(-1, logistics)
    with_logistics = {"short_version": body, "full_version": "\n\n".join(blocks)}
    assert letters_module.cover_letter_violations(with_logistics, kind="focused") == []


# --- the evidence ------------------------------------------------------------------


def test_story_bank_text_never_reaches_the_writer_or_the_evidence(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The offer evaluator's model wrote the story bank, so no field of it
    verifies a claim (spec 158, amendment 15), and since spec 160 the writer
    is not sent it at all."""
    _store_story_bank(db)
    seen: list[str] = []

    def fake(system_prompt: str, user_input: str) -> str:
        seen.append(user_input)
        claims = [*GROUNDED_CLAIMS, candidate(REFUND, REFUND_RESULT)]
        return letter_json(f"{CHECKOUT} {INVOICES} {REFUND}", claims)

    monkeypatch.setattr(letters_module, "generate_text", fake)
    assert f"unverified evidence: {REFUND_RESULT}" in refusal(db)
    assert "story_bank" not in seen[0]
    assert REFUND_RESULT not in seen[0]

    opener = "I once rebuilt the refund queue for the whole company."
    claims = [*GROUNDED_CLAIMS, candidate(opener, REFUND_OPENER)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {opener}", claims))
    assert f"unverified evidence: {REFUND_OPENER}" in refusal(db)


def test_the_writer_payload_names_no_story_company_or_role(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A story's company and role are the posting it was written for, never
    the candidate's employer (review of #405); the writer sees neither."""
    stories = [{**STORY_BANK[0], "company": "Acme", "role": "Staff Engineer"}]
    put_document(db, "story_bank", "story-bank.json", "json", json.dumps(stories))
    seen: list[str] = []

    def fake(system_prompt: str, user_input: str) -> str:
        seen.append(user_input)
        return letter_json()

    monkeypatch.setattr(letters_module, "generate_text", fake)
    generate(db)
    assert "Acme" not in seen[0]


def test_a_disclaimer_line_does_not_register_an_entity(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The linter reads asserted lines only: a line under "Claims I must not
    make" registers nothing (review of #405)."""
    put_document(
        db, "resume_truth", "truth.md", "markdown", f"{TRUTH}That my GmbH is registered.\n"
    )
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} I contract through my GmbH."))
    assert "calls a legal entity 'my GmbH'" in refusal(db)


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
    """Since spec 160 the gaps are the posting analysis's: an ask no fact
    meets, with its posting quote."""

    def analysis(system_prompt: str, user_input: str) -> str:
        facts = list(json.loads(user_input)["facts"])
        return json.dumps(
            {
                "asks": [
                    {
                        "ask": "invoicing tools",
                        "quote": "builds invoicing tools",
                        "fact_ids": facts,
                    },
                    {
                        "ask": "Code review practice",
                        "quote": "We enforce code review",
                        "fact_ids": [],
                    },
                ],
                "logistics": None,
                "product_fact_ids": [],
            }
        )

    monkeypatch.setattr(postingasks, "generate_text", analysis)
    stub_letter(monkeypatch, letter_json())
    letter = generate(db)
    assert ("Code review practice", "We enforce code review") in [
        (g.requirement, g.posting_evidence) for g in letter.gaps
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
    assert '- Code review practice: "We enforce code review"' in markdown
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
    """Since spec 160 a gap is an analysis ask with no facts, and its quote
    must be in the posting like every ask's."""

    def analysis(system_prompt: str, user_input: str) -> str:
        facts = list(json.loads(user_input)["facts"])
        return json.dumps(
            {
                "asks": [
                    {
                        "ask": "invoicing tools",
                        "quote": "builds invoicing tools",
                        "fact_ids": facts,
                    },
                    {"ask": "Payments", "quote": "You will build payment rails", "fact_ids": []},
                ],
                "logistics": None,
                "product_fact_ids": [],
            }
        )

    monkeypatch.setattr(postingasks, "generate_text", analysis)
    stub_letter(monkeypatch, letter_json())
    with pytest.raises(
        ClaimCheckError, match="quote not in the posting: You will build payment rails"
    ):
        generate(db)


def test_a_null_gap_field_is_empty_not_none(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON null is no text (review of PR #405): the parser drops a gap with
    no requirement and keeps an empty quote, never the text "None". Since
    spec 160 the industry letter ignores the writer's gaps, so nothing the
    writer sends reaches the draft either."""
    response = json.dumps(
        {
            "gaps": [
                {"requirement": None, "posting_evidence": None},
                {"requirement": "Payments", "posting_evidence": None},
            ]
        }
    )
    assert letters_module.parse_cover_letter_gaps(response) == [
        letters_module.Gap(requirement="Payments", posting_evidence="")
    ]
    stub_letter(
        monkeypatch, letter_json(gaps=[{"requirement": "Payments", "posting_evidence": None}])
    )
    gaps = generate(db).gaps
    assert all("None" not in g.requirement and g.posting_evidence != "None" for g in gaps)
    assert "Payments" not in [g.requirement for g in gaps]


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


# --- spec 165: motivation and the letter's frame ----------------------------------


def test_motivation_reaches_the_writer_or_is_a_gap(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The track's `motivation` is in the writer payload. Without one, the
    payload's is empty and the asks document lists the gap."""
    seen: list[dict[str, object]] = []

    def writer(system_prompt: str, user_input: str) -> str:
        seen.append(json.loads(user_input))
        return letter_json()

    monkeypatch.setattr(letters_module, "generate_text", writer)
    letter = generate(db)
    assert seen[-1]["motivation"] == ""
    assert letter.asks is not None
    assert letters_module.MOTIVATION_GAP in cast("list[str]", letter.asks["gaps"])
    assert letters_module.MOTIVATION_GAP in [gap.requirement for gap in letter.gaps]

    profile = json.loads(PROFILE_JSON_PATH.read_text(encoding="utf-8"))
    motivation = "Billing products are the work I want to do more of."
    put_document(
        db,
        "application_profile",
        "application-profile.json",
        "json",
        json.dumps({**profile, "motivation": motivation}),
        track_id=1,
    )
    letter = generate(db)
    assert seen[-1]["motivation"] == motivation
    assert letter.asks is not None
    assert letters_module.MOTIVATION_GAP not in cast("list[str]", letter.asks["gaps"])


def test_a_letter_without_a_posting_still_lists_the_motivation_gap(
    db: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no posting text there is no analysis, but an empty `motivation`
    is still a gap in the asks document."""
    stub_letter(monkeypatch, letter_json())
    letter = generate_cover_letter(db, COMPANY, ROLE, jd_text=None, scope=DEFAULT_SCOPE)
    assert letter.asks is not None
    assert letters_module.MOTIVATION_GAP in cast("list[str]", letter.asks["gaps"])


def test_the_industry_letter_has_a_salutation_and_sign_off(
    db: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The industry HTML opens with the salutation and ends with the sign-off
    and the name; neither is in the letter text or the markdown draft. The
    academic letter has neither."""
    stub_letter(monkeypatch, letter_json())
    letter = generate(db)

    def write(kind: str, directory: str) -> dict[str, Path]:
        return letters_module.write_cover_letter_artifacts(
            db,
            COMPANY,
            ROLE,
            None,
            letter.short_version,
            letter.full_version,
            output_dir=tmp_path / directory,
            render=_fake_render,
            validate=lambda pdf_path, html_text: [],
            kind=kind,
        )

    paths = write("industry", "industry")
    html_text = paths["html"].read_text(encoding="utf-8")
    salutation = html_text.index("Dear Hiring Manager,")
    assert salutation < html_text.index(ROLE_LINE)
    sign_off = html_text.index("Kind regards,")
    assert html_text.index(CLOSE) < sign_off < html_text.index('<p class="signature">')
    assert "Dear Hiring Manager" not in letter.full_version
    markdown = paths["markdown"].read_text(encoding="utf-8")
    assert "Dear Hiring Manager" not in markdown
    assert "Kind regards" not in markdown

    academic = write("academic", "academic")["html"].read_text(encoding="utf-8")
    assert "Dear Hiring Manager" not in academic
    assert "Kind regards" not in academic
