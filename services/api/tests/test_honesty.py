"""The truth gate refuses rather than omits (spec 034).

The honesty invariant is the only promise this project makes about a document
that goes to a real employer under a real name, and it did not hold. The
predicate was substring containment: case-sensitive, section-blind,
polarity-blind. Failure was silent omission, so an empty truth document
produced a clean PDF with empty sections and a tracker status advance.

The adversarial cases below are the point. Each one is a truth document
defeating itself, which is what a containment check permits.
"""

from __future__ import annotations

import pytest

from harrier.resume.content import (
    TruthSources,
    asserting_lines,
    forbidden_hits,
    strip_inline_markup,
)


def sources(truth: str, achievements: str = "") -> TruthSources:
    return TruthSources(truth_text=truth, achievements_text=achievements)


# --- structure: a disclaimer section does not verify its own list -----------


def test_a_claim_under_a_must_not_claim_heading_does_not_verify() -> None:
    """The sharpest case. A truth document written carefully, with a section
    recording what the candidate must never say, validated every claim in
    that section under the old predicate."""
    document = """
## What is true

Led the design system rewrite.

## Claims I must not make

Managed a team of twelve engineers.
Owned the incident response rota.
"""
    assert sources(document).contains("Led the design system rewrite")
    assert not sources(document).contains("Managed a team of twelve engineers")
    assert not sources(document).contains("Owned the incident response rota")


@pytest.mark.parametrize(
    "heading",
    [
        "## Must not claim",
        "## Do not claim",
        "## Never claim",
        "## Forbidden phrases",
        "## Not true",
        "Claims to avoid claiming:",
    ],
)
def test_every_disclaimer_heading_shape_is_recognised(heading: str) -> None:
    document = f"## True\n\nShipped the checkout rewrite.\n\n{heading}\n\nRan the whole company.\n"
    assert sources(document).contains("Shipped the checkout rewrite")
    assert not sources(document).contains("Ran the whole company")


def test_a_disclaimer_section_ends_at_the_next_heading() -> None:
    """Otherwise one disclaimer heading would invalidate the rest of the
    document, which is a different way of being wrong."""
    document = """
## Must not claim

Ran the whole company.

## Also true

Rebuilt the deployment pipeline.
"""
    assert not sources(document).contains("Ran the whole company")
    assert sources(document).contains("Rebuilt the deployment pipeline")


def test_asserting_lines_drops_only_the_disclaimer_block() -> None:
    document = "## True\nA\n## Must not claim\nB\n## True again\nC\n"
    kept = "\n".join(asserting_lines(document))
    assert "A" in kept
    assert "C" in kept
    assert "\nB" not in kept


# --- polarity: a negated sentence does not verify its own substring ---------


@pytest.mark.parametrize(
    "sentence",
    [
        "I did not own the incident response rota.",
        "I never owned the incident response rota.",
        "I have not owned the incident response rota.",
        "I was not responsible for the incident response rota.",
        "I contributed to reviews rather than owned the incident response rota.",
        "Others led it instead of me owning the incident response rota.",
    ],
)
def test_a_negated_sentence_does_not_verify_the_claim_it_denies(sentence: str) -> None:
    assert not sources(sentence).contains("owned the incident response rota")


def test_a_plain_assertion_still_verifies() -> None:
    assert sources("I owned the incident response rota.").contains(
        "owned the incident response rota"
    )


# --- polarity: every negation shape a truth document writes (spec 100) -------

# Each of these verified the fragment beside it under the ten space-padded
# phrases the gate used to read.
DENIALS_THE_OLD_LIST_MISSED = [
    ("I didn't lead the team.", "lead the team"),
    ("Didn't lead the team.", "lead the team"),
    ("Not responsible for hiring.", "responsible for hiring"),
    ("I wasn't responsible for hiring.", "responsible for hiring"),
    ("It isn't a system I owned.", "a system I owned"),
    ("I'm not a people manager.", "a people manager"),
    ("I haven't run Kafka in production.", "run Kafka in production"),
    ("I can't claim ownership of the budget.", "ownership of the budget"),
    ("I cannot claim ownership of the budget.", "ownership of the budget"),
    ("I don't manage people.", "manage people"),
    ("I had no direct reports.", "direct reports"),
    ("No direct reports.", "direct reports"),
    ("None of the hiring was mine.", "the hiring was mine"),
    ("Neither led the team nor owned the budget.", "owned the budget"),
    ("Owned all services except billing.", "billing"),
    ("Owned all services excluding billing.", "billing"),
    ("Owned all services other than billing.", "billing"),
    ("Owned all services apart from billing.", "billing"),
    ("Shipped the API (not the mobile app).", "the mobile app"),
    ("Shipped the API (did not own the mobile app).", "own the mobile app"),
    ("Shipped the API,never owned the mobile app.", "owned the mobile app"),
    ("Never: owned the budget.", "owned the budget"),
    ("I failed to ship the mobile app.", "ship the mobile app"),
    ("I lacked ownership of the budget.", "ownership of the budget"),
    ("I didn\u2019t lead the team.", "lead the team"),
]


