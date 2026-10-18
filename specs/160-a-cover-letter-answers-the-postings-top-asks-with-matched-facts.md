---
spec: 160
title: A cover letter answers the posting's top asks, in its order, with matched facts only
status: accepted
approved: yes
milestone: M9
depends: [065, 071, 085, 140, 158, 159]
---

# Spec 160: A cover letter answers the posting's top asks, in its order, with matched facts only

## Problem

Spec 158 fixed the letter's shape but not what goes in it. The writer
picks facts by how strong they look in the profile, not by what the
posting asks for. Nothing ranks the posting's asks, so nothing is
dropped.

A design-engineer posting shows the result. Its first asks are crafting
features from idea to design to implementation, a UX process, Figma, and a
portfolio. The generated letter (operator's local store, not quoted here):

- opened with the profile's biggest metric, which answers none of those
  asks, and stated the number without the action that produced it;
- gave the design asks one clause and the portfolio no link;
- spent a paragraph on a release pipeline no ask mentioned;
- paraphrased the perks section ("<Company> describes a startup with a
  flat structure");
- wrote the company name in the tracker's lowercase, not the posting's
  casing, and carried "~" on percentages the profile also states exactly.

Spec 158's L4 requires a number in the hook's first sentence. For a
posting whose first ask has no metric, that rule forces the wrong fact
into the hook.

## Scope

- `services/api/src/harrier/apply/postingasks.py` (new): posting sections,
  the analysis call, its checks, and the asks document.
- `services/api/src/harrier/apply/letters.py`: the writer payload, the link
  and experience lines, the two-call flow, and the asks file beside the
  letter.
- `services/api/src/harrier/apply/letterlint.py`: rules L4 (changed), L7
  (changed), L12 to L17 (new).
- `services/api/src/harrier/voices.py`: the industry and focused letter
  voices.
- `services/api/src/harrier/tracks.py`: the letter block range of
  `KindRules` only.
- `services/api/tests/fixtures/letters/` (new, public, synthetic): a failing
  and a passing letter with their asks and facts-used documents.
- Tests: `services/api/tests/test_posting_asks.py` (new), and the letter
  test files spec 158 added.

## Behavior

On the industry and focused kinds, `generate_cover_letter` makes two model
calls through the provider seam. The academic kind is unchanged.

### 1. Posting sections

Code splits the posting into sections at heading lines. A heading is a
short line (at most eight words, no sentence-ending punctuation) or a line
ending in ":".

- **Ask sections** are headings that match responsibilities or
  requirements: "what you'll do", "what you will do", "responsibilities",
  "your role", "the role", "requirements", "what we look for", "what you
  bring", "you have", "qualifications", "must have", "nice to have", "about
  you".
- **Dropped sections** match offer, perks, culture or history: "what we
  offer", "benefits", "perks", "why join", "why us", "our culture", "life
  at", "about us", "our story", "who we are", "compensation".
- Text before the first heading is an ask section when no ask heading
  exists, and dropped otherwise. Any other heading stays with the section
  type before it.

When no ask heading is found, the whole posting minus dropped sections is
the ask text. The posting text never reaches the writer.

### 2. The analysis call

Input: the ask text, and the candidate's facts as `{id: text}`:

- each line of the track's bullet pool, by its pool id (`r1_b3`, `ach_1`);
- each asserting line of the truth document as `t1`, `t2`, ... and of the
  achievements document as `a1`, `a2`, ..., numbered in document order
  (`TruthSources.asserted_lines`).

Output, strict JSON:

```json
{
  "asks": [{"ask": "string", "quote": "string", "fact_ids": ["string"]}],
  "logistics": {"quote": "string", "fact_ids": ["string"]},
  "product_fact_ids": ["string"]
}
```

- `asks`: the posting's first five distinct asks, in the posting's order.
  `quote` is verbatim from the ask text. `fact_ids` are the facts that
  meet the ask. An empty list makes the ask a gap.
- `logistics`: present only when the ask text states location, remote
  region, time zone, eligibility or contract terms. Its facts are where
  the candidate works from and how they can contract.
- `product_fact_ids`: facts that show first-hand experience of the
  company's product. Usually empty.

Code refuses the analysis when a quote is not in the ask text, the quotes
are out of posting order, a fact id is unknown, or there are more than
five asks. The analysis gets one retry naming what failed, as spec 085
gives a letter.

### 3. The asks document

Written beside the letter as `<slug>.asks.json`, in the never-in-git
letter store:

```json
{
  "company": "Restream",
  "asks": [{"rank": 1, "ask": "...", "quote": "...", "fact_ids": ["r1_b3"], "gap": false}],
  "logistics": {"quote": "...", "fact_ids": ["t12"]},
  "dropped_sections": ["What We Offer"],
  "gaps": ["..."]
}
```

`gaps` lists each ask with no facts, and "first-hand experience of
<Company>'s product" when `product_fact_ids` is empty. The markdown draft's
`## Gaps` section (spec 158) reads from it. Facts that match no ask are not
in the writer payload, however strong.

### 4. The writer call

The writer gets only:

- `company`: the name as the posting writes it (section 6);
- `role`;
- `asks`: rank, ask, and the text of each matched fact, by id;
- `logistics_facts`: the texts of `logistics.fact_ids`, when present;
- `product_facts`: the texts of `product_fact_ids`, when present;
- on the focused kind, `scope_reason` (spec 140).

No posting text, story bank, application profile or unmatched fact is
sent. Claims (spec 065) cite fact texts, and every fact text is letter
evidence: pool lines join the evidence the letter is checked against.

The writer rules:

- **Hook** answers ask 1 with its facts. When ask 1 is a gap, it answers
  the highest-ranked ask that has facts. It never states a metric without
  the action that produced it in the same paragraph. When the facts hold
  a metric but not its action, the hook uses another fact.
- **Proof**: one short paragraph per ask, in rank order, from that ask's
  facts. Asks 4 and 5 may share a paragraph.
- **Why this company**: only as a first-person product or technical fact
  from `product_facts`. With none, it is omitted, and the gap is listed.
  Never "<Company> describes" or "<Company> says".
- **Logistics** from `logistics_facts` only, its own paragraph.
- **Close**: one sentence, a concrete offer tied to the hook's work.

### 5. The template lines code adds

- **Link line.** When the ask text names a portfolio, work samples, case
  studies or GitHub, code inserts one line after the proof: `Portfolio:
  <url>`, `Work samples: <url>` or `GitHub: <url>`. For GitHub it is the
  facts' `candidate.github`; otherwise the first `candidate.links` entry
  whose label names a portfolio or site, else the first link, else
  `candidate.github`. The URL is shown without its scheme. With no URL in
  the facts, there is no line, and "a portfolio link" joins the gaps.
- **Experience line.** When the posting's title or ask text states years
  ("5+ years") or seniority (Senior, Staff, Lead, Principal), code inserts
  the facts' `candidate.experience_statement` as its own line after the
  hook. With no statement set, there is no line, and "an experience
  statement" joins the gaps. The statement is checked as spec 071 checks
  it.

### 6. Company name

The company is written as the posting writes it: the most frequent
case-sensitive form of the tracker's name in the posting, else the
tracker's name.

### 7. Shape and length

`KindRules.letter_blocks` becomes: industry 4 to 10, focused 5 to 10, role
line, link, experience line and close included. The body is two to five
paragraphs: the hook and at least one proof. The letter is 120 to 220
words, role line included.

### 8. The linter

Changed:

- **L4**: the hook's first sentence no longer needs a number. The banned
  openers stay.
- **L7**: a run of six or more consecutive words shared with the posting
  is refused (was eight). The evidence exemption (spec 158 amendment 1)
  stays.

New:

- **L12 tilde**: a percentage written with "~" is refused when an evidence
  line states the same value without "~".
- **L13 qualifier**: a number is refused when every evidence line holding
  it carries a qualifier ("~", "approximately", "about", "around",
  "roughly", "in total") and the letter's sentence carries none.
- **L14 casing**: any occurrence of the company name in another case than
  section 6's form is refused.
- **L15 company says**: a sentence that starts with the company name, or
  its possessive, followed by describes, says, offers, believes, claims or
  promises, is refused.
- **L16 coverage**: each of the top three asks with facts must have one of
  its facts cited by a claim (spec 158's facts used). An ask with no facts
  must be in `gaps`.
- **L17 link**: when section 5 inserts a link line, a letter without it is
  refused.
- **L3** now also refuses fewer than 120 words.

L12 to L17 read the asks document, and the pipeline passes it to
`lint_letter`. Without one, those rules do not run.

### 9. Fixtures

`services/api/tests/fixtures/letters/` holds two synthetic letters for a
synthetic design-engineer posting at Restream, with their asks and
facts-used documents:

- `restream-bad.*`: has the defects the Problem lists (tilde percentages,
  no link, "Restream describes", lowercase "restream", top asks uncovered).
- `restream-target.*`: the target shape, which passes.

Every candidate fact in them is synthetic. The operator's real letters are
personal data and are never committed (privacy rule).

## Failure modes

- **Ask 1 has no facts.** The hook answers the next ask that has facts.
  Ask 1 is in `gaps`.
- **No ask has facts.** The letter is refused with "no posting ask
  matches a profile fact", and the asks file is still written for the
  operator.
- **The analysis fails its checks twice.** Exit 1, naming the failed
  checks. No letter is written.
- **No headings in the posting.** The whole posting is the
  ask text. Perks may then reach the analysis, but never the writer.
- **A metric's action is not in the profile.** The hook uses another fact
  (section 4). The operator adds the action to the truth sources to use
  that metric.
- **A brief states limits** (spec 066). They replace the block range and
  word range, as today.

## Acceptance criteria

- [x] A posting with "What You'll Do", "What We Look For" and "What We
  Offer" sections gives the analysis only the first two
  (`services/api/tests/test_posting_asks.py::test_offer_and_perks_sections_never_reach_the_analysis`).
- [x] A quote not in the ask text, or out of posting order, refuses the
  analysis (`services/api/tests/test_posting_asks.py::test_an_ask_quote_must_be_verbatim_and_in_order`).
- [x] The writer payload holds no posting text and no unmatched fact
  (`services/api/tests/test_posting_asks.py::test_the_writer_gets_only_asks_and_matched_facts`).
- [x] The asks file is written beside the letter with ranks, matches and
  gaps (`services/api/tests/test_posting_asks.py::test_the_asks_file_is_written_beside_the_letter`).
- [x] A posting that asks for a portfolio gets the link line; one that does
  not gets none (`services/api/tests/test_posting_asks.py::test_the_link_line_follows_the_posting`).
- [x] A senior title gets the experience line from the facts
  (`services/api/tests/test_posting_asks.py::test_a_senior_posting_gets_the_experience_line`).
- [x] The company name takes the posting's casing
  (`services/api/tests/test_posting_asks.py::test_the_company_name_takes_the_postings_casing`).
- [x] The bad fixture fails L12, L14, L15, L16 and L17, and the target
  fixture passes every rule
  (`services/api/tests/test_letter_lint.py::test_the_bad_fixture_fails_and_the_target_passes`).
- [x] L7 refuses a six-word run, and L3 refuses 119 words
  (`services/api/tests/test_letter_lint.py::test_six_shared_words_and_too_few_words_are_refused`).
- [x] A "~" on another number in the evidence line qualifies nothing
  (amendment 3) (`services/api/tests/test_letter_lint.py::test_a_qualifier_counts_only_next_to_its_number`).
- [x] The academic letter and the answers generator are unchanged: their
  prompt pins hold
  (`services/api/tests/test_academic_documents.py::test_both_kinds_share_every_truth_rule`).

## Proof / origin

- The operator's review of the Restream letter, given in the request for
  this spec, with a target letter for the same posting.
- The current flow: `generate_cover_letter` and `build_cover_letter_payload`
  in `services/api/src/harrier/apply/letters.py`, and the linter in
  `services/api/src/harrier/apply/letterlint.py` (specs 158 and 159).
- The bullet pool and its ids: `bullet_pool` in the track's framing
  (`services/api/src/harrier/resume/content.py`, spec 098).

## Open decisions for Akin

1. **The funnel's action is not in the verified profile.** The target
   letter's hook says the operator researched and redesigned a funnel step
   and instrumented it. No truth line or pool bullet states that research,
   that step, or analytics on that funnel. The only analytics bullet
   belongs to another role. Under section 4 the hook cannot use the funnel
   metric until the operator adds the action to the truth sources and the
   pool. Add it, or accept a different hook?
2. **No portfolio link in the facts.** `candidate.links` holds one demo
   link and `candidate.github`. The target's portfolio URL is not a link
   in the facts. Add it to `resume_facts` with a "Portfolio" label, or let
   the demo link serve?
3. **No experience statement in the facts.** `candidate.experience_statement`
   is empty, so section 5's experience line never appears. Set it to the
   wording the truth document prescribes?
4. **The fixtures are synthetic.** The request asked to save the real bad
   and target letters as fixtures. They carry the operator's real metrics,
   employers and location, and the repository is public, so the fixtures
   reproduce the same defects with synthetic facts.

## Decisions taken (2026-10-18)

Akin approved the spec as written. Decisions 1 to 3 stay open: they are
profile data only the operator adds. Until then letters list a portfolio
link, an experience statement or a metric's action as gaps where a posting
needs them. Decision 4 stands: the fixtures are synthetic.

## Amendment (2026-10-18, implementation)

Regenerating the Restream and Simple letters, and the existing tests, found
these.

1. **No posting text.** With no posting there is nothing to rank. The
   writer gets every fact, unranked, under `facts`, and L16 and L17 do not
   run. The CLI always has a posting (spec 149); a direct caller may not.
2. **L14 needs the posting to write the name.** When the posting never
   writes the company's name, L14 does not run. Holding the letter to the
   tracker's casing would enforce the very lowercase the Problem names.
3. **L13 reads the qualifier next to its number.** A "~" attached to the
   number, "approximately", "about", "around" or "roughly" in the three
   words before it, or "in total" in the five words after it. As first
   built, a "~" anywhere on an evidence line qualified every number on it,
   so a line stating an exact funnel result beside an approximate total
   refused the exact result. This was fix 2 of 3; the generator and the
   linter agreed after it.
4. **The five asks that matter most, in posting order.** "The first five
   asks" took five duties from the first section of the Restream posting
   and left out the UX process, Figma and portfolio asks the request named.
   The analysis now picks the five that matter most for the role, judged by
   its title and craft, across every ask section, and lists them in the
   posting's order. The order check is unchanged.
5. **Code-added lines and writer gaps.** The link and experience lines are
   exempt from the eight-word paragraph floor (fix 1 of 3: the floor
   refused the link line). On this path the writer's own `gaps` are
   ignored: it never sees the posting to quote, and the analysis owns the
   gaps.
6. **The brief still reaches the writer.** `never_name`, `employer_guidance`,
   `guidance_url`, `operator_evidence` and `extra_notes` are the operator's
   instructions (spec 066), not posting text, so the writer payload keeps
   them.
7. **The focused letter's logistics.** When the posting raises no location
   or terms, the focused writer still gets the two candidate fields as
   `logistics_facts`, since spec 140 requires its contracting paragraph.
8. **The no-match refusal lists the asks.** When every ask is a gap, the
   refusal message names each ask; no asks file is written for a refused
   letter.
9. **Spec 158's "~12%" criterion.** Spec 158 accepted "12%" when the
   evidence says "~12%". L13 now refuses that, as this spec asks, and
   `test_a_number_must_be_in_the_evidence` checks it.
10. **Files the Scope did not name.** `services/api/src/harrier_cli/main.py`
    passes the asks document and the posting's company to the writer of
    the artifacts. `services/api/tests/conftest.py` stubs the analysis call
    for every test, so no test reaches a provider. The shared letter
    fixtures in `services/api/tests/test_apply_claims.py` and
    `services/api/tests/test_apply.py` gain a body block to clear the new
    floor, and `services/api/tests/test_apply_brief.py`,
    `services/api/tests/test_focused_documents.py` and
    `services/api/tests/test_academic_documents.py` follow the new ranges
    and pins. Spec 158's citations of two renamed tests are updated.

## Honest limitations

- **The letters are only as good as the matched facts.** The regenerated
  Restream letter passes every rule. But the profile has no fact for the
  UX process or a portfolio, so three of five asks carry facts, and the
  writer pads to reach 120 words. The floor and "drop unmatched facts"
  pull against each other when a profile is thin for a posting.
- **"A metric with its action" is a writer rule only.** Code cannot tell
  an action from a description, so a metric can still appear without the
  action that produced it.
- **Ranking is the model's.** Code checks that each quote is verbatim and
  in order, not that the five chosen are the five that matter most.

## Out of scope

- The academic letter, the answers generator, scoring, tracks and the
  tracker.
- Editing the operator's profile documents. Decisions 1 to 3 are the
  operator's to make through `profile put`.
- Rewriting existing letters on disk.
- Checking that a metric's action appears in the same paragraph. This is
  a writer rule; code cannot tell an action from a description.

## Migration

None for data. The next letter run writes the asks file beside the letter.
Until the operator settles decisions 1 to 3, letters omit the link and
experience lines and list them as gaps.
