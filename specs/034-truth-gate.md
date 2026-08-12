---
spec: 034
title: The truth gate refuses rather than omits, and the README stops overclaiming
status: accepted
approved: yes
milestone: M6
depends: [013, 014]
---

# Spec 034: The truth gate refuses rather than omits, and the README stops overclaiming

## Problem

The honesty invariant is the one promise this project makes about a document
that goes to a real employer under a real name. It does not hold.

**The check is substring containment.** Acceptance reduces to
`str.__contains__` minus trailing periods: case-sensitive, section-blind,
polarity-blind. Because it cannot see structure or negation, a carefully
written truth document defeats itself. A section headed "claims I must not
make" validates every claim listed under it, and a sentence saying a
responsibility was not held validates the substring naming that
responsibility.

**Failure is silent omission.** An unverifiable line is dropped and the
document renders. Nothing checks that a section retained any bullets, so an
empty or drifted truth document yields a resume with empty achievements and
experience sections, a passing one-page check, and a status advance to
`tailored_cv_requested`. The strictest possible truth failure produces a
clean PDF.

**Letters and answers have no gate at all.** Nothing in the apply package
calls the containment check. Spec 014 is honest about this; the README is
not.

**The README states guarantees the code does not provide**, in a public
repository: that every generated line is checked against a source-of-truth
document, that a PDF is refused when a claim cannot be traced, and that
cover letters and answers work the same way. All three are false as written.

Two supporting defects: the cover letter is the one recruiter-facing artifact
with neither a PDF validation nor phrase scrubbing on its header, and
`normalize_visible_role_title`, named in the parity matrix as the proof of
the internal-label-scrubbing invariant, has no production caller. Likewise
`require_truth` and `forbidden_phrases` are parsed, exported, tested, and
called by nothing, and `forbidden_phrases` is the candidate's own list of
claims never to make, which is exactly the invention class containment cannot
catch.

Found by the `principal-review` board (spec 028), honesty lens.

## Scope

**Refusal, not omission.** An unverifiable line fails the artifact. The
operator sees which line and why. Silently shipping a shorter document is the
failure mode that lets an empty truth document through.

**A predicate that sees structure and polarity.** Verification is scoped to
the sections of the truth document that assert facts, negated statements do
not verify their own substrings, and matching is normalized rather than
case-sensitive.

**Non-empty sections are an invariant.** A rendered artifact with an empty
achievements or experience section is refused, whatever the cause.

**`forbidden_phrases` is enforced.** It is read by a validator that fails the
artifact, on every generated surface. The candidate's own list of claims
never to make is the highest-value check available and is currently inert.

**The letter gets the same gates as the resume.** PDF validation, phrase
scrubbing on every visible field including the header, and title
normalization actually called.

**No invented numbers on generated output.** Every AI outreach variant is
labelled with a hardcoded confidence score and prints it, which is a number
with no computation behind it presented as a measurement. And the headline
promotion counts role labels rather than evidence, so two roles carrying a
word promote a claim with no supporting bullets. Both are inventions the
truth gate is supposed to prevent, arriving through a path it does not watch.

**The README says what is true.** Rewritten to describe the gate that exists,
per surface, with the limitation stated where a surface has none.

## Inputs, outputs, failure modes

- Inputs: the generated lines, the truth document, and the forbidden phrase
  list.
- Outputs: an artifact, or a refusal naming the offending line and the rule.
- The adversarial cases that must fail verification:

  | Construction | Must |
  |---|---|
  | a claim listed under a "must not claim" heading | not verify |
  | a claim whose truth-document sentence is negated | not verify |
  | a claim matching only by case difference | verify |
  | any line matching a forbidden phrase | refuse the artifact |
  | a truth document that is empty or unreadable | refuse, not render empty |

- Failure mode this must not introduce: a refusal so brittle that ordinary
  paraphrase fails. The gate is about invention, not wording, so the spec
  accepts a predicate that is stricter than containment without demanding
  exact quotation.
- Failure mode this must not introduce: a test suite that builds the truth
  document from the same bullet pool it validates, which is what the current
  resume tests do and why the containment check cannot fail in any of them
  but one deliberate case.

## Acceptance criteria

Proven by services/api/tests/test_honesty.py and
services/api/tests/test_resume.py:

| Criterion | Proof |
|---|---|
| a disclaimer section does not verify its own list | `test_a_claim_under_a_must_not_claim_heading_does_not_verify`, `test_every_disclaimer_heading_shape_is_recognised`, `test_a_disclaimer_section_ends_at_the_next_heading` |
| a negated sentence does not verify the claim it denies | `test_a_negated_sentence_does_not_verify_the_claim_it_denies`, parametrized over six shapes |
| a case difference still verifies | `test_matching_is_case_insensitive` |
| an unverifiable line refuses the artifact | `test_bullet_failing_truth_check_refuses_rather_than_omitting` |
| an empty truth document verifies nothing | `test_an_empty_truth_document_verifies_nothing`, `test_a_document_of_only_disclaimers_verifies_nothing` |
| an empty required section is refused | `test_an_empty_required_section_refuses_the_rendered_resume` |
| forbidden phrases refuse the artifact | `test_a_forbidden_phrase_refuses_the_rendered_resume`, `test_a_clean_resume_reports_no_forbidden_phrases` |
| the letter validates its PDF and scrubs its header | `test_write_cover_letter_artifacts_fails_when_pdf_not_created`, and the header now passes through `normalize_visible_role_title` and `strip_banned_phrases` |
| no candidate content in a fixture | the bundle and truth documents used by these tests are the synthetic ones already committed (ADR-008) |

Two scope corrections, both recorded rather than absorbed.

The letter does **not** get the line-by-line truth check. A letter is prose
rather than a list of claims, and verifying it sentence by sentence would
either reject ordinary paraphrase or verify nothing. It gets the PDF
validation and the phrase scrubbing the resume has, and the README now states
the limitation instead of claiming all three surfaces work the same way.
Application answers are in the same position.

`require_truth` and `normalize_visible_role_title` were named in the spec as
dead symbols to revive. The title normalizer now has a production caller on
the letter header. `require_truth` still has none: it is a thin wrapper over
the predicate that every caller reaches directly, so reviving it would mean
inventing a caller to satisfy a criterion. Left in place and named here
instead, which is the honest outcome.

One mutation escaped the first version of this suite and is worth recording,
because it is the same mistake twice in one session: the forbidden-phrase
tests exercised `forbidden_hits` directly, so removing the call from
`validate_rendered_markdown` changed nothing. Now tested against the
validator.

- [x] every row of the table above has a test
- [x] an unverifiable line refuses the artifact and names the line
- [x] an empty or unreadable truth document produces no PDF and no status
      advance
- [x] a rendered artifact with an empty required section is refused
- [x] `forbidden_phrases` refuses an artifact on the resume, the letter, and
      the answers path
- [x] the letter validates its PDF and scrubs its header
- [x] `normalize_visible_role_title` has a production caller, asserted by a
      test rather than by inspection
- [x] no generated surface prints a score that was not computed
- [x] the headline promotion requires supporting bullets, not label counts
- [x] at least one test builds its truth document independently of the bullet
      pool, so the predicate can fail
- [x] every honesty claim in README.md names the file or test that proves it,
      and every surface without a gate says so
- [x] no candidate content enters a committed fixture; the examples are
      synthetic and double as the test data (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The containment predicate, the
omission path, the absent apply-side caller, and the three README sentences
are verifiable in the tree.

## Out of scope

Changing how content is generated or which model produces it. Semantic or
model-based verification: this spec makes the existing invariant real, not
cleverer.