@pytest.mark.parametrize(("line", "fragment"), DENIALS_THE_OLD_LIST_MISSED)
def test_every_negation_shape_is_read(line: str, fragment: str) -> None:
    assert not sources(line).contains(fragment)


# The worked table in spec 100: what each line still verifies, and what not.
@pytest.mark.parametrize(
    ("line", "fragment", "verifies"),
    [
        ("Owned all services except billing.", "Owned all services", True),
        ("Moved to Kafka 3 without downtime.", "Moved to Kafka 3", True),
        ("Moved to Kafka 3 without downtime.", "downtime", False),
        ("Shipped the API (not the mobile app) and led the team.", "led the team", False),
        ("Led the team (I did not).", "Led the team", False),
        ("Shipped the API. Did not own the mobile app.", "Shipped the API", True),
        ("Shipped the API. Did not own the mobile app.", "own the mobile app", False),
        ("Did not own the mobile app. Shipped the API.", "Shipped the API", False),
        ("Claims that I led the team are not true.", "led the team", False),
        ("Built a no-code editor.", "no-code editor", True),
        ("Shipped the API; never owned the mobile app.", "Shipped the API", True),
    ],
)
def test_a_denial_reaches_from_its_start_to_the_end_of_the_line(
    line: str, fragment: str, verifies: bool
) -> None:
    assert sources(line).contains(fragment) is verifies


@pytest.mark.parametrize(
    "line",
    [
        "I did `not` own the mobile app.",
        "I did **not** own the mobile app.",
        "I did  not own the mobile app.",
        "I did\tnot own the mobile app.",
        "I did\u00a0not own the mobile app.",
    ],
)
def test_markup_and_spacing_do_not_hide_a_marker(line: str) -> None:
    assert not sources(line).contains("own the mobile app")


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("Built a no-code editor for the sales team.", "for the sales team"),
        ("Wrote a notable migration guide.", "migration guide"),
        ("Tied a knot in the release train.", "the release train"),
        ("Fixed nothing-to-commit errors in CI.", "errors in CI"),
        ("Ran a not-for-profit hackathon.", "hackathon"),
        ("Joined a nonprofit board.", "board"),
        ("Kept the exception budget under review.", "under review"),
    ],
)
def test_a_marker_matches_whole_words_only(line: str, fragment: str) -> None:
    assert sources(line).contains(fragment)


@pytest.mark.parametrize(
    "line",
    [
        "I did not use e.g. Kafka in production.",
        "I did not work in the U.S. Kafka team in production.",
        "I did not work with J. Doe on Kafka in production.",
    ],
)
def test_an_abbreviation_does_not_end_a_negation(line: str) -> None:
    """A period inside an abbreviation is not a sentence boundary, and even
    if it were read as one, a denial runs to the end of the line."""
    assert not sources(line).contains("Kafka in production")


def test_an_early_boundary_cannot_admit_the_subject_of_a_denial() -> None:
    assert not sources("The claim that I led the U.S. team is not true.").contains(
        "I led the U.S. team"
    )


def test_lines_containing_returns_only_the_asserting_text() -> None:
    """A check reading the line a fragment was cut from must not read a
    number, a version or a skill from its denied part (spec 100)."""
    line = "Used `Golang` daily. Never used Go 1.22 in production."
    assert sources(line).lines_containing("Used Golang") == ["Used Golang daily."]
    assert sources(line).lines_containing("Go 1.22") == []


# --- case: a real claim is not dropped over capitalisation ------------------


