"""The cover letter linter, rule by rule (spec 158, L1 to L11; spec 165, L18 to L22).

`lint_letter` is the decision: the pipeline adds its messages to the letter's
refusal, and every rule here is the linter's own. Each test breaks one rule
in a letter that otherwise passes, so removing a rule fails its test.
Everything is synthetic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from harrier.apply.claims import ClaimContext, check_claims, parse_claims
from harrier.apply.letterlint import AskCoverage, LintContext, lint_letter
from harrier.resume.content import SkillVocabulary, TruthSources

FIXTURES = Path(__file__).parent / "fixtures" / "letters"

COMPANY = "Example GmbH"
ROLE = "Frontend Engineer"

EVIDENCE = """Cut page load time by ~12% on the product pages.
Built a design system in React with documented components for product teams.
Shipped the design system in May 2024.
"""

POSTING = (
    "Example GmbH sells payment tools to small shops across Europe. "
    "You will own the checkout experience end to end and improve conversion."
)

ROLE_LINE = "Frontend Engineer, Payments team, Example GmbH"
HOOK = "I cut page load time by ~12% on the product pages."
# Spec 165: the proof does not open with "I", so the base letter has no two
# "I" sentences in a row (L20) and no two paragraphs on the same word (L22).
PROOF = "For product teams, I built a design system in React with documented components."
WHY = "Payment tools for small shops are the product I want to build next."
CLOSE = "I can walk your team through the design system on a call."
# A body block with no number, skill or hedge, so the base letter clears
# spec 160's floor of 120 words.
BODY = (
    "Most of that work shipped in small steps behind a review. The support team said what"
    " still confused the people who used it, and the next change fixed that before anything"
    " new started. That rhythm kept the product steady while it grew, and it kept the design"
    " and the code from drifting apart over time. Each review asked one plain question: would"
    " a new user understand this screen on the first try."
)

CONTEXT = LintContext(
    evidence=EVIDENCE, posting=POSTING, company=COMPANY, role=ROLE, professional_years=5
)


def letter(
    *,
    role_line: str = ROLE_LINE,
    hook: str = HOOK,
    proof: str = PROOF,
    why: str = WHY,
    close: str = CLOSE,
    body: str = BODY,
    extra: tuple[str, ...] = (),
) -> str:
    return "\n\n".join(
        block for block in [role_line, hook, proof, why, body, *extra, close] if block
    )


def lint(full: str, context: LintContext = CONTEXT, short: str = "") -> list[str]:
    return lint_letter(full, context, short)


PADDING = " ".join(["The design work served the people who used it every day."] * 20)

BROKEN = {
    "L1 dash": (letter(proof=PROOF.replace("system in", "system - in")), "dash as punctuation"),
    "L2 hedge": (letter(close=f"I think {CLOSE[0].lower()}{CLOSE[1:]}"), "hedging: I think"),
    "L3 length": (letter(extra=(PADDING,)), "over the word limit"),
    "L4 opener": (
        letter(hook=f"Example GmbH needs speed, and {HOOK[0].lower()}{HOOK[1:]}"),
        "banned opener",
    ),
    "L5 banned": (letter(why=f"I am excited that {WHY[0].lower()}{WHY[1:]}"), "excited"),
    "L6 sentences": (letter(close=f"{CLOSE} It took a year."), "more than one sentence"),
    "L6 question": (
        letter(close="Can I walk your team through the design system on a call?"),
        "asks a question",
    ),
    "L7 restatement": (
        letter(why="You will own the checkout experience end to end and improve conversion."),
        "restates the posting",
    ),
    "L8 number": (letter(hook=HOOK.replace("12%", "14%")), "number not in the evidence: 14%"),
    "L9 years": (letter(proof=f"{PROOF} That is 6+ years of frontend work."), "states 6 years"),
    "L9 decade": (letter(proof=f"{PROOF} It has been nearly a decade."), "decades"),
    "L10 entity": (letter(proof=f"{PROOF} I contract through my GmbH."), "my GmbH"),
    "L11 role line": (
        letter(role_line="I am applying to Example GmbH as a frontend engineer."),
        "missing role line",
    ),
}


def test_the_base_letter_passes_every_rule() -> None:
    assert lint(letter()) == []


@pytest.mark.parametrize("rule", sorted(BROKEN))
def test_each_rule_refuses_and_its_fix_passes(rule: str) -> None:
    """The broken letter names its rule; the base letter, the same letter
    with that one defect fixed, passes (the test above)."""
    broken, message = BROKEN[rule]
    violations = lint(broken)
    assert any(message in violation for violation in violations), violations


def test_a_number_must_be_in_the_evidence() -> None:
    """ "~12%" in the evidence backs "12%" for L8; nothing backs "14%". Since
    spec 160, L13 refuses "12%" for dropping the "~" every evidence line
    gives it."""
    assert lint(letter()) == []
    assert lint(letter(hook=HOOK.replace("~12%", "14%"))) == ["number not in the evidence: 14%"]
    dropped = lint(letter(hook=HOOK.replace("~12%", "12%")))
    assert not any(v.startswith("number not in the evidence") for v in dropped)
    assert [v.split(":")[0] for v in dropped] == ["a number lost its qualifier"]


def test_compound_hyphens_are_not_punctuation() -> None:
    assert lint(letter(proof=PROOF.replace("a design", "a well-known design"))) == []
    for mark in (" - ", " -- ", "--"):
        violations = lint(letter(proof=PROOF.replace(" in ", f"{mark}in ", 1)))
        assert any(v.startswith("dash as punctuation in full_version") for v in violations), mark
    assert lint(letter(), short="Short - version.") == [
        "dash as punctuation in short_version: spaced hyphen"
    ]


def test_may_the_month_is_not_a_hedge() -> None:
    assert lint(letter(proof=f"{PROOF} I shipped it in May 2024.")) == []
    assert any("hedging: might" in v for v in lint(letter(proof=f"{PROOF} It might help.")))


def test_year_count_is_capped_by_the_record() -> None:
    """The record holds 5 years: "6+ years" is refused, "5+ years" is not."""
    with_years = LintContext(
        evidence=f"{EVIDENCE}Professional career of 5+ years, 6 teams.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
        professional_years=5,
    )
    refused = lint(letter(proof=f"{PROOF} That is 6+ years of frontend work."), with_years)
    assert refused == ["states 6 years of experience; the record holds 5"]
    assert lint(letter(proof=f"{PROOF} That is 5+ years of frontend work."), with_years) == []


def entity_message(match: str) -> str:
    return f'calls a legal entity \'{match}\': write "a" or "an" before the entity type'


def test_an_unregistered_entity_is_not_mine() -> None:
    """Spec 159: "my GmbH" is refused whether or not the evidence states a
    registered GmbH, and "a GmbH" passes."""
    mine = letter(proof=f"{PROOF} I contract through my GmbH.")
    assert lint(mine) == [entity_message("my GmbH")]
    assert lint(letter(proof=f"{PROOF} I can contract through a GmbH.")) == []
    registered = LintContext(
        evidence=f"{EVIDENCE}Contracts through a GmbH registered in Exampleland.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    assert lint(mine, registered) == [entity_message("my GmbH")]


def test_an_owner_word_before_an_entity_type_is_refused() -> None:
    """Spec 159: "my" or "our", up to two words, any whitespace, then a type
    in any dotted or spaced spelling."""
    for phrase, match in (
        ("my own GmbH", "my own GmbH"),
        ("my Exampleland GmbH", "my Exampleland GmbH"),
        ("my\nGmbH", "my GmbH"),
        ("my  GmbH", "my GmbH"),
        ("our GmbH", "our GmbH"),
        ("my sp. z o. o.", "my sp. z o. o"),
        ("my B.V.", "my B.V"),
        ("my\nown  GmbH", "my own GmbH"),
    ):
        assert lint(letter(proof=f"{PROOF} I contract through {phrase} now.")) == [
            entity_message(match)
        ], phrase
    assert lint(letter(), short="I contract through our GmbH.") == [entity_message("our GmbH")]


def test_the_evidence_never_permits_my_entity() -> None:
    """Spec 159: L10 does not read the evidence, so no line lets "my GmbH"
    through, not even one that states the candidate's own is registered."""
    mine = letter(proof=f"{PROOF} I contract through my GmbH.")
    for line in (
        "My partner owns a GmbH registered in Exampleland.",
        "My boss's GmbH is registered in Exampleland.",
        "The founder of Acme GmbH, registered in Germany, hired me.",
        "My GmbH will be registered in Exampleland.",
        "My GmbH was registered and deregistered.",
        "Owner of Example GmbH, registered in Exampleland.",
        "My GmbH is registered in Exampleland.",
    ):
        context = LintContext(
            evidence=f"{EVIDENCE}{line}\n", posting=POSTING, company=COMPANY, role=ROLE
        )
        assert lint(mine, context) == [entity_message("my GmbH")], line


