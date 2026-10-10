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
import logging
import sqlite3
from pathlib import Path

import pytest

import harrier.apply.answers as answers_module
import harrier.apply.letters as letters_module
from harrier.apply import generate_answer_set, generate_cover_letter, write_cover_letter_artifacts
from harrier.apply.brief import EMPTY_BRIEF, Brief
from harrier.apply.claims import (
    ClaimCheckError,
    ClaimContext,
    NeedsInputError,
    check_claims,
    parse_claims,
)
from harrier.apply.letters import LetterDraft
from harrier.apply.profile import profile_text
from harrier.db import connect
from harrier.profile.store import put_document
from harrier.resume.content import load_skill_vocabulary, load_truth_sources
from harrier.tracks import default_scope

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
    """The profile is for framing. A story only there is not verified, and
    the refusal says where the text came from (spec 069)."""
    sentence = "I shipped the mobile app rewrite for a retail client."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Shipped the mobile app rewrite for a retail client"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    message = refusal(db)
    assert "application profile cited as candidate evidence" in message
    assert "unverified evidence" not in message


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


# --- spec 069: misattributed candidate evidence is named -----------------------

POSTED = "Examplesoft builds invoicing tools for small firms."
PROFILE_LINE = "Shipped the mobile app rewrite for a retail client"


def test_posting_text_cited_as_candidate_evidence_is_named(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observed refusal: a posting sentence labelled as the candidate's."""
    sentence = "I build invoicing tools for small firms."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, POSTED)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    message = refusal(db)
    assert f"posting text cited as candidate evidence: {POSTED}" in message
    assert "unverified evidence" not in message


