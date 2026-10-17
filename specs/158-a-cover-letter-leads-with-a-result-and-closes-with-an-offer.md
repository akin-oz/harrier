---
spec: 158
title: A cover letter leads with a result, proves each cluster the posting asks for, and closes with an offer
status: proposed
approved: no
milestone: M9
depends: [034, 065, 066, 069, 071, 085, 101, 140]
---

# Spec 158: A cover letter leads with a result, proves each cluster the posting asks for, and closes with an offer

## Problem

The operator reads every generated letter before sending it, and on the
industry and focused kinds most of them need a rewrite. The letters pass
every check and are still weak in four ways.

- **They open by restating the posting.** `INDUSTRY_LETTER_VOICE`
  (`services/api/src/harrier/voices.py`) asks for "why this company and
  this role specifically" first. The model answers by paraphrasing the
  posting's own description of the company or the role. The focused voice
  opens with "designs taken to production" and drifts the same way.
- **The proof is thin.** The industry voice caps the letter at one
  sentence with numbers, one or two proof points and two named tools. A
  posting that asks for a stack, a tooling practice and a domain gets
  proof for one of them, or none.
- **They end soft.** The closing paragraph is "practical interest and next
  step". The model writes "Happy to talk" or "I'm interested in talking
  through". Nothing refuses it.
- **Logistics have no place.** The industry voice forbids location,
  eligibility and contracting unless notes ask for them. The paragraph
  count is fixed at three (`KindRules.letter_paragraphs`,
  `services/api/src/harrier/tracks.py`), so a separate logistics paragraph
  the operator could delete is refused even when asked for.

Three gaps sit underneath.

- **Some of the operator's facts cannot be cited.** Candidate evidence is
  the truth document and the achievements document only
  (`ClaimContext.sources`, `services/api/src/harrier/apply/claims.py`).
  The story bank (`story_bank` profile document) never reaches the
  letter payload. The candidate document reaches the payload, but a
  sentence citing it is refused as unverified. A contracting or
  eligibility sentence drawn from it cannot pass.
- **The operator never learns what the letter left out.** When the
  posting asks for something the truth sources cannot back, the model
  drops it. That is correct, and silent.
- **Some writing rules are prompt text only.** Spec 140 lists "the dash
  check" among the letter's writing rules, but no letter code calls
  `dash_marks` (`services/api/src/harrier/resume/dashes.py`). The shared
  letter prompt tells the model to use hyphens in place of em dashes,
  which invites a spaced hyphen as punctuation. Hedging, a closing
  question, a banned opener and an inflated year count are not checked
  on a letter at all.

## Scope

- `services/api/src/harrier/voices.py`: `INDUSTRY_LETTER_VOICE` and
  `FOCUSED_LETTER_VOICE` rewritten. The academic voice and every answers
  voice are unchanged.
- `services/api/src/harrier/apply/letters.py`: the shared prompt's
  formatting line, the payload, the response parsing, the evidence the
  letter's claims are checked against, the shape checks, and the markdown
  draft's two new sections.
- `services/api/src/harrier/apply/letterlint.py` (new): the letter linter.
- `services/api/src/harrier/tracks.py`: the letter shape fields of
  `KindRules` only. No track, track kind, slug or tracker behavior changes.
- Tests: `services/api/tests/test_letter_lint.py` (new) and
  `services/api/tests/test_letter_structure.py` (new). The pins in
  `services/api/tests/test_academic_documents.py` change on purpose: the
  shared letter block's hash and the industry letter prompt's hash. The
  focused letter tests in `services/api/tests/test_focused_documents.py`
  move to the new shape.

## Behavior

### The letter's shape, on the industry and focused kinds

`full_version` is plain-text blocks separated by blank lines, in this
order:

1. **Role line.** One line naming the role, the team when the posting
   names one, and the company. For example "Staff Engineer, Payments team,
   Example GmbH". No sentence-ending punctuation.
2. **Body, three to five paragraphs:**
   - **Hook.** Opens with the candidate's strongest quantified result
     that matches the posting's top responsibility. The first sentence
     carries the number.
   - **Proof.** One short paragraph per cluster the posting asks for, at
     most three clusters (for example the stack, the tooling or AI
     practice, the domain).
   - **Why this company.** One sentence with a specific, true basis in
     the posting. Omitted when there is none.
   - On the focused kind only: **why the smaller scope**, from the
     track's `scope_reason` as a stated preference (unchanged from spec
     140).
