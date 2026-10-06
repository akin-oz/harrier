---
spec: 078
title: A posting the candidate cannot take ranks below every posting they can
status: accepted
approved: yes
milestone: M8
depends: [031, 032, 033]
---

# Spec 078: A posting the candidate cannot take ranks below every posting they can

## Problem

`score_job` in `services/api/src/harrier/screening/rules.py` only adds. No
phrase in a posting can lower its score, so a restriction that makes the
posting impossible for this candidate is invisible to the ranking.

The case that exposed it, reconstructed synthetically: a US-only W-2 posting
that says "open to candidates anywhere in the US" matches `\banywhere\b` in
`PREFERRED_REGION_PATTERNS`, earns the preferred-region bonus on top of its
skill matches, and scored 151, near the top of the queue. The candidate
cannot take a W-2 role or a US-only role. The digest and the queue rank by
this number, so the posting took a slot an eligible one should have had.

Spec 077 proposes a learned score that can weigh such phrases. It cannot ship
until enough labels exist. This spec fixes the case now, inside the rule
score, with a penalty that is derived rather than tuned.

## Scope

- `services/api/src/harrier/screening/rules.py`: two new phrase tables,
  `US_SCOPE_PATTERNS` and `EMPLOYMENT_BLOCKER_PATTERNS`; a function
  `blockers(job)` returning the blocker class and phrase for each table that
  matches; a function `score_bounds(cfg)` returning the lowest and highest
  score an unblocked posting can reach; and one subtraction in `score_job`.
- `services/api/src/harrier/screening/policy.py`: both tables join
  `_rule_fingerprint`.
- Tests under `services/api/tests/` with synthetic postings only.

The gates (`title_allowed`, `remote_region_allowed`), `PREFERRED_REGION_PATTERNS`,
the tracker schema, `score_fields()`, the API contract and `apps/web` do not
change.

## Behavior

### Blocker tables

Matched against title, location and description after
`strip_eu_permit_phrases`, so an EU-permit phrase can never read as a
blocker (product invariant: those are positive signals). Every pattern is
word-bounded or anchored, the discipline `rules.py` already applies since the
Siracusa defect.

| Class | Phrases (the table is the authority; these are its shape) |
|---|---|
| `us_scope` | anywhere in the US or United States; US only; US-based or based in the US; must reside, live or be located in the US; US time zones only; authorized or eligible to work in the US |
| `employment` | W-2 or W2; at-will employment; no visa sponsorship, unable or not able to sponsor, sponsorship not available; F-1, OPT or CPT, student visa; US citizens or green card holders only; security clearance required |

`us_scope` does not fire when the location field names an explicit EMEA
region (a `PREFERRED_REGION_PATTERNS` match other than the ambiguous
`worldwide`, `global` and `anywhere`). "Remote, Europe" in the location with
"we also hire anywhere in the US" in the description is a posting the
candidate can take. `employment` has no such override: W-2 and at-will are
US payroll terms whatever the location says.

The location override is what the description-scoping rule in
`remote_region_allowed` protects against in a different form. That rule keeps
negative location hints out of descriptions because descriptions make
comparisons; here the description is the only place these phrases appear,
so they are read there, and the location field gets the last word on scope.

### The penalty is derived, not chosen

A blocked posting is one the candidate cannot take, so it belongs below every
posting they can. The penalty is the smallest number that guarantees that,
computed from the configuration:

`penalty = high - low + 1`

where, from `score_bounds(cfg)`:

- `high` is the most an unblocked posting can score: base, exact title
  bonus, include keyword cap, every `SKILL_SIGNALS` weight, every
  `PREFERRED_SIGNAL_WEIGHTS` weight, remote bonus, preferred-region bonus,
  and the larger of the two domain bonuses.
- `low` is the least an unblocked posting can score once it has passed the
  gates: base plus remote bonus, the part
  `test_the_arithmetic_floor_is_derived_from_the_rules` already proves is
  unavoidable.

There is no configuration key for it. A number someone can set is a number
someone will tune until the 151 case looks right; a derived one moves when
the weights do and is always exactly sufficient. No constant appears in the
code or in this spec.

The penalty is applied once when any blocker fires. Two blockers do not
stack: the posting is already below everything eligible, and ordering among
impossible postings has no value.

### Output

- `fit_score` is the rule score minus the penalty. It is negative for a
  blocked posting. That is intended: a negative score reads as "cannot take
  this", and no reader compares the score with a fixed number (checked at
  spec time: no threshold on `fit_score` or `stored_score` exists in
  `services/api/src` or `apps/web/src`). Not clamped at 0, because clamping
  would tie every blocked posting.
- `signals` gains one entry per blocker that fired, for example
  `blocker=us_scope "anywhere in the us"` and `blocker=employment "w-2"`,
  after the existing reasons. The phrase is the matched text, so "why is this
  last" is answerable from the row.
- Written through `score_fields()` only.
  `test_every_score_field_is_written_together` stays green unchanged.

### Gates stay filters

A blocker lowers a rank. It never rejects. The gates run before scoring
exactly as now and their verdicts do not depend on the blocker tables.
Remote-only and EMEA enforcement are not touched. A US-only posting that
passes the region gate today still passes it; spec 032 owns that defect.

### Versioning

