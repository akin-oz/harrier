---
spec: 031
title: A screening decision can be reconsidered when the rules change
status: accepted
approved: yes
milestone: M6
depends: [007, 011]
---

# Spec 031: A screening decision can be reconsidered when the rules change

## Problem

services/api/src/harrier/screening/pipeline.py adds a posting's key to the
seen set before any gate has decided anything, and that set is the persisted
one: discovery loads it with `load_seen_keys` and writes it back with
`save_seen_keys`. So a rejection is permanent. The next run skips the posting
as already seen, and no change to the rules can ever reach it.

Nothing records why. The stored payload is keys and a timestamp, with nothing
about the policy that produced the decision, so it is not possible to ask
which postings were rejected under a rule that has since been corrected.
`write_rejected_debug` exists and no caller ever passes `True`, so a
rejection leaves one integer in a per-run counter.

This makes every screening fix retroactively worthless, which matters
because spec 032 is a set of screening fixes. Correcting the location gate
changes nothing for any posting already rejected by it.

Re-opening a rejection is cheap: nothing is fetched at the point of
reconsideration, because the postings themselves were never stored. Only
their keys were.

Found by the `principal-review` board (spec 028), screening lens, which
marked every other screening finding void until this is fixed.

## Scope

**Record the decision, not just the sighting.** The seen entry carries the
verdict, the gate that produced it, and a policy version. A rejection is a
decision with a reason attached, which is what makes it reviewable.

**A policy version that changes when the policy does.** Derived from the
screening configuration and rule set so it cannot be forgotten: any change to
the weights, the keyword lists, or the gate order produces a different
version.

**Reconsider on demand.** A command *clears* the recorded decision for
postings rejected under a policy version other than the current one, and
reports what it cleared. Clearing rather than re-screening, and the
distinction is the whole contract: only keys are stored, not postings, so
this layer can make a posting eligible again and the next discovery run is
what fetches and judges it under the current rules. That also disposes of the
posting that no longer exists, which is simply not fetched.

Not automatic, because clearing after every configuration edit would surprise
the operator with a burst of tracker rows from decisions they thought were
settled. They ask for it.

**Mark seen after the decision, with the decision.** A posting suppressed
before it has been judged cannot be judged later.

## Inputs, outputs, failure modes

- Inputs: the persisted seen state and the current screening configuration.
  Nothing else: reconsideration reads no postings and makes no request.
- Outputs: seen entries carrying a verdict and policy version; a report of
  how many decisions were examined, how many were stale, how many were
  cleared, and how many were left alone because the operator had rejected
  them by hand. Cleared entries are removed from the state file, which is
  what makes the posting eligible on the next run.
- Migration: existing entries carry no verdict and no version. They are
  treated as decided under an unknown policy, which makes them eligible for
  the first reconsideration rather than invisible to it. Read as unknown, not
  as rejected, so the migration cannot resurrect a posting into the tracker
  without a decision.
- Failure mode this must not introduce: reconsideration that re-adds a
  posting the operator manually rejected. A human decision outranks a rule
  and is never overturned by a policy change.
- Failure mode this must not introduce: unbounded growth. The seen state
  gains fields, so its size bound is revisited in the same change. Eviction
  is currently by lexicographic hash order, which is stable and therefore
  evicts the same entries forever while keeping genuinely stale ones; it
  becomes age-based.

## Acceptance criteria

Proven by services/api/tests/test_seen_policy.py:

| Criterion | Proof |
|---|---|
| marked seen only after a verdict, with the verdict | `test_a_posting_is_recorded_only_after_a_verdict_exists`, `test_an_accepted_posting_records_its_acceptance`, `test_the_reason_recorded_is_the_gate_that_decided` |
| any weight or keyword change moves the version | `test_changing_a_scoring_weight_changes_the_version`, `test_changing_any_deciding_key_changes_the_version` (one case per configuration path), `test_the_domain_bonus_is_part_of_the_version`, `test_changing_a_rule_table_in_code_changes_the_version`, `test_changing_the_domain_keyword_table_changes_the_version` |
| the deciding paths describe the real configuration | `test_every_deciding_path_exists_in_the_real_configuration` |
| a recorded reason is groupable | `test_the_recorded_reason_is_a_stable_slug` |
| a corrupt state file is reported | `test_a_corrupt_state_file_is_reported_rather_than_silently_empty`, `test_a_state_file_that_is_not_an_object_is_reported` |
| stale rejections reconsidered, current ones not | `test_a_rejection_under_an_older_policy_is_cleared`, `test_a_rejection_under_the_current_policy_is_left_alone` |
| a manual rejection is never resurrected | `test_a_job_the_operator_rejected_is_never_resurrected`, `test_the_protection_matches_on_company_and_title_too` |
| pre-change entries are unknown and eligible | `test_the_old_format_migrates_to_unknown_rather_than_being_discarded`, `test_a_migrated_entry_is_eligible_for_the_first_reconsideration`, `test_a_migrated_entry_is_never_read_as_an_acceptance` |
| eviction is age-based | `test_eviction_keeps_the_newest_not_the_lexicographically_largest` |
| no network on reconsideration | `test_a_rejection_under_an_older_policy_is_cleared` runs with no network stubbed and no fetch reachable: `reconsider_source` imports no HTTP client and reads only the state file and the tracker |
| no personal data committed | `services/api/tests/test_publishable.py::test_no_fixture_names_a_real_company` covers the committed fixtures; the seen state itself lives under the data directory, which `.gitignore` excludes and `config/data-classification.json` marks never-in-git (ADR-008) |

One design point the spec left open and the implementation decided:
reconsideration **clears** stale rejections rather than re-screening them.
The postings themselves are not stored, only their keys, so the most this
layer can do is make them eligible again; the next discovery run fetches and
judges them under the current rules, which is where the decision belongs.
That also removes the question of what to do with a posting that no longer
exists: it simply is not fetched.

Review on PR #33 found the fingerprint reading flat configuration keys that
the real file has never contained, so the filter dropped every one and the
digest reduced to the scoring block plus the compiled tables. Changing the
title keywords or the preferred countries moved no version and freed no
stale rejection, and the test passed because it inserted a flat key a real
configuration does not have. The paths are now nested and one test asserts
every one of them exists in the real configuration, which is what turns the
list from a guess into a claim.

An acceptance is never reconsidered
(`test_an_acceptance_is_never_reconsidered`). It already produced a tracker
row, so re-running it would at best do nothing and at worst duplicate it.

- [x] a posting is marked seen only after a verdict exists, and the verdict is
      stored with it
- [x] changing any screening weight or keyword list changes the policy version
- [x] reconsideration clears rejections from an older policy version and
      leaves current-version rejections untouched
- [x] a manually rejected job is never resurrected by reconsideration
- [x] entries written before this change are treated as unknown policy and
      are eligible, and none of them enters the tracker without a fresh
      decision
- [x] eviction is age-based, proven by a test where the stable-hash rule would
      evict the wrong entry
- [x] reconsideration performs no network request when the description cache
      covers the postings
- [x] no company name, posting title, or URL enters any committed file (ADR-008)
- [ ] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The premature `add`, the payload
shape, and the unreachable `write_rejected_debug` argument are verifiable in
the tree.

## Out of scope

Changing any screening rule, which is spec 032. Storing full postings for
rejected rows: the keys and the verdict are enough to reconsider, and the
posting is refetchable.
