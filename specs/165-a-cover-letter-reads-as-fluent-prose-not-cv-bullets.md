---
spec: 165
title: A cover letter reads as fluent professional prose that says why this role, not as CV bullets with "I" in front
status: accepted
approved: yes
milestone: M9
depends: [065, 085, 140, 158, 160]
---

# Spec 165: A cover letter reads as fluent professional prose that says why this role, not as CV bullets with "I" in front

## Problem

Specs 158 and 160 fixed what a letter may claim and in what order. They
did not fix how it reads. The operator's verdict on the letters written
since spec 160 shipped: every sentence starts with "I", and the letter reads
like a beginner's English homework.

A letter in the operator's local store, written after spec 160 (personal
data, not quoted here), shows the pattern:

- 11 of its 13 body sentences start with "I" or "My".
- Most body sentences are a pool bullet copied word for word with "I" put
  in front. Read in a row, the body is the CV's bullet list.
- Each proof paragraph stacks two unrelated facts with no sentence linking
  them to the ask they answer.
- Nothing says why the candidate wants this role.
- The industry letter has no salutation and no sign-off. It begins with a
  bare role line and ends with the close.

The causes are in the prompt and the template:

- `INDUSTRY_LETTER_VOICE` and `FOCUSED_LETTER_VOICE`
  (`services/api/src/harrier/voices.py`) ask for "short, punchy sentences".
  The model writes one fact per sentence, each starting with the subject.
- Both voices say of the facts: "Quote them verbatim as claim evidence."
  The model copies them into the letter, not only into the claim. The claim
  check (spec 065, `check_claims` in
  `services/api/src/harrier/apply/claims.py`) does not need that: a claim's
  sentence must be in the letter and its evidence fragments must be in the
  sources, but the sentence may say the fact in other words.
- The proof rule is "one short paragraph per further ask, from that ask's
  facts only". Nothing asks the paragraph to say what the facts show for
  the ask.
- The writer has no input that says why the candidate wants this kind of
  role. Only the focused kind has one (`scope_reason`, spec 140).
- `templates/cover-letter-template.html` prints no salutation and no
  sign-off.

The University of Edinburgh Careers Service's cover letter guidance (see
Proof / origin) names each of these:

- A letter answers three questions: why this job, why this organisation,
  why you. The first paragraph should say why the candidate wants the job.
- Pick three to five examples tied to the job. Put them in context; do not
  list what you have done.
- "Never copy sentences or paragraphs directly from your CV. Vary your
  language choices to keep the reader engaged, but ensure it sounds like
  you."
- Avoid negative tone ("I had to", "I was required to"), passive voice about
  your own growth ("my communication skills were strengthened by"), and
  starting every sentence with "I am" or "I can".
- Use a business letter layout with a salutation, and close positively.

## Scope

- `services/api/src/harrier/voices.py`: `INDUSTRY_LETTER_VOICE` and
  `FOCUSED_LETTER_VOICE`. The academic voice and every answers voice are
  unchanged.
- `services/api/src/harrier/apply/letters.py`: `writer_payload` passes
  `motivation`; `cover_letter_violations` passes the asks' fact texts to
  the linter; the industry render fills the salutation and sign-off.
- `services/api/src/harrier/apply/letterlint.py`: rules L18 to L22, and
  `LintContext.fact_texts`.
- `services/api/src/harrier/apply/postingasks.py`: the gap "a motivation
  statement" when `motivation` is empty.
- `templates/cover-letter-template.html`: the salutation and sign-off.
- Tests: `services/api/tests/test_letter_lint.py`,
  `services/api/tests/test_letter_structure.py`, and the synthetic fixtures
  in `services/api/tests/fixtures/letters/` (spec 160), which gain a
  bullet-copy letter. The prompt pins in
  `services/api/tests/test_academic_documents.py` change on purpose: the
  industry and focused letter prompts' hashes. The answers pins do not
  change.

## Behavior

### 1. Motivation

The track's application profile (the JSON `load_profile_json` reads) gains
an optional string key `motivation`: in the operator's words, why they want
this kind of role. It is a stated preference, like the focused kind's
`scope_reason`, never a claim about the employer and never evidence.

`writer_payload` passes it as `motivation` on the industry and focused
kinds. When it is empty, the writer writes no "why this role" sentence and
the asks document's `gaps` gains "a motivation statement".

### 2. What the writer is told

Both voices replace "short, punchy sentences" and "Quote them verbatim as
claim evidence" with these rules:

- **Voice.** Write as a fluent, professional speaker of English writing to
  a hiring manager. Vary sentence length and structure. Join related facts
  in one sentence with a clause (by, after, so that, which, while) where
  they belong together.
- **Facts are evidence, not sentences.** Say each fact in your own words.
  Keep every number, tool, product and organisation exactly as the fact
  states it. Never copy a fact as a sentence. The claim's evidence
  fragments stay verbatim; the sentence does not have to.
