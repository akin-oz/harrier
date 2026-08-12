---
spec: 041
title: The smaller review corrections, gathered
status: accepted
approved: yes
milestone: M6
depends: [028]
---

# Spec 041: The smaller review corrections, gathered

## Problem

Four findings from the `principal-review` board (spec 028) that are real,
independent of each other, and too small to carry a specification each. They
are gathered here rather than dropped, because the board's own point about
the nine missing CLI verbs is that nothing looks at the seams, and a finding
with no home is a finding that will be rediscovered.

**An unroutable board URL is silently discarded.** The router has a branch per
supported provider and no final branch, so a watchlist entry for any other
provider produces no error and no jobs, forever. Spec 025 made the feed health
report name such an entry, and fixed a pruning defect that deleted them, but
discovery itself still drops them without a word. For a single-user tool where
the watchlist is edited by hand, this is the most likely real-world failure in
the system.

**Run state is per-process.** The one-active-run invariant is held in process
memory, so it holds per worker rather than per machine, and a reloading
development server orphans subprocesses that the journal then reports as
failed. The invariant is stated more strongly than it is enforced.

**The scope column is speculative generality, in the wrong table.** It is
threaded through eight signatures and has never been passed a value other
than the default. It guards the configuration table, which holds no personal
data, while the tables that do hold personal data have no equivalent. Either
it earns its place across the schema or it leaves the one table it is in.

**A spec shipped with a destroyed heading.** One approved spec has a section
heading collapsed into an acceptance criterion, which left two of its findings
orphaned. It passed both the spec gate and the generated-artifact check,
because neither reads a spec's structure.

## Scope

**A watchlist entry that routes nowhere is reported.** At configuration time
if possible, at run time otherwise, and never silently. The operator finds out
that the URL they pasted is not supported, rather than concluding the tool is
broken.

**The run invariant matches its enforcement.** Either the active-run state
moves somewhere shared, or the invariant is restated as per-process and the
development-server case is handled so an orphan is not reported as a failure.

**The scope column is resolved in one direction.** Extended to the tables
that hold personal data, or removed from the one that does not. Not left as
a parameter that exists to be defaulted.

**Spec structure is checked.** The required headings are asserted, so a spec
whose section headings are damaged fails rather than merging.

## Inputs, outputs, failure modes

- Inputs: the watchlist, the run journal, the database schema, the spec files.
- Outputs: an error where there was silence, a schema that means one thing,
  and a structural check on specs.
- Failure mode this must not introduce: refusing to run because one watchlist
  entry is unsupported. The entry is reported and skipped; the rest of
  discovery proceeds.
- Failure mode this must not introduce: a heading check so rigid that a spec
  cannot add a section. The check asserts that the required headings are
  present, not that no others are.
- The scope decision is genuinely open. Removing it is the smaller change and
  the spec does not presume the answer, but it does require an answer.

## What the implementation decided

**The scope column is removed, and ADR-009 is amended to say so.** It never
held anything but `default`, and it guarded `user_config`, which holds no
personal data, while `jobs` and `contacts` had no equivalent. Extending it
would add an unused column to every personal-data table: more speculative
generality, not less. Re-adding it later is a migration, and the ADR now
records that as the accepted cost.

**The run invariant is restated rather than moved.** It is one active run per
kind **per process**, which is what the in-memory registry actually enforces.
The deployment is a single uvicorn worker on the operator's laptop, where per
process and per machine are the same thing; a second worker would need the
registry in SQLite, and that is written down rather than done.

A run left non-terminal by a vanished process is now `interrupted`, not
`failed`. Calling it failed was a guess dressed as a fact, and a reloading
development server produced one on every reload. This adds a state to the
published contract, which is why it is named here.

## Found while implementing

Three open branches each added a **migration 4**: `job_runs` (spec 029),
dropping `manual_reject` (spec 036), and dropping `scope` (this one). The
runner skips any version at or below the recorded one, so two of the three
would never have run, and the symptom would have been a missing column long
after the merge. The branches are renumbered 4, 5 and 6, and
`tests/test_userconfig.py::test_every_migration_version_is_unique_and_ordered`
stops the next one being found in production. Recorded here rather than
absorbed silently, because it is not one of the four findings above.

## Acceptance criteria

- [x] a watchlist entry on an unsupported host produces a reported error and
      does not prevent the other sources running
      (`services/api/tests/test_sources.py::test_an_unroutable_entry_is_kept_rather_than_dropped`,
      `::test_the_unrouted_key_is_never_treated_as_an_importer`,
      `services/api/tests/test_discovery.py::test_an_unroutable_watchlist_entry_is_reported_and_the_run_continues`)
- [x] the active-run invariant is enforced as strongly as it is stated, or
      restated to match, with a test either way. Restated to per-process, in
      the `RunManager` docstring.
- [x] a reloading development server does not leave a run recorded as failed
      (`services/api/tests/test_runs.py::test_journal_marks_an_orphaned_run_interrupted_not_failed`,
      `::test_an_interrupted_run_does_not_block_the_next_one`,
      `::test_a_real_failure_is_still_recorded_as_failed`)
- [x] the scope column is absent from the configuration table, and no
      signature carries a parameter that is never passed
      (`services/api/tests/test_userconfig.py::test_the_schema_carries_no_scope_column`,
      `::test_a_kind_is_unique_on_its_own`)
- [x] a spec missing a required heading fails CI, proven against a fixture
      with a damaged heading
      (`services/api/tests/test_spec_structure.py::test_a_damaged_heading_fails`,
      `::test_every_required_heading_is_checked`,
      `::test_a_heading_inside_prose_does_not_count`,
      `::test_a_spec_may_add_sections`)
- [x] the spec that shipped damaged is repaired and its two orphaned findings
      are restored. Spec 006 lost its whole `## Problem` section in the
      rewrite before implementation, taking both of its claims with it: that
      ADR-004 is what requires start, progress, logs and cancel, and that the
      seam is proved by a real run rather than a mock. Both are restored, and
      `::test_the_committed_specs_all_carry_their_headings` keeps every spec
      whole.
- [x] no watchlist content appears in an error message written to a committed
      file (ADR-008). The unrouted entries are logged and written to the run
      summary under `data/incoming/`, which is gitignored. Limitation: this
      is a property of where the file is written, not something a test
      asserts about the repository.
- [x] All gates green on PR

## Proof / origin

The `principal-review` board, spec 028, architect lens. Each of the four is
verifiable in the tree: the router's missing final branch, the in-process run
registry, the unused scope parameter, and the collapsed heading.

## Out of scope

The feed health command itself, which is spec 025 and shipped. Provider
support for any additional job board.