3. **Logistics.** Location, time zone, eligibility, contracting, each
   only as the evidence states it. Its own paragraph, so the operator can
   delete it whole. Optional on the industry kind, written when the
   posting or the notes raise location, eligibility or contracting.
   Required on the focused kind, where it replaces spec 140's contracting
   paragraph and keeps its `[[TODO: ...]]` rule for a missing value.
4. **Close.** One sentence, alone in its block: one concrete offer, for
   example a walkthrough of a named piece of work. Never a question. On
   the focused kind the offer names the demo or the repository from
   `candidate_links` (spec 140).

The letter is 150 to 220 words, role line included. The prompt asks for
that range. The linter refuses over 220; it does not refuse under 150.

The focused template still prints "Hello <Company> team," above the
blocks and the name below them. Neither is a block.

### The writing rules the prompt states

The industry and focused voices state:

- Short sentences. No hedging. The final sentence is the sharpest true
  one.
- Never open with the posting, and never restate the posting's text.
- No em dash, en dash, double hyphen or spaced hyphen as punctuation.
  A hyphen inside a compound word is fine.
- State experience only in the wording the truth sources give for it.
- Every candidate fact comes from the evidence below. A metric, tool,
  employer or experiment the evidence does not state is never written.
- Numbers are copied exactly as the evidence states them, including any
  `~` and any "in total".
- A legal entity is "a" or "an" entity of its type unless the evidence
  states it is registered.
- When the posting asks for something the evidence cannot back, leave it
  out of the letter and list it as a gap.

No personal fact appears in the prompt text. The facts reach the model
only through the payload.

The shared letter block's formatting line changes for every kind,
academic included, from "use hyphens instead" to: no em dash, en dash,
double hyphen or spaced hyphen as punctuation; use a comma, a colon or a
new sentence. This removes an instruction that contradicts spec 071.

### What the generator returns

The model's JSON gains one optional key on every kind:

```json
"gaps": [{"requirement": "string", "posting_evidence": "string"}]
```

`posting_evidence` is quoted verbatim from the posting. A gap whose
evidence is not in the posting is refused like employer evidence (spec
065). An absent key is an empty list, so the academic letter parses as
today.

`LetterDraft` gains:

- `gaps`: the parsed gaps.
- `facts_used`: one entry per candidate claim, pairing the claim's
  sentence with each evidence fragment and the name of the document it
  was found in: `resume_truth`, `achievements`, `story_bank`, `candidate`
  or `operator_evidence` (spec 066). Code finds the document; the model
  does not name it.

The markdown draft gains `## Gaps` and `## Facts used` sections after the
letter, before `## To verify`. Like every review section, they are never
in the HTML or the PDF. An empty gaps list prints "None.".

### The evidence a letter may cite

On every kind, a letter's candidate claim may cite, beside today's truth
and achievements documents and the brief's operator evidence:

- the `situation`, `task`, `action` and `result` strings of each story in
  the `story_bank` document. Its `jd_hook` and `opening_line` are prose the
  model once wrote and are never evidence.
- any string value in the `candidate` document.

The payload carries the story bank under `truth_sources.story_bank`. The
application profile stays positioning only, and citing it is refused as
today (spec 069). Answers keep today's evidence.

### The linter

`lint_letter(full_version, context) -> list[str]` in
`harrier.apply.letterlint` is deterministic: no model call, no network.
`context` holds the evidence text, the posting, the company, the role, the
kind and the record's professional years (`professional_experience_years`,
`services/api/src/harrier/resume/facts.py`). It returns one message per
violation, empty when the letter passes. It refuses:

- **L1 dash as punctuation:** any mark `dash_marks` finds (spec 071), in
  `full_version` and `short_version`.
- **L2 hedging:** any of hopefully, perhaps, possibly, somewhat, fairly,
  relatively, arguably, might, "I believe", "I think", "I feel", "happy
  to", "would love to", "I hope", on word boundaries, case-insensitive.
  "may" is not on the list: it is also a month.
- **L3 length:** over 220 words in `full_version`.
- **L4 banned opener:** the hook, the first block after the role line,
  begins with "I am writing", "I'm writing", "I am applying", "I'm
  applying", "I am excited", "I'm excited", the company name, the role
  title, "The role", "This role" or "Your team"; or its first sentence has
  no number.
- **L5 banned phrase anywhere:** "I am writing to apply", "excited",
  "let me know", "looking forward".
