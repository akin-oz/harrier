---
spec: 100
title: The truth gate reads every negation it is given
status: proposed
approved: no
milestone: M9
depends: [034, 065, 068, 071, 087]
---

# Spec 100: The truth gate reads every negation it is given

## Problem

Spec 034 made the truth gate polarity-aware: "I did not own the incident
response rota" must not verify "own the incident response rota". The rule
it built is a list of ten phrases, `NEGATIONS` in
`services/api/src/harrier/resume/content.py`, matched by `_is_negated` as
substrings of the line padded with one space each side.

A truth document is written in plain English, and plain English negates in
many more ways than ten. Each of these synthetic lines is read today as an
assertion, so the fragment beside it verifies:

| Truth line | Fragment that verifies today |
|---|---|
| I didn't lead the team. | lead the team |
| Not responsible for hiring. | responsible for hiring |
| I wasn't responsible for hiring. | responsible for hiring |
| I haven't run Kafka in production. | run Kafka in production |
| I can't claim ownership of the budget. | ownership of the budget |
| I don't manage people. | manage people |
| I had no direct reports. | direct reports |
| None of the hiring was mine. | the hiring was mine |
| Neither led the team nor owned the budget. | owned the budget |
| Owned all services except billing. | billing |
| Shipped the API (not the mobile app). | the mobile app |
| Shipped the API,never owned the mobile app. | owned the mobile app |
| I did `not` own the mobile app. | own the mobile app |
| I did not own the mobile app, with a non-breaking space between "did" and "not". | own the mobile app |

These were run against the real functions with synthetic strings during
drafting. The gate does not read the negation the document gives it.

The opposite failure exists too. One marker anywhere drops the whole line.
"Shipped the API. Did not own the mobile app." no longer verifies "Shipped
the API". "Moved to Kafka 3 without downtime." grounds nothing (spec 087
records this as current behavior). A true half is lost, so a resume comes
out shorter or a real claim is refused.

Every check reaches the gate through `TruthSources._supporting`, so every
check inherits both failures:

- Resume bullets: `resolve_bullets` (`services/api/src/harrier/resume/markdown.py`)
  and the AI bullet ids (`services/api/src/harrier/resume/ai.py`).
- Cover letter and answer evidence (C2), the unverified skill rule (C8) and
  the rate context for numbers (C6): `services/api/src/harrier/apply/claims.py`.
- Brief confirmed skills: `services/api/src/harrier/apply/brief.py`.

The harm is one-sided in the worst direction for the first failure: a claim
the candidate's own truth document denies can reach a recruiter or a
committee under the candidate's name. Spec 101 puts academic documents
through this gate, and a committee reads them closely.

There is one negation list in the codebase. No other module carries a
claim-polarity list, so this spec has one place to change.

## Scope

- What counts as a negation marker.
- Where a marker is looked for: the comparable text, not the raw line.
- How far a marker reaches: a negated span, not the whole line.
- Which fragment occurrences verify: those that overlap no negated span.

The disclaimer headings (`DISCLAIMER_HEADINGS`, `asserting_lines`) are
unchanged.

## Behavior

### Where markers are read

Markers are found in the same text fragments are compared against: the
line after `strip_inline_markup` (spec 068), lowercased, with every run of
whitespace, including a non-breaking space and a tab, read as one space,
and with a typographic apostrophe (U+2019) read as `'`. So `did \`not\``,
`did **not**`, a double space and a non-breaking space no longer hide a
marker.

A marker matches as whole words. A word boundary is the start or end of
the text, whitespace, or punctuation other than a hyphen or an apostrophe.
So `,never` and `(not` match, and `no-code`, `nothing`, `knot`, `cannon`
and `notable` do not.

### The markers

Two classes.

**Sentence negators.** The sentence they sit in does not assert anything.

- `not`, `no`, `never`, `none`, `neither`, `nor`, `no longer`, `cannot`
- every word ending in `n't` (`didn't`, `wasn't`, `isn't`, `can't`,
  `won't`, `haven't`, `hadn't`, `don't`, `doesn't`, `aren't` and the rest)
- `failed to`, `lacked`

**Exclusion markers.** What comes after them is denied; what comes before
them is asserted.

- `without`, `except`, `excluding`, `other than`, `apart from`,
  `rather than`, `instead of`