- **Hook.** One or two sentences on why this role, from `motivation`, then
  the strongest fact for ask 1 told as a result with its action. Never
  open with "I", the company or the role.
- **Proof.** Each paragraph opens with a sentence that names what the ask
  needs, in your own words (never six words in a row from the posting, L7),
  then gives one or two facts as the evidence, then says in one clause what
  that means for this role. Facts for asks 4 and 5 may share a paragraph.
- **Openings.** Do not start two sentences in a row with "I" or "My". Do
  not start two paragraphs with the same word.
- **Tone.** Never frame a fact negatively ("had to", "was required to",
  "I have never"). Write growth in the active voice ("I strengthened X by",
  never "X was strengthened by").

Why this company (product facts only, spec 160), logistics and the
one-sentence close are unchanged.

### 3. The linter

New rules in `lint_letter`, on the industry and focused kinds. Matching is
case-insensitive on word boundaries, with a straight or curly apostrophe
read alike. "Body sentences" are the sentences of every block after the
role line, except the link and experience lines code adds (spec 160) and
the close.

- **L18 negative framing.** Refused anywhere in either version: had to,
  was required to, were required to, although I have never, although I've
  never, I have never done, I've never done, I don't have, I do not have,
  I lack, despite not having.
- **L19 passive growth.** Refused anywhere in either version: was, were,
  has been or have been, then developed, strengthened, improved, honed,
  gained or built, then by or through.
- **L20 "I" openings.** Refused when two consecutive body sentences start
  with I, I'm, I've, I'd, I'll or My, or when more than a third of the body
  sentences do. The message gives the count: `"I" or "My" starts 6 of 9
  body sentences`.
- **L21 copied fact.** Refused when a body sentence shares a run of ten or
  more consecutive words with one fact text the writer received under
  `asks`, compared as L7 compares (case and punctuation aside). The message
  names the fact id, never its text.
- **L22 paragraph openers.** Refused when two body paragraphs begin with
  the same word.

L18 to L22 run only through `lint_letter`, so they get the one retry spec
085 gives every refusal. The academic letter is not linted, as today.

### 4. The industry letter's frame

`templates/cover-letter-template.html` prints, around `full_version`:

- above it, `Dear Hiring Manager,`;
- below it, `Kind regards,` and then the candidate's name on its own line.

Neither is a block of `full_version`, so the block range (spec 160) and the
word range do not count them. The focused template is unchanged: it already
prints "Hello <Company> team," and the name.

## Failure modes

- **No motivation set.** The hook opens with the fact for ask 1 and no
  "why this role" sentence. "a motivation statement" is listed under
  `## Gaps`. The letter is not refused.
- **A fact is only ten words long.** Any sentence carrying it with "I" in
  front shares a ten-word run and is refused by L21. The retry usually
  rewords it. If not, exit 1 naming L21, as for every refused letter
  (spec 085).
- **A paraphrase changes a fact.** The claim check refuses a number, a
  skill or an entity the evidence does not state (spec 065), as today.
  Code cannot catch a paraphrase that changes a meaning while keeping
  every number and name. The operator reads every letter before sending
  (product invariant "Nothing auto-sends").