- **L6 closing:** the last block is more than one sentence, or contains a
  "?".
- **L7 restatement:** a run of eight or more consecutive words that also
  appears in the posting, compared case-insensitively after dropping
  punctuation.
- **L8 unbacked number:** a number token (`number_tokens`, spec 065) not
  found in the evidence text.
- **L9 experience:** a year count (`year_counts`, spec 071) larger than
  the record's professional years, or the word "decade" or "decades".
- **L10 entity:** "my" followed by a legal entity type (OÜ, GmbH, Ltd,
  LLC, BV, SAS, SRL, sp. z o.o.) unless an evidence line names that type
  with "registered" and without "not".
- **L11 shape:** the role line is missing (the first block is more than
  one line, more than 15 words, or ends in ".", "!" or "?"), or the block
  count is outside the kind's range.

`cover_letter_violations` calls `lint_letter` on the industry and focused
kinds and adds its messages to the refusal. A lint refusal is a
`ClaimCheckError`, so it gets the one retry spec 085 gives every refusal.
The academic kind is not linted.

### The kind's letter shape

`KindRules.letter_paragraphs` becomes `letter_blocks: tuple[int, int] |
None`, the inclusive range of blocks in `full_version`, role line and
close included:

| kind | blocks | words |
| --- | --- | --- |
| industry | 5 to 8 | at most 220 |
| focused | 6 to 8 | at most 220 |
| academic | no range | no cap (unchanged) |

Industry: role line, 3 to 5 body paragraphs, the close, and an optional
logistics block. Focused: role line, 3 to 5 body paragraphs, the required
logistics block, the close. The per-paragraph floor of 8 words
(`MIN_PARAGRAPH_WORDS`) no longer applies to the role line. A brief's
`letter.paragraphs` and `letter.max_words` (spec 066) still replace the
kind's range and cap.

## Failure modes

- **The posting asks for a skill the evidence lacks.** Not in the letter.
  Listed under `## Gaps` with the posting line. If the model writes it
  anyway, today's skill check (spec 065) refuses the letter.
- **The evidence has no quantified result.** The hook cannot pass L4. The
  letter is refused after its retry, and the command exits 1 naming L4.
  The operator adds a result to the truth sources or writes the letter by
  hand. Nothing invents a number.
- **The posting gives no basis for "why this company".** The sentence is
  omitted. The letter still needs three body paragraphs, from the hook and
  the proof clusters.
- **A story bank line states a number differently from the truth
  document** (for example a rounded total in one and an exact one in the
  other). Both are evidence, so L8 and spec 065 pass either. The operator
  keeps the two consistent. This spec does not reconcile them.
- **The story bank is absent or not JSON.** The payload carries an empty
  story bank and the letter is checked against the other documents. No
  error.
- **A brief states paragraph or word limits.** They replace the kind's
  range and cap, as today. L1, L2 and L4 to L10 still apply.
- **The retry is also refused.** Exit 1 with every violation, as today
  (spec 085). No draft is written for a refused letter, as today.
- **An earlier run's files for the same company and role.** Replaced as
  today (spec 067). This spec adds no versioned output path.

## Acceptance criteria

- [ ] A letter with each of L1 to L11 broken is refused with that rule's
  message, and the same letter with only that defect fixed passes the
  linter (planned test_letter_lint.py::test_each_rule_refuses_and_its_fix_passes).
- [ ] "12%" in a letter passes L8 when the evidence states "~12%", and
  "14%" is refused (planned test_letter_lint.py::test_a_number_must_be_in_the_evidence).
- [ ] A hyphen in "well-known" passes L1; " - " and "--" between words
  are refused (planned test_letter_lint.py::test_compound_hyphens_are_not_punctuation).
- [ ] "May 2024" passes L2 (planned test_letter_lint.py::test_may_the_month_is_not_a_hedge).
- [ ] "6+ years" is refused by L9 when the record holds 5, and "5+ years"
  passes (planned test_letter_lint.py::test_year_count_is_capped_by_the_record).
- [ ] "my GmbH" is refused when no evidence line states one is registered,
  and "a GmbH" passes (planned test_letter_lint.py::test_an_unregistered_entity_is_not_mine).
- [ ] An industry letter with a role line, three body paragraphs and a
  close passes the shape check; a three-block letter is refused naming the
  range (planned test_letter_structure.py::test_the_industry_letter_has_five_to_eight_blocks).
