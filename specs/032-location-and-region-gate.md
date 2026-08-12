---
spec: 032
title: The location gate rejects the roles it exists to find
status: accepted
approved: yes
milestone: M6
depends: [007, 031]
---

# Spec 032: The location gate rejects the roles it exists to find

## Problem

`remote_region_allowed` in services/api/src/harrier/screening/rules.py is
wrong in three directions at once, verified by running it:

- `Remote (must be based in the EU)` is **rejected** as "location says
  hybrid/on-site", because `must be based` is in `REMOTE_NEGATIVE_HINTS` and
  that list is matched against the location field. The product invariant in
  CLAUDE.md names that exact phrase class as a positive signal and says it
  must never be a filter. The comment immediately above the check explains
  this hazard and guards the description path only, so the author identified
  the risk and covered the wrong field. This is the phrasing an EU-permit
  posting uses, which makes it the phrasing the candidate most needs to see.
- `Remote - US` **passes** and collects the region bonus, because
  `REGION_NEGATIVE_HINTS` has no shape matching a bare US remote posting.
- `Remote - Jerusalem` and `Remote, Siracusa, Italy` are **rejected** as
  non-EMEA, because `usa` is matched unanchored inside those words.

So the gate admits the out-of-scope postings and rejects two classes of
in-scope ones, including the class the project's own invariant singles out.

The board also reported that `Remote - Germany` and `Remote (Portugal)` fail
for a missing preferred region. They do not: both pass. That sub-claim is
withdrawn, and the working path is pinned by a test in this spec so it stays
working.

Found by the `principal-review` board (spec 028), screening lens, and
confirmed by direct execution rather than by reading.

## Scope

**Match words, not substrings.** Every keyword list that is currently matched
by containment becomes token-aware, so `usa` cannot match inside a city name
and `node` cannot match inside "Kubernetes nodes".

**Permit phrasing is a signal, never a filter.** The EU-permit phrase class
is excluded from the negative hints wherever they are applied, location field
included, and is scored as the positive signal the invariant describes.

**A non-EMEA lexicon that covers the real shapes.** Including bare country
and region forms as they appear in ATS location strings, so a US-only remote
posting is rejected on its location rather than admitted by a description
mentioning a global team.

**Multi-location strings are evaluated per location.** Providers join several
locations into one field; today any single matching token rejects the whole
posting, so one out-of-scope office removes an otherwise valid multi-region
role.

**The remote signal is read for every source.** It is currently consulted
for one source only, so a posting from a genuinely remote-only board is
rejected for a missing remote signal. Related: one importer discards the
provider's own workplace-type field whenever a city is present, which throws
away the most reliable remote evidence available.