The ten phrases in today's `NEGATIONS` are all covered: `did not`,
`have not`, `has not`, `was not`, `were not` through `not`, `never` and
`no longer` as sentence negators, and `rather than`, `instead of`,
`without` as exclusion markers.

### How far a marker reaches

Each comparable line is divided into negated spans and asserting text.

- **A sentence negator** negates from the start of its sentence to the end
  of the line.
- **An exclusion marker** negates from the marker to the end of the line.
- **Parentheses do not end a span.** "Led the team (I did not)" denies the
  text before the parenthesis, so a marker inside parentheses reaches as
  far as one outside them.

A sentence starts at the start of the line or after a sentence boundary: a
`.`, `!`, `?` or `;` followed by whitespace. A period is not a boundary
when the token it ends is a single letter or already holds a period
(`U.S.`, `e.g.`, `i.e.`).

The span runs to the end of the line, not the end of the sentence, on
purpose. A boundary found in the wrong place would otherwise end a
negation too soon and let a denied claim through. With spans ending at the
line, a wrong boundary can only move where a span starts. Only text before
the span is recovered: earlier sentences, or the head of an exclusion.

### Which occurrences verify

`contains(fragment)` is true when the comparable fragment occurs in some
supporting line at a position that overlaps no negated span.
`lines_containing(fragment)` returns the raw lines that have such an
occurrence. A line with a negated span is no longer dropped as a whole:
its asserting text still verifies.

Worked results, each a test row:

| Truth line | Fragment | Verifies |
|---|---|---|
| I didn't lead the team. | lead the team | no |
| Not responsible for hiring. | responsible for hiring | no |
| I had no direct reports. | direct reports | no |
| Neither led the team nor owned the budget. | owned the budget | no |
| Owned all services except billing. | billing | no |
| Owned all services except billing. | Owned all services | yes |
| Moved to Kafka 3 without downtime. | Moved to Kafka 3 | yes |
| Shipped the API (not the mobile app) and led the team. | the mobile app | no |
| Shipped the API (not the mobile app) and led the team. | led the team | no |
| Led the team (I did not). | Led the team | no |
| Shipped the API. Did not own the mobile app. | Shipped the API | yes |
| Shipped the API. Did not own the mobile app. | own the mobile app | no |
| Did not own the mobile app. Shipped the API. | Shipped the API | no |
| Claims that I led the team are not true. | led the team | no |
| I did not use e.g. Kafka in production. | Kafka in production | no |
| Built a no-code editor. | no-code editor | yes |

The last but one row is why an early boundary cannot end a negation: the
span from "did not" runs past "e.g." to the end of the line.

## Failure modes