def test_evidence_in_neither_document_is_still_unverified(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I ran the payments platform team."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, "Ran the payments platform team")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    message = refusal(db)
    assert "unverified evidence: Ran the payments platform team" in message
    assert "cited as candidate evidence" not in message


def test_evidence_in_the_truth_and_the_posting_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truth wins: the posting asking for it does not undo that the
    candidate did it."""
    stub_letter(monkeypatch, letter_json())
    posting = f"{POSTING} {CHECKOUT_EVIDENCE}."
    letter = generate_cover_letter(db, COMPANY, ROLE, jd_text=posting)
    assert CHECKOUT in letter.full_version


def test_evidence_in_the_posting_and_the_profile_gets_the_posting_message(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I shipped the mobile app rewrite for a retail client."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, PROFILE_LINE)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    with pytest.raises(ClaimCheckError) as caught:
        generate_cover_letter(db, COMPANY, ROLE, jd_text=f"{POSTING} {PROFILE_LINE}.")
    message = str(caught.value)
    assert "posting text cited as candidate evidence" in message
    assert "application profile cited" not in message


def test_the_answers_path_names_posting_text_cited_as_candidate_evidence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I build invoicing tools for small firms."
    claims = [*GROUNDED_CLAIMS, candidate(sentence, POSTED)]
    stub_answers(monkeypatch, [answer(f"{CHECKOUT} {INVOICES} {sentence}", claims)])
    with pytest.raises(ClaimCheckError, match="posting text cited as candidate evidence"):
        generate_answer_set(
            db, COMPANY, ROLE, ["What relevant experience do you have?"], jd_text=POSTING
        )


def test_with_no_profile_stored_profile_text_is_unverified_and_nothing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    conn = connect()
    put_document(conn, "resume_truth", "truth.md", "markdown", TRUTH)
    profile = profile_text(conn)
    assert profile == ""
    context = ClaimContext(
        sources=load_truth_sources(conn),
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
        vocabulary=load_skill_vocabulary(conn),
        profile=profile,
    )
    sentence = "I shipped the mobile app rewrite for a retail client."
    claims = parse_claims([candidate(sentence, PROFILE_LINE)])
    assert check_claims([sentence], claims, context) == [f"unverified evidence: {PROFILE_LINE}"]


def test_both_prompts_forbid_citing_the_posting_or_profile_for_the_candidate() -> None:
    """The prompt text is the decision here: the rule is either sent or not.
    Whether the model obeys it is not testable (spec 069 Limitations)."""
    for prompt in (letters_module.SYSTEM_PROMPT_BASE, answers_module.SYSTEM_PROMPT_BASE):
        flat = " ".join(prompt.split())
        assert (
            "Never cite job_description_text or the application profile as candidate evidence"
            in flat
        )
        assert "not something the candidate did" in flat


# --- spec 068: inline markup is formatting, not text --------------------------

MARKED_TRUTH = "Wrote the `sync-contract` script that bundles the **billing** schema."
MARKED_POSTING = "Examplesoft builds invoicing tools. We ship the **billing** `export` weekly."


def with_marked_truth(db: sqlite3.Connection) -> None:
    put_document(
        db, "resume_truth", "truth.md", "markdown", TRUTH + f"\n## Tooling\n\n{MARKED_TRUTH}\n"
    )


def test_a_claim_quoting_a_backticked_truth_line_without_backticks_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal that prompted spec 068, rebuilt synthetically."""
    with_marked_truth(db)
    sentence = "I wrote the sync-contract script that bundles the billing schema."
    claims = [
        *GROUNDED_CLAIMS,
        candidate(sentence, "Wrote the sync-contract script that bundles the billing schema"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}", claims))
    assert sentence in generate(db).full_version


def test_employer_evidence_quoted_without_markers_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "Examplesoft ships the billing export weekly."
    claims = [*GROUNDED_CLAIMS, employer(sentence, "We ship the billing export weekly")]
    stub_letter(monkeypatch, letter_json(first=f"{sentence} {PARAGRAPH_ONE}", claims=claims))
    letter = generate_cover_letter(db, COMPANY, ROLE, jd_text=MARKED_POSTING)
    assert sentence in letter.full_version


def test_employer_evidence_absent_after_marker_removal_is_still_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "Examplesoft ships the billing export daily."
    claims = [*GROUNDED_CLAIMS, employer(sentence, "We ship the billing export daily")]
    stub_letter(monkeypatch, letter_json(first=f"{sentence} {PARAGRAPH_ONE}", claims=claims))
    with pytest.raises(ClaimCheckError, match="employer evidence not in posting"):
        generate_cover_letter(db, COMPANY, ROLE, jd_text=MARKED_POSTING)


def test_a_plain_claim_sentence_matches_output_that_carries_markers(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = [candidate(CHECKOUT, CHECKOUT_EVIDENCE), *GROUNDED_CLAIMS[1:]]
    marked = "I built the **checkout flow** in TypeScript and React."
    stub_letter(monkeypatch, letter_json(f"{marked} {INVOICES}", claims))
    assert marked in generate(db).full_version


def test_a_marked_claim_sentence_matches_plain_output(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked = "I built the **checkout flow** in TypeScript and React."
    claims = [candidate(marked, CHECKOUT_EVIDENCE), *GROUNDED_CLAIMS[1:]]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES}", claims))
    assert CHECKOUT in generate(db).full_version


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

    _, row = add_manually(
        db, default_scope(db), company=COMPANY, title=ROLE, url="https://example.com/jobs/1"
    )
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


# --- one retry after a refusal (spec 085) ------------------------------------------

QUESTION = "What relevant experience do you have?"
REWORDED = "I rebuilt the search service."
NUMBERED = "I also cut build times by 35% in the same year."


def stub_sequence(
    monkeypatch: pytest.MonkeyPatch, module: object, responses: list[str]
) -> list[tuple[str, str]]:
    """The model returns each response in turn. Every call is recorded, and a
    call past the last response fails the test rather than repeating one."""
    calls: list[tuple[str, str]] = []

    def fake(system_prompt: str, user_input: str) -> str:
        calls.append((system_prompt, user_input))
        assert len(calls) <= len(responses), f"model called {len(calls)} times"
        return responses[len(calls) - 1]

    monkeypatch.setattr(module, "generate_text", fake)
    return calls


def refused_letter() -> str:
    """Refused on C1 only: a declared sentence the letter does not contain."""
    return letter_json(claims=[*GROUNDED_CLAIMS, candidate(REWORDED, CHECKOUT_EVIDENCE)])


def answers_json(*answers: dict[str, object]) -> str:
    return json.dumps({"answers": list(answers)})


def refused_answers() -> str:
    return answers_json(
        answer(
            CHECKOUT,
            [candidate(CHECKOUT, CHECKOUT_EVIDENCE), candidate(REWORDED, CHECKOUT_EVIDENCE)],
        )
    )


def passing_answers() -> str:
    return answers_json(answer(f"{CHECKOUT} {INVOICES}", GROUNDED_CLAIMS))


def test_a_refused_letter_is_retried_once_and_the_second_draft_is_kept(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = stub_sequence(monkeypatch, letters_module, [refused_letter(), letter_json()])
    letter = generate(db)
    assert len(calls) == 2
    assert [claim.sentence for claim in letter.claims] == [CHECKOUT, INVOICES]


def test_a_refused_answer_set_is_retried_once_and_the_second_set_is_written(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from harrier.tracker.actions import add_manually
    from harrier_cli.main import main

    _, row = add_manually(
        db, default_scope(db), company=COMPANY, title=ROLE, url="https://example.com/jobs/1"
    )
    assert row is not None
    jd_file = tmp_path / "posting.txt"
    jd_file.write_text(POSTING, encoding="utf-8")
    calls = stub_sequence(monkeypatch, answers_module, [refused_answers(), passing_answers()])

    code = main(
        ["answers", "--job-id", str(row["id"]), "--question", QUESTION, "--jd-file", str(jd_file)]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert len(calls) == 2
    answers_line = next(line for line in out.splitlines() if line.startswith("answers="))
    written = Path(answers_line.removeprefix("answers=")).read_text(encoding="utf-8")
    assert f"{CHECKOUT} {INVOICES}" in written
    assert REWORDED not in written


def test_the_retry_sends_the_refusals_and_the_previous_response(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = refused_letter()
    calls = stub_sequence(monkeypatch, letters_module, [first, letter_json()])
    generate(db)

    (first_prompt, first_input), (second_prompt, second_input) = calls
    assert second_prompt == first_prompt
    second = json.loads(second_input)
    retry = second.pop("retry")
    assert second == json.loads(first_input)
    assert retry["refusals"] == [f"claim sentence not in output: {REWORDED}"]
    assert retry["previous_response"] == first
    assert retry["instruction"]


def test_a_second_refusal_fails_with_its_own_violations(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    unclaimed_number = letter_json(f"{CHECKOUT} {INVOICES} {NUMBERED}")
    calls = stub_sequence(monkeypatch, letters_module, [refused_letter(), unclaimed_number])
    message = refusal(db)
    assert len(calls) == 2
    assert "number without evidence" in message
    assert "claim sentence not in output" not in message


def test_cli_answers_exits_1_after_two_refusals(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from harrier.apply.answers import answers_path_for
    from harrier.tracker.actions import add_manually
    from harrier_cli.main import main

    _, row = add_manually(
        db, default_scope(db), company=COMPANY, title=ROLE, url="https://example.com/jobs/1"
    )
    assert row is not None
    jd_file = tmp_path / "posting.txt"
    jd_file.write_text(POSTING, encoding="utf-8")
    calls = stub_sequence(monkeypatch, answers_module, [refused_answers(), refused_answers()])

    code = main(
        ["answers", "--job-id", str(row["id"]), "--question", QUESTION, "--jd-file", str(jd_file)]
    )

    assert code == 1
    assert len(calls) == 2
    assert "answers failed:" in capsys.readouterr().err
    assert not answers_path_for(COMPANY, ROLE).exists()


def test_a_passing_first_response_calls_the_model_once(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    letter_calls = stub_sequence(monkeypatch, letters_module, [letter_json(), refused_letter()])
    generate(db)
    answer_calls = stub_sequence(
        monkeypatch, answers_module, [passing_answers(), refused_answers()]
    )
    generate_answer_set(db, COMPANY, ROLE, [QUESTION], jd_text=POSTING)
    assert len(letter_calls) == 1
    assert len(answer_calls) == 1


def test_a_parse_failure_is_not_retried(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    malformed = letter_json(claims=[{"sentence": CHECKOUT, "about": "candidate"}])
    calls = stub_sequence(monkeypatch, letters_module, [malformed, letter_json()])
    with pytest.raises(RuntimeError, match="failed to parse AI response"):
        generate(db)
    assert len(calls) == 1


def test_a_placeholder_is_not_retried(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_placeholder = answers_json(
        answer(f"{CHECKOUT} {PLACEHOLDER}", [candidate(CHECKOUT, CHECKOUT_EVIDENCE)])
    )
    calls = stub_sequence(monkeypatch, answers_module, [with_placeholder, passing_answers()])
    drafts = generate_answer_set(db, COMPANY, ROLE, [QUESTION], jd_text=POSTING)
    assert len(calls) == 1
    assert PLACEHOLDER in drafts[0].medium_answer


def test_the_retry_logs_the_first_refusals(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub_sequence(monkeypatch, answers_module, [refused_answers(), passing_answers()])
    with caplog.at_level(logging.WARNING, logger=answers_module.__name__):
        generate_answer_set(db, COMPANY, ROLE, [QUESTION], jd_text=POSTING)
    retries = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("answers refused on attempt 1, retrying once:")
    ]
    assert len(retries) == 1
    assert f"claim sentence not in output: {REWORDED}" in retries[0]


# --- spec 086: a number refusal names its sentence ---------------------------------

MENTORED = "I mentored 4 engineers on the team."


def violations(db: sqlite3.Connection) -> list[str]:
    with pytest.raises(ClaimCheckError) as caught:
        generate(db)
    return caught.value.violations


def test_a_number_refusal_names_its_sentence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = f"I build product features. {MENTORED}"
    stub_letter(monkeypatch, letter_json(short=short))
    assert violations(db) == [f"number without evidence: 4 (in: {MENTORED})"]


def test_a_scope_refusal_names_its_sentence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I processed 1,200 invoices a month with the billing service."
    claims = [candidate(CHECKOUT, CHECKOUT_EVIDENCE), candidate(sentence, INVOICES_EVIDENCE)]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {sentence}", claims))
    assert violations(db) == [f"number changed scope: 1,200 (in: {sentence})"]


def test_the_same_number_in_two_sentences_is_refused_in_each(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    workshops = "I ran 4 workshops for the support staff."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {MENTORED} {workshops}"))
    assert violations(db) == [
        f"number without evidence: 4 (in: {MENTORED})",
        f"number without evidence: 4 (in: {workshops})",
    ]


def test_a_sentence_in_both_letter_versions_is_refused_once(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {MENTORED}", short=MENTORED))
    assert violations(db) == [f"number without evidence: 4 (in: {MENTORED})"]


def test_a_repeated_number_in_one_sentence_names_the_word_before_it(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`40%` and `40` are one value, so each is told apart by the word before
    it. The `3` inside `300` is not the value 3, so `3` keeps the plain form."""
    pages = "I cut load time by 40% across 40 product pages."
    drills = "I ran 3 drills before 300 tickets arrived."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {pages} {drills}"))
    assert violations(db) == [
        f'number without evidence: 40% (after "by" in: {pages})',
        f'number without evidence: 40 (after "across" in: {pages})',
        f"number without evidence: 3 (in: {drills})",
        f"number without evidence: 300 (in: {drills})",
    ]


def test_a_repeated_number_that_opens_its_sentence_says_first_word(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = "40 product pages loaded faster once I cut load time by 40%."
    joined = "12 engineers joined the billing team after the launch."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {pages} {joined}"))
    assert violations(db) == [
        f"number without evidence: 40 (first word of: {pages})",
        f'number without evidence: 40% (after "by" in: {pages})',
        f"number without evidence: 12 (in: {joined})",
    ]


def test_a_field_without_end_punctuation_is_its_own_sentence(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short version and the full version are joined by a blank line. A
    short version with no end punctuation is still quoted alone."""
    short = "Billing work across 4 teams"
    stub_letter(monkeypatch, letter_json(short=short))
    assert violations(db) == [f"number without evidence: 4 (in: {short})"]


def test_the_retry_receives_the_sentence_of_a_number_refusal(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = letter_json(f"{CHECKOUT} {INVOICES} {MENTORED}")
    calls = stub_sequence(monkeypatch, letters_module, [first, letter_json()])
    generate(db)
    retry = json.loads(calls[1][1])["retry"]
    assert retry["refusals"] == [f"number without evidence: 4 (in: {MENTORED})"]


# --- spec 087: a version the truth sources state needs no claim --------------------

PIPELINE = "Ran the event pipeline on Kafka 3."
ON_KAFKA = "I ran the event pipeline on Kafka 3."
EVERY_FIRST = (
    "Every invoicing tool Examplesoft builds for small firms is the kind of product "
    "I want to work on next."
)
STACK = "Event pipelines (Kafka 3, Django)"


def with_versions(
    db: sqlite3.Connection,
    *lines: str,
    disclaimed: tuple[str, ...] = (),
    verified: tuple[str, ...] = (),
) -> None:
    """The spec 087 vocabulary, and truth lines added under their own
    heading. `disclaimed` lines go under the truth document's "Claims I must
    not make" heading instead."""
    body = TRUTH + "".join(f"{line}\n" for line in disclaimed)
    if lines:
        body += "\n## Stack\n\n" + "".join(f"{line}\n" for line in lines)
    put_document(db, "resume_truth", "truth.md", "markdown", body)
    skills = ["TypeScript", "React", "Kafka", "Apache Kafka", "Django", "Spring", "Go", "make"]
    put_document(
        db,
        "resume_data",
        "resume-content.json",
        "json",
        json.dumps(
            {
                "all_skills": skills,
                "verified_skills": [
                    "TypeScript",
                    "React",
                    "Kafka",
                    "Apache Kafka",
                    "Django",
                    "Spring",
                    *verified,
                ],
                "technology_aliases": {},
            }
        ),
    )


def refused_versions(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *sentences: str,
    posting: str = POSTING,
    brief: Brief = EMPTY_BRIEF,
) -> list[str]:
    """The violations of a letter holding each sentence undeclared, or an
    empty list when the letter passes."""
    stub_letter(monkeypatch, letter_json(" ".join([CHECKOUT, INVOICES, *sentences])))
    try:
        generate_cover_letter(db, COMPANY, ROLE, jd_text=posting, brief=brief)
    except ClaimCheckError as caught:
        return caught.violations
    return []


def unclaimed(raw: str, sentence: str) -> str:
    return f"number without evidence: {raw} (in: {sentence})"


def test_a_version_the_truth_states_needs_no_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The short version has no end punctuation, so over the joined text the
    `3` reads as a rate from the "Every" that opens the full version. Read on
    its own line it does not."""
    with_versions(db, PIPELINE)
    stub_letter(monkeypatch, letter_json(short=STACK, first=EVERY_FIRST))
    assert generate(db).short_version == STACK


def test_a_grounded_version_passes_in_an_answer(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    medium = f"Every billing change I shipped went through review. {CHECKOUT} {INVOICES}"
    stub_answers(monkeypatch, [answer(medium, GROUNDED_CLAIMS, short=STACK)])
    drafts = generate_answer_set(db, COMPANY, ROLE, [QUESTION], jd_text=POSTING)
    assert drafts[0].short_answer == STACK


def test_an_invented_version_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I ran the event pipeline on Kafka 4."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("4", sentence)]


def test_a_version_inside_a_longer_number_is_not_grounded(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    for line in (
        "Ran the event pipeline on Kafka 30.",
        "Ran the event pipeline on Kafka 3.6.",
        "Ran a Kafka 3-based event pipeline.",
    ):
        with_versions(db, line)
        assert refused_versions(db, monkeypatch, ON_KAFKA) == [unclaimed("3", ON_KAFKA)], line


def test_a_more_precise_version_than_the_truth_is_refused(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I ran the event pipeline on Kafka 3.6."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("3.6", sentence)]


def test_a_number_after_a_word_outside_the_vocabulary_still_needs_a_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec's "led 3 squads." is also refused because a noun follows the
    number (V1.5). "Of the teams, I led 3." ends the phrase, so only the
    vocabulary refuses it, against a truth line that spells "led" the same
    way."""
    with_versions(db, "Led 3 teams.", "Hired and led 3 teams.")
    squads = "As the lead I led 3 squads."
    assert refused_versions(db, monkeypatch, squads) == [unclaimed("3", squads)]
    teams = "Of the teams, I led 3."
    assert refused_versions(db, monkeypatch, teams) == [unclaimed("3", teams)]


def test_a_grounded_version_does_not_exempt_the_same_number_elsewhere(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I ran Kafka 3 and led 3 workshops."
    assert refused_versions(db, monkeypatch, sentence) == [
        f'number without evidence: 3 (after "led" in: {sentence})'
    ]


def test_a_version_with_a_rate_is_not_exempt(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I run Kafka 3, every week."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("3", sentence)]


def test_a_number_followed_by_a_noun_is_not_exempt(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(
        db,
        "Ran the event pipeline on Kafka 3.6 in production.",
        PIPELINE,
        "Helped make 3 senior hires.",
    )
    events = "I moved Kafka 3.6 million events through the pipeline."
    years = "I have Kafka 3 plus years of tooling behind me."
    dashboards = "I helped make 3 dashboards for finance."
    assert refused_versions(db, monkeypatch, events, years, dashboards) == [
        unclaimed("3.6", events),
        unclaimed("3", years),
        unclaimed("3", dashboards),
    ]


def test_years_after_a_version_are_not_a_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    ago = "I ran Kafka 3 years ago."
    assert refused_versions(db, monkeypatch, ago) == [unclaimed("3", ago)]

    with_versions(db, "Ran Kafka 3 years in production.")
    assert refused_versions(db, monkeypatch, ON_KAFKA) == [unclaimed("3", ON_KAFKA)]

    with_versions(db, PIPELINE)
    assert refused_versions(db, monkeypatch, ON_KAFKA, "Day to day I keep it running.") == []


def test_years_money_and_multipliers_after_a_term_are_not_versions(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(
        db,
        "Moved in Spring 2024.",
        "Joined in Spring '24.",
        "Cut the Kafka $40k bill.",
        "Made Django 10x faster.",
    )
    year = "I moved the services to Spring 2024."
    short_year = "I joined the team on Spring 24."
    money = "I cut the bill on Kafka $40k."
    multiplier = "I made the pages on Django 10x."
    assert refused_versions(db, monkeypatch, year, short_year, money, multiplier) == [
        unclaimed("2024", year),
        unclaimed("24", short_year),
        unclaimed("$40k", money),
        unclaimed("10x", multiplier),
    ]


def test_a_demo_line_does_not_ground_a_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, "Built a demo pipeline on Kafka 3.")
    labelled = "I built a demo pipeline on Kafka 3."
    assert refused_versions(db, monkeypatch, ON_KAFKA) == [unclaimed("3", ON_KAFKA)]
    assert refused_versions(db, monkeypatch, labelled) == [unclaimed("3", labelled)]


def test_only_supporting_truth_lines_ground_a_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = [unclaimed("3", ON_KAFKA)]

    with_versions(db)
    posting = f"{POSTING} {PIPELINE}"
    assert refused_versions(db, monkeypatch, ON_KAFKA, posting=posting) == refused

    with_versions(db, disclaimed=(PIPELINE,))
    assert refused_versions(db, monkeypatch, ON_KAFKA) == refused

    with_versions(db, "Did not ship Kafka 3 to production.")
    assert refused_versions(db, monkeypatch, ON_KAFKA) == refused

    with_versions(db)
    put_document(
        db,
        "application_profile",
        "application-profile.md",
        "markdown",
        PROFILE_MD_PATH.read_text(encoding="utf-8") + f"\n\n{PIPELINE}\n",
    )
    assert refused_versions(db, monkeypatch, ON_KAFKA) == refused

    with_versions(db, verified=("Kafka 3",))
    assert refused_versions(db, monkeypatch, ON_KAFKA) == refused

    with_versions(db)
    brief = Brief(evidence=(PIPELINE,))
    assert refused_versions(db, monkeypatch, ON_KAFKA, brief=brief) == []


def test_a_marked_term_still_names_its_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    assert refused_versions(db, monkeypatch, "I ran the event pipeline on **Kafka** 3.") == []


def test_a_placeholder_between_term_and_number_breaks_the_phrase(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I ran the event pipeline on Kafka [[TODO: cluster]] 3."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("3", ON_KAFKA)]


def test_a_word_ending_in_a_term_does_not_name_a_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each truth line holds the same text as the output, so only where the
    term may start decides."""
    with_versions(db, "Ran non-Kafka 2.", "Set up my.kafka 3.")
    hyphen = "I also ran non-Kafka 2."
    dotted = "I also set up my.kafka 3."
    assert refused_versions(db, monkeypatch, hyphen, dotted) == [
        unclaimed("2", hyphen),
        unclaimed("3", dotted),
    ]


def test_the_term_must_be_spelled_the_same_in_the_truth(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, "In spring 2 engineers joined.", "Wrote the service in go 1.")
    spring = "I moved the services to Spring 2."
    assert refused_versions(db, monkeypatch, spring) == [unclaimed("2", spring)]
    go = "I wrote the service in go 1."
    assert refused_versions(db, monkeypatch, go) == [unclaimed("1", go)]


def test_the_longest_term_names_the_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, PIPELINE)
    sentence = "I ran the event pipeline on Apache Kafka 3."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("3", sentence)]


# --- spec 113: a number in inline code or underscore emphasis is a number ----------

IN_CODE = "I led `12` engineers on the billing team."


def test_a_number_in_inline_code_needs_a_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {IN_CODE}"))
    assert violations(db) == [f"number without evidence: 12 (in: {IN_CODE})"]


def test_a_number_in_underscore_emphasis_needs_a_claim(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I led _12_ engineers on the billing team."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}"))
    assert violations(db) == [f"number without evidence: 12 (in: {sentence})"]


def test_a_claimed_number_in_inline_code_passes(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_versions(db, "Led 12 engineers on the billing team.")
    claims = [*GROUNDED_CLAIMS, candidate(IN_CODE, "Led 12 engineers on the billing team")]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {IN_CODE}", claims))
    assert IN_CODE in generate(db).full_version


def test_a_rate_read_from_markup_keeps_its_scope(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The truth line holds `1,200` as a total in inline code. The claim
    cites it, so the rewrite as a rate is a change of scope, not a number
    without evidence."""
    with_versions(db, "Processed `1,200` invoices for the reporting team.")
    sentence = "I processed 1,200 invoices a month for the reporting team."
    claims = [
        candidate(CHECKOUT, "Built the checkout flow in TypeScript and React"),
        candidate(sentence, "Processed `1,200` invoices for the reporting team"),
    ]
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {sentence}", claims))
    assert violations(db) == [f"number changed scope: 1,200 (in: {sentence})"]


def test_a_number_in_markup_no_longer_drops_a_grounded_version(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both readings of the line now find `3` and `2`, so the `3` the truth
    states stays exempt and the invented `2` is refused."""
    with_versions(db, PIPELINE)
    sentence = "I ran the event pipeline on **Kafka** 3 and `2` replicas."
    assert refused_versions(db, monkeypatch, sentence) == [unclaimed("2", sentence)]


def test_the_word_before_a_repeated_number_drops_its_markup(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I ran `deploy` 3 times and `rollback` 3 times in the drill."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}"))
    assert violations(db) == [
        f'number without evidence: 3 (after "deploy" in: {sentence})',
        f'number without evidence: 3 (after "rollback" in: {sentence})',
    ]


def test_markup_inside_a_word_does_not_make_a_number(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentence = "I renamed retry_3 and a`12`b and 1_000 in the billing service."
    stub_letter(monkeypatch, letter_json(f"{CHECKOUT} {INVOICES} {sentence}"))
    assert sentence in generate(db).full_version