def test_an_article_or_a_name_passes() -> None:
    """Spec 159: an article, a company name, three words between, a longer
    word and a sentence end are not an owner word before a type."""
    for sentence in (
        "I can contract through an Estonian OÜ.",
        "I can contract through a GmbH.",
        "Example GmbH ships payment tools.",
        "I led my work at Example GmbH alone.",
        "I set up my Ltda contract.",
        "That was my job. GmbH registration took a week.",
    ):
        assert lint(letter(proof=f"{PROOF} {sentence}")) == [], sentence


def test_the_short_version_is_linted_too() -> None:
    """The short version goes into an application textbox (review of #405)."""
    assert lint(letter(), short="Two decades building payment systems.") == [
        "counts experience in decades"
    ]
    assert lint(letter(), short="I think it fits.") == ["hedging: I think"]
    assert lint(letter(), short="I contract through my GmbH.") == [entity_message("my GmbH")]


def test_a_run_the_evidence_also_has_is_not_a_restatement() -> None:
    """Spec 158 amendment 1: the posting may phrase a requirement in the very
    words of the candidate's own fact."""
    posting = f"{POSTING} Built a design system in React with documented components."
    context = LintContext(evidence=EVIDENCE, posting=posting, company=COMPANY, role=ROLE)
    assert lint(letter(), context) == []