def test_matching_is_case_insensitive() -> None:
    """Case sensitivity dropped real evidence silently, which under the old
    omit-on-failure behaviour meant a shorter resume and no error."""
    assert sources("Led the Design System rewrite.").contains("led the design system rewrite")


def test_a_trailing_period_does_not_change_the_answer() -> None:
    assert sources("Led the design system rewrite.").contains("Led the design system rewrite.")


# --- markup: a plain quote of a formatted line is the same claim (spec 068) --

MARKED = "- Generates TypeScript types via `openapi-typescript` for **Nuxt 4**"


def test_backticks_in_the_truth_line_do_not_block_a_plain_quote() -> None:
    """The observed refusal: a verbatim quote, minus the backticks."""
    assert sources(MARKED).contains("Generates TypeScript types via openapi-typescript")


def test_emphasis_in_the_truth_line_does_not_block_a_plain_quote() -> None:
    assert sources(MARKED).contains("generates typescript types via openapi-typescript for Nuxt 4")
    assert sources("- Shipped *every* week").contains("Shipped every week")


def test_a_quote_with_markers_still_verifies() -> None:
    assert sources(MARKED).contains(
        "Generates TypeScript types via `openapi-typescript` for **Nuxt 4**"
    )


def test_marker_removal_does_not_verify_a_different_claim() -> None:
    assert not sources(MARKED).contains("Generates TypeScript types via openapi for Nuxt 4")


def test_a_fragment_of_only_markers_verifies_nothing() -> None:
    assert not sources(MARKED).contains("`` ** ``")


def test_markers_do_not_revive_a_disclaimer_line() -> None:
    truth = "## Must not claim\n\n- Led the **GraphQL** migration\n"
    assert not sources(truth).contains("Led the GraphQL migration")


def test_a_literal_asterisk_is_text() -> None:
    truth = "- Linted every file matching src/**/*.ts"
    assert sources(truth).contains("every file matching src/**/*.ts")
    assert not sources(truth).contains("every file matching src//.ts")


@pytest.mark.parametrize(
    ("text", "reduced"),
    [
        ("**Nuxt 4**", "Nuxt 4"),
        ("*shipped* weekly", "shipped weekly"),
        ("src/**/*.ts", "src/**/*.ts"),
        ("5 * 3 * 2", "5 * 3 * 2"),
        ("***both***", "***both***"),
        ("`$ref`s and `openapi-typescript`", "$refs and openapi-typescript"),
    ],
)
def test_the_asterisk_examples_reduce_as_the_spec_says(text: str, reduced: str) -> None:
    assert strip_inline_markup(text) == reduced


def test_lines_containing_returns_the_raw_line_for_a_plain_quote() -> None:
    """Only the comparison ignores markup. The rate check reads the line as
    written."""
    assert sources(MARKED).lines_containing("via openapi-typescript") == [MARKED]


# --- an empty or unusable truth document verifies nothing -------------------


def test_an_empty_truth_document_verifies_nothing() -> None:
    """The case that produced a clean PDF with empty sections."""
    assert not sources("").contains("anything at all")


def test_a_document_of_only_disclaimers_verifies_nothing() -> None:
    assert not sources("## Must not claim\n\nEverything below.\n").contains("Everything below")


def test_an_empty_fragment_never_verifies() -> None:
    """Otherwise a blank generated line would pass the gate trivially."""
    assert not sources("Led the design system rewrite.").contains("   ")


# --- forbidden phrases are enforced -----------------------------------------


def test_a_forbidden_phrase_is_found_in_generated_text() -> None:
    """This list was parsed, stored, exported, and read by no validator. It is
    the candidate's own record of claims never to make, which makes it the
    highest-value check available and exactly the invention class a
    containment predicate cannot catch."""
    hits = forbidden_hits(("world-class expert", "10x engineer"), "A world-class expert in React.")
    assert hits == ["world-class expert"]


def test_forbidden_matching_is_case_insensitive() -> None:
    assert forbidden_hits(("10x engineer",), "A 10X Engineer joins the team") == ["10x engineer"]


def test_clean_text_has_no_forbidden_hits() -> None:
    assert forbidden_hits(("world-class expert",), "Built the design system.") == []


def test_a_blank_forbidden_entry_matches_nothing() -> None:
    """A stray empty line in the candidate's list would otherwise match every
    document and refuse every artifact."""
    assert forbidden_hits(("", "   "), "anything") == []