- **A true sentence needs a listed phrase** ("the team had to migrate in
  two weeks"). L18 refuses it; the retry rewords it.

## Acceptance criteria

- [x] A synthetic letter breaking each of L18 to L22 is refused with that
      rule's message, and the same letter fixed passes (`services/api/tests/test_letter_lint.py::test_prose_rules_refuse_and_their_fixes_pass`).
- [x] Two consecutive "I" sentences are refused; three "I" sentences in
      nine, none consecutive, pass (`services/api/tests/test_letter_lint.py::test_i_openings_count_runs_and_share`).
- [x] A body sentence that is a fact with "I" in front is refused by L21
      naming the fact id; the same fact reworded with its number and tool
      kept passes L21 and the claim check (`services/api/tests/test_letter_lint.py::test_a_copied_fact_is_refused_and_a_reworded_one_passes`).
- [x] "was built through a migration" is refused by L19 and "built a
      migration path" is not (`services/api/tests/test_letter_lint.py::test_passive_growth_needs_the_preposition`).
- [x] The writer payload carries `motivation` from the application
      profile, and an empty one adds "a motivation statement" to the asks
      file's gaps (`services/api/tests/test_letter_structure.py::test_motivation_reaches_the_writer_or_is_a_gap`).
- [x] The industry HTML has "Dear Hiring Manager," before the letter and
      "Kind regards," and the name after it; the markdown draft's
      `full_version` has neither (`services/api/tests/test_letter_structure.py::test_the_industry_letter_has_a_salutation_and_sign_off`).
- [x] A new synthetic fixture, `bullet-copy-bad.*` in
      `services/api/tests/fixtures/letters/`, fails L20 and L21, and the
      spec 160 target fixture, revised to the new voice, passes every rule
      (`services/api/tests/test_letter_lint.py::test_the_bullet_copy_fixture_fails_and_the_target_passes`).
- [x] The academic letter is not linted, and the answers prompt pins hold
      (`services/api/tests/test_academic_documents.py::test_both_kinds_share_every_truth_rule`).

## Proof / origin

- University of Edinburgh Careers Service, "Writing a cover letter"
  (https://careers.ed.ac.uk/cvs-and-applications/writing-a-cover-letter):
  the three questions, three to five tailored examples, "Never copy
  sentences or paragraphs directly from your CV", tip 7 on negative tone,
  passive voice and repeated "I am" openings, and tip 2 on business layout.
- The cover letter examples the Careers Service publishes through
  CareerSet (graduate, legal training contract, speculative). Their margin
  notes say to show why the organisation appeals, to vary language, and to
  avoid repeating words. They are not committed.
- The operator's review of the letters written since spec 160, given in
  the request for this spec. The letters are personal data and are not
  quoted.
- The current prompt: `INDUSTRY_LETTER_VOICE` and `FOCUSED_LETTER_VOICE`
  in `services/api/src/harrier/voices.py`. The payload: `writer_payload` in
  `services/api/src/harrier/apply/letters.py`. The claim check: `check_claims`
  in `services/api/src/harrier/apply/claims.py`.

## Out of scope

- The academic letter and every answers generator. Tone rules for answers
  are spec 167.
- A named addressee ("Dear Ms Example,"). It needs a brief field, which the
  API contract and the web brief panel carry (spec 066); that is its own
  change.
- The em dash in the industry template's `<title>`, which spec 071 left to
  the letter path. It is a separate fix.
- "Why this company" from research. Spec 160 limits it to first-hand
  product facts, and this spec does not reopen that.
- The close's wording. Spec 158 bans "looking forward", and this spec
  keeps the ban.

## Honest limitations

- Fluency is not measured. The rules refuse the patterns the operator
  named (the "I" openings, copied bullets, repeated paragraph openers); a
  letter that avoids them can still read flat.
- L21's ten words and L20's one third are judgments, not figures from the
  guidance. The guidance says to vary the language and not to copy, with no
  number.
- More freedom to reword means more room for a reworded fact to drift. The
  claim check holds numbers, skills and entities; it does not hold meaning.

## Open decisions for Akin

1. **Your `motivation` text.** The letter can only say why you want the
   role if you write it. Set it per track with `profile put`, or let the
   gap show until you do?
2. **L20's limit.** No two "I" sentences in a row and at most a third of
   body sentences. Stricter, looser, or as written?
3. **L21's window.** Ten shared words with one fact. A shorter window
   pushes harder paraphrase and more drift risk.
4. **The sign-off.** "Kind regards," suits an international reader. The
   Edinburgh guidance uses the British "Yours faithfully" after "Dear
   Hiring Manager". Keep "Kind regards,"?

## Decisions taken (2026-10-26)

Akin approved the spec as written, which settles the open decisions above
as the spec states them.

## Amendment (2026-10-26, implementation)

The build found these places where the Behavior was silent or named the
wrong file.

1. **The motivation gap is added in `letters.py`.** Scope named
   `postingasks.py`. The other code-added gaps (the link, the experience
   statement, the product) are added in `plan_letter`
   (`services/api/src/harrier/apply/letters.py`), so the motivation gap is
   added there too, through `track_motivation` and `MOTIVATION_GAP`.
   `postingasks.py` does not change.
2. **The academic letter shares the industry template.** Both kinds render
   `templates/cover-letter-template.html`. The template gains two
   placeholders, `{{salutation_html}}` and `{{sign_off_html}}`, which
   `letter_frame` fills on the industry kind and leaves empty on the
   academic kind, so the academic letter is unchanged.
3. **One CSS rule.** `templates/cover-letter-template.css`, not in Scope,
   gains a top margin on the sign-off so it sits apart from the close.
4. **L20 to L22 read the body only when they can.** The linter learns which
   blocks code added from `LintContext.added_lines`, set with
   `LintContext.fact_texts` in `_checked_letter`. A caller that sets neither
   (the tests' direct calls, the academic path) still gets L18 to L20 and
   L22 over every block between the role line and the close; L21 needs the
   facts and does not run without them.
5. **L22 names the word as the letter writes it** ("For", not "for"), and
   compares case-insensitively.
6. **Shared test letters move to the new voice.** The synthetic letters
   other suites share broke L20 to L22: `CHECKOUT` and `INVOICES` in
   `services/api/tests/test_apply_claims.py` started with "I" one after the
   other, `PROOF` in `services/api/tests/test_letter_lint.py` did too, and
   `FULL_LETTER` in `services/api/tests/test_apply.py` followed an "I" hook
   with a "My" sentence. Each is reworded to keep its facts and numbers.
   `restream-target.json` is revised as section 3 of the Acceptance criteria
   says; its hook and component sentence also copied a fact (L21).
