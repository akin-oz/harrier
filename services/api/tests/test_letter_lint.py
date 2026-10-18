"""The cover letter linter, rule by rule (spec 158, L1 to L11).

`lint_letter` is the decision: the pipeline adds its messages to the letter's
refusal, and every rule here is the linter's own. Each test breaks one rule
in a letter that otherwise passes, so removing a rule fails its test.
Everything is synthetic.
"""

from __future__ import annotations

import pytest

from harrier.apply.letterlint import LintContext, lint_letter

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
HOOK = "I cut page load time by 12% on the product pages."
PROOF = "I built a design system in React with documented components for product teams."
WHY = "Payment tools for small shops are the product I want to build next."
CLOSE = "I can walk your team through the design system on a call."

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
    extra: tuple[str, ...] = (),
) -> str:
    return "\n\n".join([role_line, hook, proof, why, *extra, close])


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
    "L4 number": (letter(hook=f"I care about fast pages. {HOOK}"), "first sentence has no number"),
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
    """ "~12%" in the evidence backs "12%"; nothing backs "14%"."""
    assert lint(letter()) == []
    assert lint(letter(hook=HOOK.replace("12%", "14%"))) == [
        "the hook's first sentence has no number from the evidence",
        "number not in the evidence: 14%",
    ]


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


def test_an_unregistered_entity_is_not_mine() -> None:
    mine = letter(proof=f"{PROOF} I contract through my GmbH.")
    assert lint(mine) == ["calls an entity the evidence does not show registered 'my GmbH'"]
    assert lint(letter(proof=f"{PROOF} I can contract through a GmbH.")) == []
    pending = LintContext(
        evidence=f"{EVIDENCE}A GmbH is not registered yet.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    assert lint(mine, pending) != []
    # A whole word: "unregistered" is not registration (review of PR #405).
    unregistered = LintContext(
        evidence=f"{EVIDENCE}GmbH unregistered, filing pending.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    assert lint(mine, unregistered) == [
        "calls an entity the evidence does not show registered 'my GmbH'"
    ]
    registered = LintContext(
        evidence=f"{EVIDENCE}Contracts through a GmbH registered in Exampleland.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    assert lint(mine, registered) == []


def test_a_negated_registration_is_not_registration() -> None:
    """ "never registered", "isn't registered" and "not yet" say the
    opposite (review of #405)."""
    mine = letter(proof=f"{PROOF} I contract through my GmbH.")
    for line in (
        "A GmbH is planned, never registered.",
        "The GmbH isn't registered.",
        "No GmbH is registered yet.",
    ):
        context = LintContext(
            evidence=f"{EVIDENCE}{line}\n", posting=POSTING, company=COMPANY, role=ROLE
        )
        assert lint(mine, context) == [
            "calls an entity the evidence does not show registered 'my GmbH'"
        ], line


def test_an_employers_registered_entity_is_not_mine() -> None:
    """A registered GmbH the candidate worked at is not the candidate's own
    (review of #405)."""
    mine = letter(proof=f"{PROOF} I contract through my GmbH.")
    employer = LintContext(
        evidence=f"{EVIDENCE}Worked at Acme GmbH, registered in Germany.\n",
        posting=POSTING,
        company=COMPANY,
        role=ROLE,
    )
    assert lint(mine, employer) == [
        "calls an entity the evidence does not show registered 'my GmbH'"
    ]


def test_the_short_version_is_linted_too() -> None:
    """The short version goes into an application textbox (review of #405)."""
    assert lint(letter(), short="Two decades building payment systems.") == [
        "counts experience in decades"
    ]
    assert lint(letter(), short="I think it fits.") == ["hedging: I think"]
    assert lint(letter(), short="I contract through my GmbH.") == [
        "calls an entity the evidence does not show registered 'my GmbH'"
    ]


def test_a_run_the_evidence_also_has_is_not_a_restatement() -> None:
    """Spec 158 amendment 1: the posting may phrase a requirement in the very
    words of the candidate's own fact."""
    posting = f"{POSTING} Built a design system in React with documented components."
    context = LintContext(evidence=EVIDENCE, posting=posting, company=COMPANY, role=ROLE)
    assert lint(letter(), context) == []