Both tables join `_rule_fingerprint`, so `policy_version` and
`scoring_version` change when this lands and whenever a phrase is added or
removed. Stored scores are not recomputed (spec 033). Open rows can be
rescored with `harrier reevaluate`, which goes through `score_job` and so
picks up the penalty. Because the policy version also stamps seen decisions
(spec 031), stored gate rejections become eligible for `harrier reconsider`;
the gates are unchanged, so reconsideration reaches the same verdicts.

## Failure modes

- A blocker phrase in a posting the candidate can take (a false positive)
  sends an eligible posting to the bottom. The location override covers the
  known shape; the false-positive fixtures below pin the others. A miss is
  visible in `signals` and fixed by editing the table, which moves the
  version.
- A blocker phrased in a way the tables do not list (a false negative) ranks
  as it does today. No worse than now.
- Must not introduce: a gate verdict that depends on a blocker; an EU-permit
  phrase read as a blocker; a configurable penalty; a score written outside
  `score_fields()`.

## Acceptance criteria

Tests marked planned do not exist yet. They are named here so the
implementation has a target; the implementing change cites each one in
backticks, where `tests/test_spec_structure.py` checks it exists.

Tests in `services/api/tests/test_scoring.py` unless named otherwise. Every
posting is synthetic, with an invented company.

- [ ] A synthetic US-only W-2 posting ("anywhere in the US", W-2, no
      sponsorship) ranks below a synthetic EMEA-remote posting with the same
      skill keywords, and its `signals` name both blockers with the matched
      phrases (planned test_a_us_only_w2_posting_ranks_below_an_emea_remote_one)
- [ ] The strongest unblocked posting the configuration allows, blocked by one
      phrase, scores below the weakest unblocked posting that passes the
      gates; the penalty is computed from `score_bounds`, not restated
      (planned test_the_blocker_penalty_is_derived_from_the_rules)
- [ ] Two blockers apply one penalty (planned test_blockers_do_not_stack)
- [ ] "Remote, Europe" in the location suppresses `us_scope` from the
      description, and does not suppress `employment`
      (planned test_an_explicit_emea_location_overrides_us_scope)
- [ ] EU-permit phrases never fire a blocker ("must be based in the EU",
      "EU work permit required", "EU-based contractor")
      (planned test_eu_permit_phrases_are_never_blockers)
- [ ] False-positive fixtures do not fire: "we sponsor visas", "unlike US-only
      roles, this one is open across Europe" with a European location,
      "US" inside an unrelated word, "W2" inside a product name
      (planned test_blocker_tables_do_not_fire_on_eligible_postings)
- [ ] A blocker never changes a gate verdict
      (planned tests/test_screening.py::test_a_blocker_never_changes_a_gate_verdict);
      the rest of `tests/test_screening.py` passes unchanged
- [ ] `policy_version` changes when either table changes
      (planned tests/test_seen_policy.py::test_the_blocker_tables_move_the_policy_version)
- [ ] `test_every_score_field_is_written_together`,
      `test_no_reader_takes_a_field_the_writer_does_not_fill` and
      `test_the_arithmetic_floor_is_derived_from_the_rules` pass unchanged
- [ ] No real posting or company appears in a fixture (ADR-008)
- [ ] All gates green on PR

## Honest limitations

- **Untested against real outcomes.** The tables come from the known case and
  from what US-only postings usually say, not from measuring which rows the
  candidate skipped. Spec 077's export and evaluation would measure them; it
  does not exist yet. Before merge, the phrases are checked read-only against
  the local tracker by counting how often each fires on acted-on rows. That
  count is reported in the session, not in the repository (ADR-008), and a
  phrase that fires on acted-on rows is removed or narrowed.
- **Phrase tables have phrase blind spots.** A blocker worded differently is
  missed and the posting ranks as it does today.
- **The region gate still lets "anywhere in the US" through.** This spec ranks
  such a posting last; it does not reject it. Spec 032.
- **Negative scores mix with history.** Rows scored before this change keep
  their scores and their version. A list sorted across versions puts a new
  blocked row below an old one that was never checked for blockers, which is
  what `scoring_version` exists to make visible.

## Out of scope

- Changing `PREFERRED_REGION_PATTERNS` so `\banywhere\b` stops matching "anywhere
  in the US". The same table feeds the region gate, so editing it changes gate
  verdicts. Spec 032.
- The region gate itself. Spec 032.
- The learned score. Spec 077, which should reuse these two tables as its
  `us_scope` and `employment_blocker` features and use the penalized rule
  score as its baseline. That is an amendment to 077, made on its own branch.
- Penalties for anything other than impossibility, such as seniority or stack
  mismatch. Those are preferences and belong to ranking weights, not to a
  "cannot take" penalty.
- The web UI.

## Proof / origin

- `services/api/src/harrier/screening/rules.py`: `score_job` only adds, and
  `PREFERRED_REGION_PATTERNS` contains `\banywhere\b`. The 151 case follows
  from those two lines.
- `services/api/tests/test_scoring.py::test_the_arithmetic_floor_is_derived_from_the_rules`:
  the unavoidable part of every unblocked score, which `low` reuses.
- Spec 033: there is no cutoff and the score ranks, so a penalty that only
  reorders is the correct tool, and a filter would not be.
- Spec 077, Minimum data: this spec is the interim fix proposed there.
- Product invariant (CLAUDE.md): EU-permit phrases are positive signals,
  never filters, hence `strip_eu_permit_phrases` before any blocker matches.