**An inert policy switch is resolved.** `candidate.remote_only` sits in the
configuration and is read by nothing: flipping it changes no decision, which
was verified by running the gate both ways. A key that reads like a switch
and silently does nothing is a trap, so it is either wired to the gate or
removed from the configuration. Found while reviewing spec 031 (PR #33),
where adding it to the policy fingerprint would have been wrong precisely
because it decides nothing.

**One place that decides.** The gate order and the fields each rule reads are
stated in one table in the module, because the current defect is precisely
that one rule reads a field its own comment says it must not.

## Inputs, outputs, failure modes

- Inputs: title, location, description, and the candidate configuration.
- Outputs: an allow or reject with the rule that decided, unchanged in shape.
- The cases that must hold, drawn from real ATS phrasings and from the
  product invariant:

  | Location string | Verdict |
  |---|---|
  | `Remote (must be based in the EU)` | allow, positive signal |
  | `Remote - EU (must be based in Europe)` | allow, positive signal |
  | `Remote - Germany`, `Remote (Portugal)` | allow |
  | `Remote, Europe` | allow |
  | `Hybrid - Berlin`, `On-site, Madrid` | reject, not remote |
  | `Remote - US`, `Remote (United States)`, `Remote, US` | reject, non-EMEA |
  | `Remote - Jerusalem`, `Remote, Siracusa, Italy` | allow |
  | `Remote - Berlin or Remote - New York` | allow, one location qualifies |

- Failure mode this must not introduce: widening the negative hints to the
  description. The existing comment is right about why, and its two proving
  tests stay.
- Failure mode this must not introduce: a lexicon that silently shrinks
  coverage. Every row above is a test, and the reject rows matter as much as
  the allow rows.
- This change is retroactively worthless without spec 031, because every
  posting these rules already rejected is permanently suppressed. Sequence
  031 first.

## Acceptance criteria

Proven by services/api/tests/test_screening_location.py:

| Criterion | Proof |
|---|---|
| every row of the table, allow and reject alike | `test_the_eu_permit_phrasing_is_allowed`, `test_a_remote_european_location_is_allowed`, `test_a_non_remote_location_is_rejected`, `test_a_non_emea_remote_location_is_rejected`, `test_a_city_containing_usa_is_not_read_as_non_emea` |
| no keyword list matched by bare substring | `test_matching_is_by_word` (nine cases), `test_a_title_is_not_excluded_by_a_substring` |
| the permit class scores and never filters | `test_the_eu_permit_phrasing_is_allowed`, `test_stripping_removes_the_permit_phrase_and_leaves_the_rest`, `test_stripping_leaves_a_genuine_hybrid_marker_alone` |
| a multi-location string qualifies on any one | `test_a_multi_location_posting_qualifies_on_any_one_location`, and the two negative halves |
| the two description-scope tests still pass | `test_hybrid_wording_in_description_does_not_reject`, `test_must_be_based_in_eu_description_stays_accepted`, unchanged |
| the gate order is stated and asserted | `test_the_gate_order_is_what_the_module_declares` |
| the remote signal is read for every source | `test_a_remote_only_board_signal_is_honoured`, `test_a_source_signal_does_not_override_a_non_remote_location` |
| the provider workplace type is not discarded | `harrier/sources/lever.py` now joins both fields |
| `candidate.remote_only` decides or is gone | `test_the_example_configuration_has_no_inert_remote_only_switch` |
| no real posting or board in a fixture | every location string here is a shape, not an observation (ADR-008) |

Two things found while implementing, both decided with a test.

**A comma does not split a location.** The first version split on commas as
well as on the alternative separators, and `On-site, Madrid` then evaluated
"On-site" and "Madrid" independently: the modality carried no place, the
place carried no modality, and both passed. `Remote, US` passed the same way.
A comma qualifies a location; only `|`, `;`, `/` and " or " offer an
alternative.

**An explicit source signal does not override a non-remote location.** The
location gate runs first on purpose, so a board-wide claim about remoteness
cannot admit a posting whose own location says on-site.

The board's second sub-claim is withdrawn and pinned instead:
`Remote - Germany` and `Remote (Portugal)` do pass, and did before this
change. `test_a_remote_european_location_is_allowed` keeps them passing.

- [x] every row of the table above has a test, allow and reject alike
- [x] no keyword list is matched by bare substring containment
- [x] the EU-permit phrase class scores as a positive signal and is not
      matched by any negative hint on any field
- [x] a multi-location string qualifies when any one of its locations does
- [x] the two existing description-scope tests still pass unchanged
- [x] the gate order and the fields each rule reads are stated in the module
      and asserted by a test, so a rule reading a field it should not fails CI
- [x] `candidate.remote_only` either changes a decision or is gone, with a test either way
- [x] a remote-only posting from every source passes the remote signal check
- [x] a provider workplace-type field is not discarded when a city is present
- [x] no real posting, company, or board name enters a fixture or a test name
      (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028, plus direct execution of
`remote_region_allowed` against the strings above. Three of the board's four
sub-claims reproduced; the fourth is withdrawn above and pinned as a
regression test instead.

## Out of scope

The score cutoff and the bonus arithmetic, which are spec 033. The seen-state
change that makes this reach existing rows, which is spec 031. Location
parsing into structured geography.
