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


# --- case: a real claim is not dropped over capitalisation ------------------


def test_matching_is_case_insensitive() -> None:
    """Case sensitivity dropped real evidence silently, which under the old
    omit-on-failure behaviour meant a shorter resume and no error."""
    assert sources("Led the Design System rewrite.").contains("led the design system rewrite")


def test_a_trailing_period_does_not_change_the_answer() -> None:
    assert sources("Led the design system rewrite.").contains("Led the design system rewrite.")


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
