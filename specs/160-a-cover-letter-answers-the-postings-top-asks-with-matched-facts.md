---
spec: 160
title: A cover letter answers the posting's top asks, in its order, with matched facts only
status: proposed
approved: no
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

- [ ] A posting with "What You'll Do", "What We Look For" and "What We
  Offer" sections gives the analysis only the first two
  (planned test_posting_asks.py::test_offer_and_perks_sections_never_reach_the_analysis).
- [ ] A quote not in the ask text, or out of posting order, refuses the
  analysis (planned test_posting_asks.py::test_an_ask_quote_must_be_verbatim_and_in_order).
- [ ] The writer payload holds no posting text and no unmatched fact
  (planned test_posting_asks.py::test_the_writer_gets_only_asks_and_matched_facts).
- [ ] The asks file is written beside the letter with ranks, matches and
  gaps (planned test_posting_asks.py::test_the_asks_file_is_written_beside_the_letter).
- [ ] A posting that asks for a portfolio gets the link line; one that does
  not gets none (planned test_posting_asks.py::test_the_link_line_follows_the_posting).
- [ ] A senior title gets the experience line from the facts
  (planned test_posting_asks.py::test_a_senior_posting_gets_the_experience_line).
- [ ] The company name takes the posting's casing
  (planned test_posting_asks.py::test_the_company_name_takes_the_postings_casing).
- [ ] The bad fixture fails L12, L14, L15, L16 and L17, and the target
  fixture passes every rule
  (planned test_letter_lint.py::test_the_bad_fixture_fails_and_the_target_passes).
- [ ] L7 refuses a six-word run, and L3 refuses 119 words
  (planned test_letter_lint.py::test_six_shared_words_and_too_few_words_are_refused).
- [ ] The academic letter and the answers generator are unchanged: their
  prompt pins in `services/api/tests/test_academic_documents.py` hold.

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
