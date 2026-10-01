"""Letters and answers cite the evidence for every claim (spec 065).

Every test stubs the provider and goes through `generate_cover_letter` or
`generate_answer_set`, the decisions, never a rule helper alone: removing a
rule from the decision must fail its test (spec 034's lesson about testing
the helper instead of the decision).

The truth document is written here, independent of any bullet pool, so the
predicate can fail. Everything is synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
from harrier.apply import generate_answer_set, generate_cover_letter, write_cover_letter_artifacts
from harrier.apply.claims import ClaimCheckError, NeedsInputError
from harrier.apply.letters import LetterDraft
from harrier.db import connect
from harrier.profile.store import put_document

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE_JSON_PATH = REPO_ROOT / "config" / "application-profile.example.json"
PROFILE_MD_PATH = REPO_ROOT / "config" / "application-profile.example.md"

COMPANY = "Examplesoft"
ROLE = "Senior Product Engineer"

TRUTH = """# Truth

## Experience

Built the checkout flow in TypeScript and React.
Processed 1,200 invoices over 2025 with the billing service.
Cut page load time by 40% on the product pages.
Handled 300 support tickets a week during the launch.
Built a demo of the reporting dashboard on synthetic data.
Demonstrated the reporting dashboard to the finance team.
Added a lint rule that is enforced in CI for every pull request.

## Claims I must not make

Led the GraphQL migration across all services.
"""

POSTING = (
    "Examplesoft builds invoicing tools for small firms. "
    "The team ships to customers every week. "
    "We enforce code review on every change."
)

PARAGRAPH_ONE = (
    "The invoicing tools Examplesoft builds for small firms are the kind of product "
    "I want to work on next."
)
PARAGRAPH_THREE = (
    "If the role is still open, I would be glad to talk about the billing work in more detail."
)

CHECKOUT = "I built the checkout flow in TypeScript and React."
CHECKOUT_EVIDENCE = "Built the checkout flow in TypeScript and React"
INVOICES = "I processed 1,200 invoices over 2025 with the billing service."
INVOICES_EVIDENCE = "Processed 1,200 invoices over 2025 with the billing service"


def candidate(sentence: str, *evidence: str) -> dict[str, object]:
    return {"sentence": sentence, "about": "candidate", "evidence": list(evidence)}


def employer(sentence: str, *evidence: str) -> dict[str, object]:
    return {"sentence": sentence, "about": "employer", "evidence": list(evidence)}


GROUNDED_CLAIMS = [
    candidate(CHECKOUT, CHECKOUT_EVIDENCE),
    candidate(INVOICES, INVOICES_EVIDENCE),
]


def letter_json(
    middle: str = f"{CHECKOUT} {INVOICES}",
    claims: list[dict[str, object]] | None = None,
    *,
    first: str = PARAGRAPH_ONE,
    last: str = PARAGRAPH_THREE,
    short: str = "I build product features and would like to do that at Examplesoft.",
    extra_paragraphs: tuple[str, ...] = (),
) -> str:
    paragraphs = [first, middle, last, *extra_paragraphs]
    return json.dumps(
        {
            "short_version": short,
            "full_version": "\n\n".join(paragraphs),
            "claims": GROUNDED_CLAIMS if claims is None else claims,
        }
    )


def stub_letter(monkeypatch: pytest.MonkeyPatch, response: str) -> None:
    def fake(system_prompt: str, user_input: str) -> str:
        return response

    monkeypatch.setattr(letters_module, "generate_text", fake)


def stub_answers(monkeypatch: pytest.MonkeyPatch, answers: list[dict[str, object]]) -> None:
    def fake(system_prompt: str, user_input: str) -> str:
        return json.dumps({"answers": answers})

    monkeypatch.setattr(answers_module, "generate_text", fake)


def answer(
    medium: str,
    claims: list[dict[str, object]],
    *,
    short: str = "The billing work is close to what I have shipped.",
    notes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "question": "What relevant experience do you have?",
        "short_answer": short,
        "medium_answer": medium,
        "notes": notes or [],
        "claims": claims,
    }


def generate(db: sqlite3.Connection, role: str = ROLE) -> LetterDraft:
    return generate_cover_letter(db, COMPANY, role, jd_text=POSTING)


def refusal(db: sqlite3.Connection, role: str = ROLE) -> str:
    with pytest.raises(ClaimCheckError) as caught:
        generate(db, role)
    return str(caught.value)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    return seed(connect())


def seed(conn: sqlite3.Connection) -> sqlite3.Connection:
    """The synthetic profile, truth document and skill vocabulary these tests
    run against. Shared with test_apply_brief.py."""
    put_document(
        conn,
        "application_profile",
        "application-profile.json",
        "json",
        PROFILE_JSON_PATH.read_text(encoding="utf-8"),
    )
    put_document(
        conn,
        "application_profile",
        "application-profile.md",
        "markdown",
        PROFILE_MD_PATH.read_text(encoding="utf-8")
        + "\n\nShipped the mobile app rewrite for a retail client.\n",
    )
    put_document(
        conn,
        "candidate",
        "candidate.json",
        "json",
        json.dumps({"candidate": {"name": "Deniz Örnek", "location": "Exampleland"}}),
    )
    put_document(conn, "resume_truth", "truth.md", "markdown", TRUTH)
    put_document(
        conn,
        "resume_data",
        "resume-content.json",
        "json",
        json.dumps(
            {
                "all_skills": ["TypeScript", "React", "GraphQL", "Kubernetes"],
                "verified_skills": ["TypeScript", "React"],
                "technology_aliases": {
                    "TypeScript": ["typescript"],
                    "React": ["react", "react.js"],
                    "GraphQL": ["graphql"],
                    "Kubernetes": ["kubernetes", "k8s"],
                },
            }
        ),
    )
    return conn


# --- the grounded case passes ------------------------------------------------


def test_a_grounded_letter_passes_every_rule(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json())
    letter = generate(db)
    assert INVOICES in letter.full_version


def test_a_grounded_answer_set_passes_every_rule(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_answers(monkeypatch, [answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS)])
    drafts = generate_answer_set(
        db, COMPANY, ROLE, ["What relevant experience do you have?"], jd_text=POSTING
    )
    assert drafts[0].medium_answer == f"{CHECKOUT} {INVOICES}"


# --- N1: normalization refuses instead of editing --------------------------


def test_a_banned_phrase_refuses_and_is_not_deleted_mid_word(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`strip_banned_phrases("I leveraged caching")` returned "I d caching".

    The banned word refuses the letter. A longer word that merely contains it
    is neither refused nor cut, because matching is on word boundaries.
    """
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} I leverage caching there."))
    assert "banned phrase: leverage" in refusal(db)

    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} I leveraged caching there."))
    assert "I leveraged caching there." in generate(db).full_version


