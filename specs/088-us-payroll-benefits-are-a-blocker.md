---
spec: 088
title: A posting that offers only US payroll benefits ranks with the postings the candidate cannot take
status: accepted
approved: yes
milestone: M8
depends: [078]
---

# Spec 088: A posting that offers only US payroll benefits ranks with the postings the candidate cannot take

## Problem

Spec 078 floors a posting the candidate cannot take, but only when the
posting says so in words its tables list: "US only", "authorized to work in
the US", "W-2". Many US-payroll postings never say any of these. They say it
through the benefits they offer instead.

The case that exposed it, reconstructed synthetically: a job aggregator
relays a US consultancy's posting with the location "Worldwide" and the type
"Remote". The description lists a 401(k) plan, subsidized medical, dental
and vision insurance, and short and long term disability cover. The
work-authorization question appears only in the application form, which
Harrier never reads. No phrase in `US_SCOPE_PATTERNS` or
`EMPLOYMENT_BLOCKER_PATTERNS` matches, "Worldwide" is ambiguous so the
location says nothing, and the posting ranks on its skill matches alone. The
candidate found out it was impossible only when filling in the form.

A 401(k) is a US retirement plan. A posting that offers one and no
alternative is paying through US payroll, which the candidate cannot join.

## Scope

- `services/api/src/harrier/screening/rules.py`: a new phrase table
  `US_PAYROLL_PATTERNS`, a new blocker class `us_payroll` in `blockers(job)`.
- `services/api/src/harrier/screening/policy.py`: the table joins
  `_rule_fingerprint`.
- Tests under `services/api/tests/` with synthetic postings only.

`score_job`, `score_bounds`, the penalty, the gates, the existing two
tables, the tracker schema, the API contract and `apps/web` do not change.

## Behavior

### The table

`US_PAYROLL_PATTERNS` holds US retirement and tax-advantaged account names
that have no equivalent outside US payroll:

| Phrase | Spellings matched |
|---|---|
| 401(k) | `401(k)`, `401k`, `401 k`, `401 (k)` |
| 403(b) | `403(b)`, `403b`, `403 b`, `403 (b)` |
| Health savings account | `health savings account` |

Matched like the other blocker tables: against title, location and
description after `strip_eu_permit_phrases` and `normalize`, word-bounded.

### When it fires

`us_payroll` fires on the first table match, unless one of these holds:

1. **The location names an explicit EMEA region**
   (`location_names_explicit_emea`), the same override `us_scope` has. A
   posting located "Remote, EMEA" that lists a 401(k) is a company with US
   staff hiring in Europe.
2. **The benefit is qualified as one region's.** A US word (`us`, `u.s.`,
   `usa`, `united states`, `american`) in the same list item as the match
   says the benefit applies to US staff only, which implies the posting hires
   elsewhere: "401k (US employees)", "401(k) for US-based staff",
   "US: 401(k) match". A list item ends at a line break, a bullet, a
   semicolon or a sentence-ending full stop.
3. **The match is a salary.** A number that is part of a money amount does
   not fire: "$350k to $401k", "USD 401k". A currency sign or code directly
   before it, or a range joining it to another amount, makes it money.