# --- spec 160 ---------------------------------------------------------------------


def test_six_shared_words_and_too_few_words_are_refused() -> None:
    six = letter(why="I would own the checkout experience end to end here too.")
    assert any(
        v.startswith("restates the posting: own the checkout experience end to") for v in lint(six)
    )
    words = len(letter().split())
    short = letter(body=" ".join(BODY.split()[: 119 - (words - len(BODY.split()))]))
    assert len(short.split()) == 119
    assert "under the word floor: 119 words, at least 120" in lint(short)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_context(letter_doc: Any) -> LintContext:
    facts: dict[str, str] = _fixture("restream-profile.json")["facts"]
    asks: list[dict[str, Any]] = letter_doc["asks"]["asks"]
    used: list[dict[str, str]] = letter_doc["facts_used"]
    return LintContext(
        evidence="\n".join(facts.values()),
        posting=(FIXTURES / "restream-posting.txt").read_text(encoding="utf-8"),
        company="restream",
        role="Design Engineer - Studio Team",
        professional_years=5,
        company_form=str(letter_doc["asks"]["company"]),
        asks=tuple(
            AskCoverage(
                rank=int(ask["rank"]),
                ask=str(ask["ask"]),
                fact_texts=tuple(facts[fact_id] for fact_id in ask["fact_ids"]),
                listed_as_gap=bool(ask["gap"]),
            )
            for ask in asks[:3]
        ),
        cited=tuple(fact["evidence"] for fact in used),
        link_line=letter_doc.get("link_line") or None,
    )


def test_the_bad_fixture_fails_and_the_target_passes() -> None:
    """The synthetic copies of the Restream letters (spec 160, section 9)."""
    bad = _fixture("restream-bad.json")
    violations = lint_letter(str(bad["full_version"]), _fixture_context(bad))
    for expected in (
        "a tilde on an exact percentage: ~41%",
        "a tilde on an exact percentage: ~58%",
        "the link line is missing: Portfolio: portfolio.example.dev",
        "a sentence reports what the company says:",
        "company name casing: 'restream', the posting writes 'Restream'",
        "ask 1 not addressed by its facts:",
        "ask 3 not addressed by its facts:",
    ):
        assert any(v.startswith(expected) for v in violations), (expected, violations)

    target = _fixture("restream-target.json")
    assert lint_letter(str(target["full_version"]), _fixture_context(target)) == []