- **A true line that uses a negator for emphasis** ("Never missed a
  release", "Not only led the team but owned the budget"). It verifies
  nothing, as today for "never". The loss is a shorter resume or a refused
  claim, which the operator sees by name (spec 034: refusal, not omission).
  The fix is to reword the truth line.
- **A negative claim quoted from a negative truth line** ("never missed a
  release" against "Never missed a release"). Refused, as today. Verifying
  a negative claim is out of scope.
- **"No" as a numeral label** ("No. 1 in the region"). Read as a negator.
  The line verifies nothing after it; same direction of loss.
- **A truth line with a negation in an earlier sentence and the claim in a
  later one.** The later sentence does not verify, because the span runs to
  the end of the line. The operator puts the claim on its own line.
- **An exclusion marker in a sentence that is also negated.** The sentence
  negator's span starts earlier and wins. Nothing before it is recovered.
- **An empty line or a line of only markers.** No asserting text, nothing
  verifies, no error.
- **A truth document that relied on the old rule.** A claim that verified
  through one of the missed negations now fails, and the artifact command
  refuses naming the claim (spec 034). That claim was being verified by a
  sentence that denied it. A line that was dropped whole now partly
  verifies, which can make a resume longer.

## Acceptance criteria

- [ ] Every row of the Problem table is refused, and every row of the
      worked table gives its stated result
      (planned test_every_negation_shape_is_read).
- [ ] The six existing polarity cases in
      `services/api/tests/test_honesty.py::test_a_negated_sentence_does_not_verify_the_claim_it_denies`
      and `test_a_plain_assertion_still_verifies` pass unchanged.
- [ ] Every disclaimer heading test in `services/api/tests/test_honesty.py`
      passes unchanged.
- [ ] A marker hidden by inline markup, a double space, a tab or a
      non-breaking space is read (planned test_markup_and_spacing_do_not_hide_a_marker).
- [ ] A marker inside a hyphenated word or a longer word is not read
      (planned test_a_marker_matches_whole_words_only).
- [ ] An early sentence boundary never shortens a negated span: a false
      split inside a negated sentence leaves everything after the marker
      negated (planned test_an_abbreviation_does_not_end_a_negation).
- [ ] C2 refuses a letter quoting "lead the team" against a truth that says
      "I didn't lead the team", and C8 refuses a skill whose only truth line
      is "I haven't used Rust"
      (planned test_letter_claims_read_contracted_negations).
- [ ] A resume bullet whose only truth line is negated in a form the old
      list missed is refused with today's refusal message
      (planned test_a_bullet_denied_by_a_contraction_is_refused).
- [ ] `NEGATIONS` and `_is_negated` are replaced by the two marker lists
      and the span reader; no other module gains a negation list.
- [ ] Spec 087's sentence on "Haven't run Kafka 3 in production" and
      "Moved to Kafka 3 without downtime" is amended to the new results.
- [ ] `uv run ruff check`, `uv run pyright` and `just check` pass.

## Honest limitations

- **This is a word list, not a parser.** English can deny a claim without
  any listed word ("I left before the launch", "the team was led by
  someone else"). Those lines still verify their substrings. The gate
  reads negation words, not meaning.
- **It loses true text to stay safe.** Running every span to the end of the
  line, and reading "no" and "not only" as negators, drops some true
  claims. That is the deliberate direction: a lost claim is visible as a
  refusal, an admitted false claim is not visible at all.
- **Text before an exclusion marker is trusted.** "Owned all services
  except billing" verifies "Owned all services", although the line as a
  whole says the ownership had an exception. A fragment that quotes only
  the head drops the exception. The claims check (C2) reads the fragment,
  not the sentence it came from.

## Migration

None for data. After it ships, the operator runs `tailor`, `cover-letter`
and `answers` on one job each. A refusal names a claim that a truth line
was denying. Reword the truth line, or accept that the claim goes.

## Options weighed

- **Add the missing phrases to the one list.** Smallest change, but it
  keeps the space-padded substring rule, so punctuation and markup still
  hide a marker, and it keeps dropping whole lines.
- **Negation scoped to the sentence only.** Recovers more true text, but a
  wrong sentence boundary ends a negation early and admits a denied claim.
  Declined for that reason.
- **Ask a model whether a line denies the claim.** Catches the wording a
  list cannot, but puts a non-deterministic judgement in the one gate that
  must be repeatable and testable offline. Declined.

## Open decisions for Akin

1. **The head of an exclusion stays asserted** ("Moved to Kafka 3 without
   downtime" verifies "Moved to Kafka 3"). The alternative keeps today's
   rule and drops the whole line.
2. **`failed to` and `lacked`** are sentence negators. They are the two
   non-grammatical words on the list; drop them if they cost more true
   lines than they catch.
3. **`no`** is a sentence negator, with the numeral-label loss noted
   above.

## Proof / origin

- The gate: `services/api/src/harrier/resume/content.py` (`NEGATIONS`,
  `_is_negated`, `asserting_lines`, `TruthSources`, `strip_inline_markup`).
- The polarity rule: spec 034. Markup ignored on both sides: spec 068.
  The Kafka examples: spec 087.
- Callers: `services/api/src/harrier/resume/markdown.py`,
  `services/api/src/harrier/resume/ai.py`,
  `services/api/src/harrier/apply/claims.py`,
  `services/api/src/harrier/apply/brief.py`.
- Existing tests: `services/api/tests/test_honesty.py`.
- The old repository had no polarity handling, only containment with a
  trailing period dropped: `~/job-hunt-local/scripts/tailor_resume.py`.
- The follow-on list this spec belongs to: spec 097, Out of scope.

## Out of scope

- Verifying a negative claim from a negative truth line.
- Disclaimer headings and section structure (spec 034, unchanged).
- How C6 reads a rate from joined text, and the claim path's per-unit
  clearing (spec 087 Out of scope).
- Negation in job descriptions, screening or scoring.
- Any language other than English.
- Academic documents (spec 101) and the resume document split (spec 098).