def test_an_answer_with_invented_evidence_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I ran the payments platform team."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, "Ran the payments platform team")]
    stub_answers(monkeypatch, [answer(f"{CHECKOUT} {INVOICES} {sentence}", claims)])
    with pytest.raises(ClaimCheckError, match="unverified evidence"):
        generate_answer_set(db, COMPANY, ROLE, ["Why?"], jd_text=POSTING)


def test_a_banned_phrase_refuses_the_answers(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_answers(monkeypatch, [answer(f"{CHECKOUT} I am thrilled to apply.", GROUNDED_CLAIMS)])
    with pytest.raises(ClaimCheckError, match="banned phrase"):
        generate_answer_set(db, COMPANY, ROLE, ["Why?"], jd_text=POSTING)


def test_a_letter_over_240_words_is_refused_not_trimmed(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    padding = " ".join(["The billing work matters to the people who use it every day."] * 22)
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {padding}"))
    assert "over the word limit" in refusal(db)


def test_a_stub_paragraph_refuses_instead_of_being_dropped(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json(last="Thanks for reading."))
    assert "stub paragraph" in refusal(db)


def test_a_fourth_paragraph_refuses_instead_of_being_dropped(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(
        monkeypatch,
        letter_json(extra_paragraphs=("I would also like to mention my interest in the team.",)),
    )
    assert "too many paragraphs" in refusal(db)


# --- C1 to C4: the citations ---------------------------------------------------


def test_a_claim_sentence_missing_from_the_letter_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = [*GROUNDED_CLAIMS, candidate("I rebuilt the search service.", CHECKOUT_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(claims=claims))
    assert "claim sentence not in output" in refusal(db)


def test_a_claim_with_invented_evidence_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I ran the payments platform team."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, "Ran the payments platform team")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "unverified evidence" in refusal(db)


def test_evidence_from_a_disclaimer_section_does_not_verify(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I led the migration across all services."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Led the GraphQL migration across all services"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "unverified evidence" in refusal(db)


def test_evidence_only_in_the_application_profile_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile is for framing. A story only there is not verified."""
    sentence = "I shipped the mobile app rewrite for a retail client."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Shipped the mobile app rewrite for a retail client"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "unverified evidence" in refusal(db)


def test_employer_evidence_absent_from_the_posting_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "Examplesoft is the market leader in invoicing."
    claims = [*GROUNDED_CLAIMS, employer(sentence, "the market leader in invoicing")]
    stub_letter(monkeypatch, letter_json(first=f"{sentence} {PARAGRAPH_ONE}", claims=claims))
    assert "employer evidence not in posting" in refusal(db)


def test_a_first_person_sentence_cited_to_the_employer_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a posting's requirement could verify the candidate's claim."""
    sentence = "I have shipped to customers every week."
    claims = [*GROUNDED_CLAIMS, employer(sentence, "ships to customers every week")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "first-person sentence cited to the employer" in refusal(db)


# --- C5 and C6: numbers --------------------------------------------------------


def test_a_number_absent_from_its_evidence_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I also cut build times by 35% in the same year."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}"))
    assert "number without evidence" in refusal(db)


def test_numbers_in_the_company_and_role_are_exempt(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    role = "Senior Engineer 2"
    first = f"The {role} role at Examplesoft is the kind of product work I want to do next."
    stub_letter(monkeypatch, letter_json(first=first))
    assert generate(db, role).full_version


def test_mixed_tokens_and_slash_pairs_are_not_numbers(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = "I am happy to talk about storage on S3, OAuth2 and running a service 24/7 any time."
    stub_letter(monkeypatch, letter_json(last=last))
    assert generate(db).full_version


def test_a_total_rewritten_as_a_rate_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The truth says 1,200 over a year. The letter says 1,200 a month."""
    sentence = "I processed 1,200 invoices a month with the billing service."
    claims = [candidate(CHECKOUT, CHECKOUT_EVIDENCE), candidate(sentence, INVOICES_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {sentence}", claims))
    assert "number changed scope" in refusal(db)


def test_a_total_rewritten_with_a_slash_suffix_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I processed 1,200/month invoices with the billing service."
    claims = [candidate(CHECKOUT, CHECKOUT_EVIDENCE), candidate(sentence, INVOICES_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {sentence}", claims))
    assert "number changed scope" in refusal(db)


def test_a_rate_kept_as_a_rate_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I handled 300 support tickets a week during the launch."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Handled 300 support tickets a week during the launch"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


def test_a_percentage_without_its_sign_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I cut page load time by 40 on the product pages."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Cut page load time by 40% on the product pages"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "number changed scope" in refusal(db)


def test_a_rate_is_read_from_the_line_the_evidence_was_quoted_from(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quoting "300 support tickets" out of "300 support tickets a week"
    must not make the kept rate look like a change of scope."""
    sentence = "I handled 300 support tickets a week during the launch."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, "300 support tickets")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


# --- C7: synthetic data is labelled --------------------------------------------


def test_unlabelled_synthetic_evidence_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I built the reporting dashboard."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, "the reporting dashboard")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert "synthetic evidence not labelled" in refusal(db)


def test_labelled_synthetic_evidence_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I built a demo of the reporting dashboard on synthetic data."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Built a demo of the reporting dashboard on synthetic data"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


def test_demonstrated_is_not_a_synthetic_marker(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I walked the finance team through the reporting dashboard."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Demonstrated the reporting dashboard to the finance team"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


# --- C8: skills ----------------------------------------------------------------


def test_an_unverified_skill_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = "I would be glad to talk about the billing work and how it ran on Kubernetes."
    stub_letter(monkeypatch, letter_json(last=last))
    assert "unverified skill" in refusal(db)


def test_an_alias_of_a_verified_skill_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    last = "I would be glad to talk about the billing work and the React.js front end."
    stub_letter(monkeypatch, letter_json(last=last))
    assert "React.js" in generate(db).full_version


def test_a_role_title_term_is_not_a_skill_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    role = "Senior GraphQL Engineer"
    first = f"The {role} role at Examplesoft is the kind of product work I want to do next."
    stub_letter(monkeypatch, letter_json(first=first))
    assert generate(db, role).full_version


# --- C9: enforcement language --------------------------------------------------


def test_enforcement_language_without_evidence_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = (
        "I built the checkout flow in TypeScript and React, with types enforced at every boundary."
    )
    claims = [candidate(sentence, CHECKOUT_EVIDENCE), candidate(INVOICES, INVOICES_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(f"{sentence} {INVOICES}", claims))
    assert "enforcement claimed without evidence" in refusal(db)


def test_enforcement_language_with_evidence_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I added a lint rule that is enforced in CI for every pull request."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Added a lint rule that is enforced in CI for every pull request"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


# --- C10: placeholders -----------------------------------------------------------


PLACEHOLDER = "[[TODO: one concrete example of a billing incident]]"


def test_a_letter_with_a_placeholder_writes_markdown_and_no_pdf(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {PLACEHOLDER}"))
    letter = generate(db)
    rendered: list[Path] = []

    def render(html_text: str, pdf_path: Path) -> None:
        rendered.append(pdf_path)

    with pytest.raises(NeedsInputError) as caught:
        write_cover_letter_artifacts(
            db,
            COMPANY,
            ROLE,
            None,
            letter.short_version,
            letter.full_version,
            output_dir=tmp_path,
            render=render,
        )
    assert caught.value.placeholders == [PLACEHOLDER]
    assert caught.value.markdown_path.is_file()
    assert PLACEHOLDER in caught.value.markdown_path.read_text(encoding="utf-8")
    assert not rendered
    assert not list(tmp_path.glob("*.html"))
    assert not list(tmp_path.glob("*.pdf"))


def test_a_placeholder_run_removes_the_pdf_and_html_of_an_earlier_run(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The paths are per company and role, and the spec 047 artifact endpoint
    serves whatever PDF is there. Without this, a rerun that stops on a
    placeholder leaves the earlier PDF on offer beside the new draft (review
    of #84)."""
    stub_letter(monkeypatch, letter_json())
    letter = generate(db)

    def render(html_text: str, pdf_path: Path) -> None:
        pdf_path.write_bytes(b"%PDF-1.4\n")

    def validate(pdf_path: Path, html_text: str) -> list[str]:
        return []

    paths = write_cover_letter_artifacts(
        db,
        COMPANY,
        ROLE,
        None,
        letter.short_version,
        letter.full_version,
        output_dir=tmp_path,
        template_dir=REPO_ROOT / "templates",
        render=render,
        validate=validate,
    )
    assert paths["pdf"].is_file()
    assert paths["html"].is_file()

    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {PLACEHOLDER}"))
    draft = generate(db)
    with pytest.raises(NeedsInputError):
        write_cover_letter_artifacts(
            db, COMPANY, ROLE, None, draft.short_version, draft.full_version, tmp_path
        )
    assert PLACEHOLDER in paths["markdown"].read_text(encoding="utf-8")
    assert not paths["pdf"].exists()
    assert not paths["html"].exists()


def test_a_bracketed_insert_is_a_placeholder_too(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} [Insert a short example here]"))
    letter = generate(db)
    with pytest.raises(NeedsInputError) as caught:
        write_cover_letter_artifacts(
            db, COMPANY, ROLE, None, letter.short_version, letter.full_version, tmp_path
        )
    assert caught.value.placeholders == ["[Insert a short example here]"]


def test_cli_cover_letter_exits_3_and_names_each_placeholder(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from harrier.tracker.actions import add_manually
    from harrier_cli.main import main

    _, row = add_manually(db, company=COMPANY, title=ROLE, url="https://example.com/jobs/1")
    assert row is not None
    jd_file = tmp_path / "posting.txt"
    jd_file.write_text(POSTING, encoding="utf-8")
    second = "[[TODO: the team size you worked in]]"
    stub_letter(monkeypatch, letter_json(last=f"{PARAGRAPH_THREE} {PLACEHOLDER} {second}"))

    code = main(["cover-letter", "--job-id", row["id"], "--jd-file", str(jd_file)])

    out = capsys.readouterr().out
    assert code == 3
    assert f"needs_input={PLACEHOLDER}" in out
    assert f"needs_input={second}" in out
    markdown_line = next(line for line in out.splitlines() if line.startswith("markdown="))
    markdown_path = Path(markdown_line.removeprefix("markdown="))
    assert markdown_path.is_file()
    assert not markdown_path.with_suffix(".pdf").exists()
    assert not markdown_path.with_suffix(".html").exists()


# --- the refusal as a whole --------------------------------------------------------


def test_every_violation_is_listed_in_one_refusal(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    invented = "I ran the payments platform team."
    numbered = "I also cut build times by 35% in the same year."
    claims = [*GROUNDED_CLAIMS, candidate(invented, "Ran the payments platform team")]
    last = "I would be glad to talk about the billing work and how it ran on Kubernetes."
    stub_letter(
        monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {invented} {numbered}", claims, last=last)
    )
    message = refusal(db)
    for rule in ("unverified evidence", "number without evidence", "unverified skill"):
        assert rule in message


def test_a_response_without_claims_fails_to_parse(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = json.loads(letter_json())
    del response["claims"]
    stub_letter(monkeypatch, json.dumps(response))
    with pytest.raises(RuntimeError, match="failed to parse AI response"):
        generate(db)


def test_a_claim_without_evidence_fails_to_parse(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json(claims=[{"sentence": CHECKOUT, "about": "candidate"}]))
    with pytest.raises(RuntimeError, match="failed to parse AI response"):
        generate(db)
