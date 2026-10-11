---
spec: 031
title: A screening decision can be reconsidered when the rules change
status: shipped
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
| a corrupt state file is reported | `test_a_corrupt_state_file_is_refused_rather_than_read_as_empty`, `test_a_state_file_that_is_not_an_object_is_refused` |
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
- [x] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028. The premature `add`, the payload
shape, and the unreachable `write_rejected_debug` argument are verifiable in
the tree.

## Out of scope

Changing any screening rule, which is spec 032. Storing full postings for
rejected rows: the keys and the verdict are enough to reconsider, and the
posting is refetchable.

## Amendment (2026-10-11): a rejection made on one source protects the posting on every source

Akin approved this section by merging PR #252 on 2026-10-11. It is built:
`posting_identities` in `services/api/src/harrier/screening/seen.py`
computes the hashes, the industry `record` in `screen_jobs` stores them,
and `human_rejected_identities` in
`services/api/src/harrier/screening/reconsider.py` matches them. Each hash
is taken over a kind tag as well as the value (`stable_key("url", ...)`,
`stable_key("company_title", ...)`), so a url can never equal a company
and title by accident.

### Problem

`reconsider_source` (`services/api/src/harrier/screening/reconsider.py`)
protects a stale seen entry when its key equals a key rebuilt from a
rejected tracker row (`human_rejected_seen_keys`, PRs #233, #238, #248).
A key is a hash of the source, the board and the posting's identity
(`make_normalized_job` in `services/api/src/harrier/screening/normalized.py`),
and a row records only the source it came from. So the rebuild can only
reach the row's own source.

The same posting is often listed on more than one source: a company's
Greenhouse board and a LinkedIn listing of it. Say the operator rejects the
Greenhouse row. The LinkedIn listing was seen, and rejected as
`tracker_duplicate` or by a gate, under a policy that has since changed.
Reconsideration clears the LinkedIn entry and counts it as eligible again.
That is the failure mode this spec names: a rule change overturning the
operator's decision. The tracker dedupe gate in
`services/api/src/harrier/screening/pipeline.py` still stops the posting
from becoming a second row, so the visible effects are a wrong report and
churn in the seen file. But the guard this spec promises is not the one
doing the work.

Nothing in a seen entry can be compared with a row from another source,
because an entry stores only the hashed key and its decision.

### Behavior after the change

- **Recording.** Every decision the industry pipeline records (`record` in
  `screen_jobs`, `services/api/src/harrier/screening/pipeline.py`) also
  stores the posting's identities: a hash of its normalized url, and a hash
  of its normalized company and title joined by `|`. These are the same two
  identities `human_rejected_keys` computes for a row, and the same ones
  tracker dedupe matches on. A posting with no url stores only the company
  and title hash. A posting with neither stores none. The hash is
  `stable_key`, so a stored identity is 20 hex characters and no posting
  text reaches the file.
- **Reconsidering.** A stale entry is protected when its key matches, as
  today, or when any of its stored identities equals the hash of an
  identity of a rejected row on the same track, whatever that row's source.
  It is counted in `protected` and stays in the seen file.
- **Reading and writing.** `load_seen` reads `identities` when present and
  reads an entry without the field as having none. `save_seen` writes the
  field only for an entry that has identities. The academic pipeline
  (`_screen_academic`) records none: its track has one source, so it has
  no cross-source case, and its protection stays `protected_seen_keys`
  (spec 097).

### Failure modes

- **An entry recorded before this change** has no identities. It keeps key
  protection only. If its posting was rejected on another source, the first
  reconsideration still clears it. The next discovery run that sees the
  posting decides it again, at the latest at the tracker dedupe gate, and
  records that decision with identities, so this heals after one run. It is not backfilled, because
  the postings are not stored.
- **The operator edits a row's url, company or title after the decision.**
  The row's identities are read at reconsideration time and the entry's at
  decision time. If both identities were edited, the entry is not protected.
- **Two different postings with the same company and title.** One rejection
  protects the other's stale entry. Tracker dedupe already treats a
  matching company and title as the same posting (`find_duplicate` in
  `services/api/src/harrier/tracker/store.py`), so the other posting could
  not become a row anyway.
- **A file written by this code and read by older code.** The older reader
  ignores the field (`_decision_from` in
  `services/api/src/harrier/screening/seen.py` reads fields by name), and
  its next save drops it. A downgrade loses identities but keeps every
  decision.
- **Size.** `SEEN_CAP` stays a count of entries. An entry grows by at most
  two 20-character hashes, a fixed amount whatever the posting's length.
  Eviction stays age-based
  (`test_eviction_keeps_the_newest_not_the_lexicographically_largest`).
- **A damaged seen file.** Unchanged: refused, never read as empty (spec 040).
- **Dry run.** Unchanged: it reports and writes nothing.

### Acceptance criteria

- An industry rejection is recorded with the url hash and the company and
  title hash of its posting; a posting without a url gets only the latter
  (`test_a_rejection_records_the_posting_identities`).
- A rejected `greenhouse` row and a stale `apify_linkedin` entry with the
  same url: the entry is protected and kept, and `protected == 1`
  (`test_a_posting_rejected_on_another_source_is_protected`).
- The same case matched by company and title alone, with different urls
  (`test_a_cross_source_match_on_company_and_title_is_protected`).
- The same identities on a shortlisted row protect nothing: the entry is
  cleared (`test_identities_of_a_row_not_rejected_protect_nothing`).
- An entry without identities is still protected by its key and cleared
  across sources
  (`test_an_entry_without_identities_keeps_key_protection_only`).
- Identities survive a save and load, and a file from before this change
  loads with none (`test_identities_round_trip`,
  `test_a_file_from_before_identities_loads_without_them`).
- The written seen file contains neither the posting's url nor its company
  or title as text (`test_the_seen_file_holds_no_posting_text`).
- The academic track is unchanged:
  `test_reconsider_never_reopens_a_human_rejection_on_an_academic_track`
  and the existing reconsideration tests in
  `services/api/tests/test_seen_policy.py` pass unmodified.

### What changes

- `services/api/src/harrier/screening/seen.py`: `SeenDecision` gains
  `identities`, and its read and write.
- `services/api/src/harrier/screening/pipeline.py`: the industry `record`
  passes the posting's identities.
- `services/api/src/harrier/screening/reconsider.py`: the hashed identities
  of rejected rows join the protection check. One function computes a
  posting's identities, used by both recording and reconsideration, so the
  two cannot drift.
- `services/api/tests/test_seen_policy.py`: the tests above.

### Out of scope

- Backfilling identities into existing entries. The postings are not stored.
- The academic track, for the reason given above.
- Narrowing protection by who rejected the row (spec 079's actor). Decided
  on 2026-10-11 to keep protecting every `rejected` row.
- Changing tracker dedupe, or any screening rule.
- Storing posting text in the seen file.

### Migration

None for the operator. Existing seen files load unchanged. Entries gain
identities as discovery records new decisions. No command needs running.

### Limitations

An entry recorded before this change stays unprotected across sources until
the posting is seen again. A posting whose url and company and title were
all edited on the row after the decision is not matched.

### Decisions (Akin, 2026-10-11)

1. **Hashed identities, not plain text.** The seen file keeps holding keys
   only, as this spec says it does, and the entry size stays fixed. Posting
   text stays out of a file that spec 107 copies into hosted storage.
2. **Identities on every decision, not only rejections.** Only rejections
   are reconsidered, but one code path for `record` is worth two hashes per
   accepted entry.
3. **Url and company plus title, not the url alone.** Company and title
   catches the same role listed under different urls, which is the common
   cross-source case. It also protects a second posting with the same title
   at the same company, which tracker dedupe already treats as a duplicate.

### Proof / origin

The cross-source gap was named on PR #233 and in its commit message, when
the same-source fix landed. Akin asked for this amendment on 2026-10-11.