4. **The description says the role can be worked from outside the US.** A
   reach phrase about where a person may work: "work from anywhere",
   "anywhere in the world", "from any country", "in any country",
   "globally distributed". Not when followed by "in the US" (that is
   `us_scope`'s "anywhere in the US"). A benefit list beside such a phrase
   is a US company that also hires abroad and states its US benefits
   without labeling them, which is the false positive the read-only check
   found on a posting the candidate could take.

   Bare "worldwide", "global" and "around the world" are not reach phrases
   here. They describe customers and offices ("used by teams worldwide")
   as often as hiring, and the check found US-located postings they would
   have let through.

Unlike `employment`, `us_payroll` has the location override, because a
benefit list describes the company's payroll as a whole, not the role's.
Unlike `us_scope`, it does not use `_offers_emea`: a benefit is not offered
as a place, so "401(k) or EU pension" is covered by rule 2 only if a US word
qualifies the 401(k), and otherwise fires. That is deliberate: see failure
modes.

### Output

As spec 078. One penalty when any blocker fires, no stacking. `signals`
gains `blocker=us_payroll "401(k)"` with the matched text. Written through
`score_fields()` only.

### Deliberately absent

Checked against the descriptions already collected before writing this
spec; the findings were reported in the session, not here (ADR-008).

- **Medical, dental and vision insurance; disability cover; life
  insurance.** European and UK employers offer these too.
- **A USD salary range.** US companies pay European contractors in dollars.
- **"HSA" alone.** The acronym has other meanings; only the full phrase is
  in the table.
- **On-site visits to a US city, or "candidates in the X area get
  priority".** Travel and preference make a posting harder, not impossible.
  Spec 078 keeps preferences out of the impossibility penalty.

## Failure modes

- **A global company lists a 401(k) without qualifying it,** with a location
  that names no EMEA region ("Remote") and no reach phrase from rule 4. The
  posting is floored though it may hire in Europe. This is the remaining
  false-positive shape and it is accepted: the row's `signals` say why it
  sank. If it recurs on postings the candidate acts on, rule 2 or rule 4
  widens.
- **A US company says "work from anywhere" meaning anywhere in the US**
  without saying "in the US". Rule 4 lets it through and it ranks as it
  does today. Accepted: rule 4 fails open by design.
- **A US-only posting whose benefits section is missing from the stored
  description** (truncated scrape, aggregator chrome) is not caught. No
  worse than today.
- **The qualifier window is too wide** when a description is one long
  unpunctuated line: a US word anywhere in it suppresses the match. Fails
  open, toward today's behavior.
- **A salary written with no currency mark** ("between 350k and 401k") is
  read as a 401(k). Rule 3 catches the range ("and" joining two amounts).
- Must not introduce: a gate verdict that depends on this table; an
  EU-permit phrase read as a blocker; a second penalty for two blockers.

## Acceptance criteria

Tests in `services/api/tests/test_scoring.py` unless named otherwise. Every
posting is synthetic, with an invented company.

- [x] A synthetic posting located "Worldwide", type remote, with React and
      TypeScript skills and a benefits list holding "401(k) plan", ranks
      below the same posting with that line removed, and its `signals` name
      `blocker=us_payroll` with the matched text
      (`test_a_401k_posting_relayed_as_worldwide_ranks_last`)
- [x] Each spelling in the table fires on its own
      (`test_every_us_payroll_spelling_fires`)
- [x] A location naming EMEA suppresses `us_payroll`; "Worldwide" and
      "Remote" do not (`test_an_explicit_emea_location_overrides_us_payroll`)
- [x] A US-qualified benefit does not fire: "401k (US employees)",
      "401(k) for US-based staff", "US: 401(k) match"
      (`test_a_us_qualified_benefit_is_not_a_blocker`)
- [x] A US word in a different list item does not suppress the match
      (`test_a_us_word_in_another_list_item_qualifies_nothing`)
- [x] A 401(k) beside "Fully remote, work from anywhere" with location
      "Remote" does not fire; beside "work from anywhere in the US" it does
      (`test_a_reach_phrase_overrides_us_payroll`)
- [x] A 401(k) beside "trusted by teams worldwide" or "customers around
      the world" with location "Remote" still fires
      (`test_a_customer_reach_is_not_a_hiring_reach`)
- [x] Salary amounts do not fire: "$350k to $401k", "USD 401k",
      "350k and 401k" (`test_a_salary_is_not_a_401k`)
- [x] Medical, dental, vision and disability cover, a USD salary range and
      on-site visits to a US city, alone or together, fire no blocker
      (`test_benefits_offered_outside_the_us_are_not_blockers`)
- [x] A posting with both `us_payroll` and `employment` takes one penalty
      (`test_us_payroll_and_employment_take_one_penalty`)
- [x] `us_payroll` never changes a gate verdict
      (extend `tests/test_screening.py::test_a_blocker_never_changes_a_gate_verdict`)
- [x] `policy_version` changes when `US_PAYROLL_PATTERNS` changes
      (extend `tests/test_seen_policy.py::test_the_blocker_tables_move_the_policy_version`)
- [x] The spec 078 tests pass unchanged
- [x] No real posting or company appears in a fixture (ADR-008)
- [ ] All gates green on PR

## Out of scope

- Rejecting blocked postings instead of ranking them last. Spec 078 and
  spec 033 decide that blockers rank and never reject; changing it is its own
  spec.
- Reading application forms. The work-authorization question in the case
  above lives there, and Harrier has no source that fetches forms.
- Cleaning aggregator page chrome out of stored descriptions.
- An aggregator's location field that names an unrelated place. The
  location override reads what is there; it does not geocode.
- Rescoring existing rows. `harrier reevaluate` does that on request
  (spec 078, Versioning).
- The learned score (spec 077). It should take `us_payroll` as a feature
  beside the spec 078 classes; that is an amendment to 077.

## Migration

None. The policy and scoring versions move when this lands. Stored scores
keep their old version until `harrier reevaluate` rescores open rows.

## Honest limitations

- **One case, one marker.** The table comes from the case above and from
  what US benefit lists usually contain, not from measured outcomes.
- **The qualifier rule is a heuristic.** "List item" is read from
  punctuation, and scraped descriptions do not always keep it.
- **A floored posting still appears,** last. The candidate still sees it
  if the queue is short.

## Proof / origin

- `services/api/src/harrier/screening/rules.py`: `US_SCOPE_PATTERNS` and
  `EMPLOYMENT_BLOCKER_PATTERNS` hold no benefit names, and `blockers(job)`
  reads only those two tables.
- `AMBIGUOUS_REGION_PATTERNS` holds `\bworldwide\b`, so a "Worldwide"
  location cannot override a blocker, which is why the new class needs no
  change to the override.
- Spec 078, Deliberately absent: "no visa sponsorship" and "US-based" were
  removed because they describe companies as often as roles. Rule 2 applies
  the same reasoning to a US-qualified benefit.

## Amendment (2026-10-07, during implementation)

Four points the implementation settled, none changing which postings the
spec meant to floor:

- **A bare "US" qualifies only in capitals.** Lower case "us" is the
  pronoun: "401(k) plan with us matching 4%" would otherwise label the
  benefit as US staff's and let a US-only posting through. `U.S.`, `USA`,
  "United States" and "American" match in any case.
  `tests/test_scoring.py::test_a_us_word_in_another_list_item_qualifies_nothing`.
- **A full stop ends a list item when a capital or a digit follows.** The
  item is split before lowercasing, so "U.S. employees" stays one item
  while "Our clients are US banks. 401(k) plan" is two.
  Same test.
- **The reach phrases are a table, `US_PAYROLL_REACH_PATTERNS`,** and join
  `_rule_fingerprint` beside `US_PAYROLL_PATTERNS`: adding one changes which
  postings are floored, as a blocker phrase does.
  `tests/test_seen_policy.py::test_the_blocker_tables_move_the_policy_version`.
- **The stacking criterion is its own test,**
  `test_us_payroll_and_employment_take_one_penalty`, rather than an
  extension of the spec 078 test, which stays unchanged.

Before merge, the blocker was run read-only over the stored descriptions of
tracked rows. What it found was reported in the session, not here
(ADR-008).
