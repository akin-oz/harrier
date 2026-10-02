---
spec: 068
title: Evidence matching ignores inline markdown code and emphasis markers
status: draft
approved: yes
milestone: M8
depends: [034, 065]
---

# Spec 068: Evidence matching ignores inline markdown code and emphasis markers

## Problem

The truth document is markdown. Its lines use inline code and emphasis:
`` `openapi-typescript` ``, `` `$ref`s ``, `**Nuxt 4**`. Generated text is
plain prose, so a model quoting a truth line verbatim drops those markers.

`TruthSources.contains` and `TruthSources.lines_containing`
(services/api/src/harrier/resume/content.py) compare lowercased substrings
and nothing else (spec 034: structure, polarity, case). A marker on one side
and not the other fails the match.

Observed on a real cover letter: the model cited a truth line word for word,
minus two pairs of backticks. Rule C2 of spec 065 refused the letter with
`unverified evidence`. Reproduced against the live truth sources: the
fragment as cited returns `False`; the same fragment with the backticks
restored returns `True`.

Who it hurts: the operator. A letter or answer grounded in real evidence is
refused, and the error reads as if the model invented a claim. The only
workaround today is to strip formatting from the truth document by hand.

The same predicate verifies resume bullets (services/api/src/harrier/resume/markdown.py,
services/api/src/harrier/resume/ai.py), so a resume bullet quoted without
markers is refused the same way.

The letter and answer checks of spec 065 have the same shape in a second
place. `_norm` (services/api/src/harrier/apply/claims.py) compares a claim
sentence to the output (C1) and employer evidence to the job posting (C3),
casefolded with whitespace collapsed and nothing else. Postings are often
markdown (`**Requirements**`, `` `Kubernetes` ``), so a model quoting a
posting line without its markers is refused with `employer evidence not in
posting`. Not yet observed; included because it is the same defect.

This is a gap, not a regression. Spec 034 never stated how formatting is
compared, so it needs a spec rather than a bug fix.

## Scope

One change, applied in two comparison functions: inline code and paired
emphasis markers are formatting, not text.

- `TruthSources.contains` and `TruthSources.lines_containing`
  (services/api/src/harrier/resume/content.py). Every caller inherits it:
  rule C2 and the skill check of spec 065, and resume bullet verification.
- `_norm` in services/api/src/harrier/apply/claims.py. Every caller inherits
  it: C1 (claim sentence in output), C3 (employer evidence in posting),
  `ClaimContext.evidence_lines` for employer claims, C4, and the sentence
  match in C9.

Nothing else about verification changes.

## Behavior

Before comparing, both sides (fragment and supporting line; claim sentence
and output; employer fragment and posting) are reduced to their text by one
shared function:

| Marker | Rule |
|---|---|
| `` ` `` | every backtick is removed, any run length |
| `**text**` | removed when paired: see below |
| `*text*` | removed when paired: see below |
| any other `*` | kept |
| `_` | kept (see Failure modes) |

An asterisk pair is an opening run of one or two `*` and a closing run of
the same length, on the same line, where:

- the opening run is not preceded by `*` and is followed by a character that
  is neither whitespace nor `*`;
- the closing run is preceded by a character that is neither whitespace nor
  `*`, and is not followed by `*`.

Both runs are removed and the text between them is kept. Pairs are matched
left to right, shortest first.

After removal, whitespace is collapsed to single spaces, then each existing
comparison runs unchanged: for `TruthSources`, lowercase, one trailing period
dropped, substring of a supporting line; for `_norm`, casefold, curly
apostrophes straightened, one trailing period dropped.

Asterisk examples:

| Input | Reduced |
|---|---|
| `**Nuxt 4**` | `Nuxt 4` |
| `*shipped* weekly` | `shipped weekly` |
| `src/**/*.ts` | `src/**/*.ts` (no pair) |
| `5 * 3 * 2` | `5 * 3 * 2` (runs touch whitespace) |
| `***both***` | `***both***` (run of three is not a pair) |

What does not change:

- Which lines are supporting lines. Disclaimer sections and negated lines
  (spec 034) are filtered on the raw line, before any marker removal, exactly
  as today.
- `lines_containing` and `evidence_lines` still return the raw lines,
  markers included. Only the comparison is normalized.
- Violation messages quote the text as the model wrote it, not the reduced
  form.
- A fragment that is empty after removal verifies nothing, the same as an
  empty fragment today.

Examples, with truth line
``- Generates TypeScript types via `openapi-typescript` for **Nuxt 4**``:

| Fragment | Today | After |
|---|---|---|
| `Generates TypeScript types via openapi-typescript for Nuxt 4` | False | True |
| ``Generates TypeScript types via `openapi-typescript` for **Nuxt 4**`` | True | True |
| `generates typescript types via openapi-typescript` | False | True |
| `Generates TypeScript types via openapi for Nuxt 4` | False | False |

## Failure modes

- **Two words joined by a removed backtick.** ``a`b`` becomes `ab`. Backticks
  sit next to spaces in practice, and both sides lose the same character,
  so both still match. A fragment only gains a match by spelling the joined
  word, which the source does contain.