- [ ] A focused letter without a logistics block is refused (planned
  test_letter_structure.py::test_the_focused_letter_has_a_logistics_block).
- [ ] A candidate claim citing a story bank `result` string passes; one
  citing its `opening_line` is refused as unverified (planned
  test_letter_structure.py::test_story_bank_results_are_evidence_and_openers_are_not).
- [ ] A candidate claim citing a candidate document value passes (planned
  test_letter_structure.py::test_candidate_document_values_are_evidence).
- [ ] The markdown draft lists each gap with its posting line and each
  fact with its source document; the HTML contains neither section
  (planned test_letter_structure.py::test_gaps_and_facts_used_are_in_the_draft_only).
- [ ] A gap whose posting evidence is not in the posting refuses the
  letter (planned test_letter_structure.py::test_a_gap_must_quote_the_posting).
- [ ] An academic letter with no `gaps` key parses and is not linted
  (planned test_letter_structure.py::test_the_academic_letter_is_unchanged).
- [ ] The answers prompt and every answers check are byte for byte what
  they were: the answers hash in `SHARED_BLOCK_SHA256` and the answers
  prompt pins in `services/api/tests/test_academic_documents.py` do not
  change.
- [ ] No personal fact appears in `voices.py` or `letters.py`: the prompt
  and the linter name rules and synthetic examples only.

## Proof / origin

- The target shape is the operator's own: a role line, a hook led by a
  result, one paragraph per cluster, an optional company sentence, a
  deletable logistics paragraph and a one-line offer. It was given in the
  request for this spec, with a letter written to it by hand.
- Three recent industry and focused letters in the operator's local store
  show the problem. Two open by paraphrasing the posting, two end on
  "Happy to", and none has a logistics paragraph the operator could
  delete. They are personal data and are not quoted here.
- The current prompts: `INDUSTRY_LETTER_VOICE` and `FOCUSED_LETTER_VOICE`
  in `services/api/src/harrier/voices.py`, `LETTER_SHARED` in
  `services/api/src/harrier/apply/letters.py`.
- The evidence boundary: `ClaimContext` and `check_claims` in
  `services/api/src/harrier/apply/claims.py`, and `load_truth_sources` in
  `services/api/src/harrier/resume/content.py`.
- The dash rule: spec 071 and `dash_marks` in
  `services/api/src/harrier/resume/dashes.py`. The year count: `year_counts`
  in `services/api/src/harrier/resume/content.py`.

## Open decisions for Akin

1. **Story bank and candidate document as evidence.** This widens what a
   letter may cite beyond the truth and achievements documents, which is
   the verified-content invariant's boundary today. The story bank holds
   prose the operator wrote from the same facts, and its numbers can
   differ in rounding. The spec limits it to the four STAR strings. Is that
   boundary right, or should the story bank stay payload context only,
   citable never?
2. **The role line repeats the industry template's headline.** The
   industry template already prints the role under the name. The role
   line adds the team and the company. Keep both, or drop the headline on
   the industry template?
3. **L7's eight-word window.** Eight shared words catches a pasted
   sentence and lets a technology list through. A shorter window refuses
   ordinary phrases such as "design system and component library".
4. **L4 needs a number in the hook's first sentence.** A track whose
   evidence has no quantified result can never pass. The spec accepts
   that and exits 1. The alternative is a softer rule for that case.
5. **The focused kind's cap falls from 320 to 220 words.** Spec 140's six
   parts now fit in 220 with the scope reason and the logistics block.
   That is tight; the operator may prefer 260 on the focused kind.

## Out of scope

- The application answers generator (`harrier.apply.answers`), its voices
  and its checks. It shares `claims.py` and two helpers with the letter
  (`extract_json_object`, `slugify`), none of which change.
- The academic letter's structure, length and voice. Its prompt changes
  only by the shared formatting line.
- Scoring, screening, the tracker, tracks and track kinds.
- A versioned output path or a flag to keep an earlier run's files.
- Refusing a letter under 150 words.
- Proving the hook is the strongest result or that a proof cluster
  matches the posting. Code checks the shape; the operator judges the
  choice.
- Reconciling numbers between the story bank and the truth document.
- The API contract. `LetterDraft` is not served; the new lists reach the
  operator through the markdown draft the artifact endpoint already serves.

## Migration

None for data. The next `cover-letter` run on an industry or focused track
writes the new shape. Earlier letters on disk are not touched until a run
for the same company and role replaces them, as today.