def test_a_qualifier_counts_only_next_to_its_number() -> None:
    """A "~" on another number in the line qualifies nothing (spec 160,
    amendment 3): the regeneration of a real letter found L13 reading the
    whole line."""
    context = LintContext(
        evidence=f"{EVIDENCE}Grew signup completion from 41% to 58% across ~120K signups.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    exact = letter(proof=f"{PROOF} Signup completion grew from 41% to 58% across ~120K signups.")
    assert lint(exact, context) == []
    dropped = letter(proof=f"{PROOF} Signup completion grew from 41% to 58% across 120K signups.")
    assert lint(dropped, context) == [
        "a number lost its qualifier: 120k (in: Signup completion grew from 41% to 58% across"
        " 120K signups.)"
    ]


# --- spec 165: how the letter reads (L18 to L22) ---

FACT_ID = "r1_b2"
FACT = "Built a design system in React with documented components for product teams."
FACT_CONTEXT = replace(CONTEXT, fact_texts=((FACT_ID, FACT),))

PROSE_BROKEN = {
    "L18 negative": (
        letter(proof=f"{PROOF} I had to ship it in a month."),
        "negative framing: had to",
    ),
    "L19 passive": (
        letter(proof=f"{PROOF} My React skills were strengthened through that work."),
        "passive growth: were strengthened through",
    ),
    "L20 I openings": (
        letter(proof="I built a design system in React with documented components."),
        '"I" or "My" starts 2 of 7 body sentences, two in a row',
    ),
    "L21 copied fact": (
        letter(proof=f"Product teams got one thing above all: I {FACT[0].lower()}{FACT[1:]}"),
        f"copies fact {FACT_ID} word for word",
    ),
    "L22 paragraph openers": (
        letter(why="For small shops, payment tools are the product I want to build next."),
        'two body paragraphs open with "For"',
    ),
}


@pytest.mark.parametrize("rule", sorted(PROSE_BROKEN))
def test_prose_rules_refuse_and_their_fixes_pass(rule: str) -> None:
    """Each broken letter names its rule; the base letter, which is the same
    letter with that defect fixed, passes with the fact in the context."""
    broken, message = PROSE_BROKEN[rule]
    assert any(message in violation for violation in lint(broken, FACT_CONTEXT)), lint(
        broken, FACT_CONTEXT
    )
    assert lint(letter(), FACT_CONTEXT) == []


def _body_letter(*sentences: str) -> str:
    return "\n\n".join([ROLE_LINE, " ".join(sentences), CLOSE])


def _i_violations(full: str) -> list[str]:
    return [v for v in lint(full) if v.startswith('"I" or "My"')]


def test_i_openings_count_runs_and_share() -> None:
    """L20 refuses two "I" sentences in a row, or more than a third of the
    body; three in nine with none in a row pass."""
    other = "The team shipped it."
    mine = "I shipped it."
    assert _i_violations(_body_letter(mine, mine, *[other] * 7)) == [
        '"I" or "My" starts 2 of 9 body sentences, two in a row'
    ]
    assert _i_violations(_body_letter(*[mine, other] * 3, other, other, other)) == []
    assert _i_violations(_body_letter(*[mine, other] * 4, other)) == [
        '"I" or "My" starts 4 of 9 body sentences'
    ]
    # "My" counts, and a curly apostrophe reads as a straight one.
    assert _i_violations(
        _body_letter("My team shipped it.", f"I{chr(0x2019)}ve shipped it.", other)
    ) == ['"I" or "My" starts 2 of 3 body sentences, two in a row']


def test_a_copied_fact_is_refused_and_a_reworded_one_passes() -> None:
    """L21 names the fact, never its text. The reworded sentence keeps the
    tool and passes L21 and the claim check (spec 065) on the same fact."""
    copied = f"For product teams, I {FACT[0].lower()}{FACT[1:]}"
    violations = lint(letter(proof=copied), FACT_CONTEXT)
    assert f"copies fact {FACT_ID} word for word: say it in other words" in violations
    assert not any(FACT in violation for violation in violations)

    reworded = "Product teams now build on a documented React design system that I built."
    assert lint(letter(proof=reworded), FACT_CONTEXT) == []
    context = ClaimContext(
        sources=TruthSources(truth_text=FACT, achievements_text=""),
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
        vocabulary=SkillVocabulary(terms=("React",), verified=frozenset({"react"})),
    )
    claims = parse_claims([{"sentence": reworded, "about": "candidate", "evidence": [FACT]}])
    assert check_claims([reworded], claims, context) == []


def test_passive_growth_needs_the_preposition() -> None:
    passive = letter(proof=f"{PROOF} The pipeline was built through a migration.")
    assert "passive growth: was built through" in lint(passive)
    active = letter(proof=f"{PROOF} Later, I built a migration path for it.")
    assert not any(v.startswith("passive growth") for v in lint(active))


def test_the_bullet_copy_fixture_fails_and_the_target_passes() -> None:
    """The synthetic letter written as CV bullets fails L20 and L21; the
    spec 160 target, revised to the new voice, passes every rule."""
    facts: dict[str, str] = _fixture("restream-profile.json")["facts"]

    def context(letter_doc: Any) -> LintContext:
        given = dict.fromkeys(f for ask in letter_doc["asks"]["asks"] for f in ask["fact_ids"])
        return replace(_fixture_context(letter_doc), fact_texts=tuple((f, facts[f]) for f in given))

    bad = _fixture("bullet-copy-bad.json")
    violations = lint_letter(str(bad["full_version"]), context(bad))
    for expected in (
        '"I" or "My" starts',
        "copies fact r1_b2 word for word",
        "copies fact r1_b3 word for word",
    ):
        assert any(v.startswith(expected) for v in violations), (expected, violations)

    target = _fixture("restream-target.json")
    assert lint_letter(str(target["full_version"]), context(target)) == []