- **A literal asterisk.** Globs, multiplication, footnote marks and bullets
  written as `* item` are not pairs and stay, on both sides. A quote that
  drops a literal asterisk still fails, as today.
- **Unbalanced emphasis.** `**open` with no closing run is left as is. The
  fragment must carry it, as today.
- **Underscore is kept.** Identifiers such as `snake_case` and `__init__`
  appear in technical truth lines. Removing `_` would merge them on both
  sides and still match, but `_emphasis_` is rare in the truth document and
  not worth the change. A truth line using `_x_` emphasis keeps today's
  behavior: the fragment must carry the underscores.
- **Negation hidden by markers.** `` did `not` own `` is not caught by the
  negation filter today, because the filter looks for ` did not ` on the raw
  line. This spec does not change that: the filter still runs on the raw
  line. Recorded under Out of scope rather than fixed here.
- **Markdown links.** `[text](url)` is left alone. A fragment quoting `text`
  still matches as a substring; a fragment quoting the URL does too.
  Nothing new is accepted or refused.
- **Empty truth document.** Unchanged: verifies nothing.

## Acceptance criteria

Proof lives in services/api/tests/test_honesty.py unless named otherwise.
Every truth line and posting in these tests is synthetic.

| Criterion | Proof |
|---|---|
| a fragment without backticks verifies a truth line that has them | `test_backticks_in_the_truth_line_do_not_block_a_plain_quote` |
| a fragment without asterisks verifies a truth line with `**bold**` or `*italic*` | `test_emphasis_in_the_truth_line_does_not_block_a_plain_quote` |
| a fragment that keeps the markers still verifies | `test_a_quote_with_markers_still_verifies` |
| removing markers does not make an absent claim verify | `test_marker_removal_does_not_verify_a_different_claim` |
| a fragment made only of markers verifies nothing | `test_a_fragment_of_only_markers_verifies_nothing` |
| a disclaimer line carrying markers still does not verify | `test_markers_do_not_revive_a_disclaimer_line` |
| a literal asterisk is kept, on both sides | `test_a_literal_asterisk_is_text` |
| every row of the asterisk examples table reduces as shown | `test_the_asterisk_examples_reduce_as_the_spec_says` |
| `lines_containing` finds the line for a plain quote and returns it raw | `test_lines_containing_returns_the_raw_line_for_a_plain_quote` |
| C2: the observed cover letter shape passes | `services/api/tests/test_apply_claims.py::test_a_claim_quoting_a_backticked_truth_line_without_backticks_passes` |
| C3: employer evidence quoted without markers passes | `services/api/tests/test_apply_claims.py::test_employer_evidence_quoted_without_markers_passes` |
| C3: evidence absent after marker removal is still refused | `services/api/tests/test_apply_claims.py::test_employer_evidence_absent_after_marker_removal_is_still_refused` |
| C1: a plain claim sentence matches output with markers | `services/api/tests/test_apply_claims.py::test_a_plain_claim_sentence_matches_output_that_carries_markers` |
| C1: a marked claim sentence matches plain output | `services/api/tests/test_apply_claims.py::test_a_marked_claim_sentence_matches_plain_output` |

Ten of these fail with the change reverted: every test where a match is
newly accepted, plus the three asterisk rows that change. The rest are
guards that pass before and after by design: they pin what must not loosen.

The observed fragment was also rerun read-only against the live truth
sources with the new `content.py`: it now verifies, and `lint-enforced
boundaries` (the C9 case from the same refusal) still does not.

- [x] every criterion that accepts something new has a test that fails
      without the change
- [x] spec 034's existing tests pass unchanged
- [x] spec 065's existing tests pass unchanged
- [x] no real truth-document content in any test
- [x] `uv run ruff check` and `uv run pyright` clean
- [ ] All gates green on PR

## Proof / origin

Observed on 2026-10-02: a cover letter was refused with `unverified
evidence` for a fragment present in the truth document except for two pairs
of backticks. Reproduced read-only against the live truth sources inside the
container: `TruthSources.contains` returned `False` for the fragment as
cited and `True` with the backticks restored. The comparison that causes it
is `fragment.strip().rstrip(".").lower()` as a substring of `line.lower()`
in `TruthSources.contains` and `TruthSources.lines_containing`
(services/api/src/harrier/resume/content.py).

## Out of scope

- **Underscore emphasis**, bold-italic `***x***`, markdown links, HTML tags,
  and escaped characters.
- **C5 and C6 number token rules.** They tokenize the raw text and use
  neither comparison. Unchanged. (C6 reads `lines_containing`, which now
  finds more lines, but the rate rule itself does not change.)
- **Negation hidden by inline markers.** A separate weakness of the spec 034
  polarity filter. Its own spec if wanted.
- **Changing the truth document.** No migration or rewrite of stored profile
  documents.
- **Removing markers from generated output.** Whether a resume renders
  backticks is the renderer's concern.
- **Paraphrase or fuzzy matching.** The comparison stays exact substring
  after normalization.

## Migration

None. Stored documents are untouched. Fragments that verified before still
verify; some that were refused now verify.
