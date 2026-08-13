---
spec: 044
title: The repository stops describing one person's real job search
status: in-progress
approved: yes
approved-note: >
  Approved by Akin in session on 2026-08-13, verbally rather than by editing
  this file. Recorded here because the agent normally never sets this flag,
  and a reader is entitled to know which kind of approval this was.
milestone: M7
depends: [003, 021, 038]
---

# Spec 044: The repository stops describing one person's real job search

## Problem

The repository is public and the subject is a person. An open-source readiness
review found that it leaks the maintainer's real job search in four distinct
ways, and that the checks meant to prevent this cannot see three of them.

The leaks are not credentials and not names in fixtures, which is the class the
existing pass already catches. They are:

- **Paths that are personal but unclassified.** The README tells a reader to
  copy every `config/*.example.*` to its real name. Ten example files exist;
  four of the real names were gitignored. The other six hold the candidate
  profile, the resume truth content, the application narrative in two formats,
  the interview story seeds, and the outreach defaults, and they staged
  cleanly under `git add -A`.

- **Never-in-git directories produced outside the ignored root.** `data_dir()`
  resolved `data/` against the process working directory. `just export` runs
  from `services/api`, so it opened a second database, wrote a header-only CSV,
  and left a `services/api/data/` holding a log of a never-in-git class that
  `git check-ignore` did not match. Two databases where ADR-003 says one.

- **A real employer in the test suite.** Three real employers, a real
  recruiting mailbox, a real posting title, a real posting id, and the subject
  and body of a real application acknowledgement, across five test files.
  Three of those files say in their own docstrings that they were ported onto
  the synthetic persona, so a scrub was intended and these survived it.

- **Aggregates measured from the real tracker.** Row counts, a contacts count,
  an evaluation-report count, a description-cache file count, and the size of
  the accumulated interview material, in ADRs, specs, the parity matrix and its
  generated checklist. Each is an observation of what one person's data
  happened to contain, not a rule the code must satisfy.

And the reason they survived: the mechanical privacy pass reads `fixtures/**`
and `config/*.example.*` and nothing else, so the test tree was never scanned.
Every check it does run matches a named entity, which is the class that stopped
recurring. The class that keeps recurring is the aggregate, which names nobody
and describes the search exactly, and it had no check anywhere.

## Scope

**Every personal path is classified and ignored, derived from the tree.** The
set comes from the example files that exist rather than a hand-maintained list,
so a new example file with no classification entry fails instead of leaking.

**`data_dir()` anchors to the repository root.** One database, wherever the
process is started from, which is what ADR-003 already says.

**The test suite names only synthetic employers**, against a declared
allowlist. A real employer entering the suite fails until someone adds the
slug, which makes it a reviewable act rather than a silent omission.

**No committed prose states an aggregate of the real search.** A count paired
with a tracker entity is flagged; a cap or a page size is a specification and
is exempt by its qualifier.

**The resume timeline and the compensation figures in the example config are
synthetic.** The employers were already scrubbed and the dates were not, which
is the documented recurrence shape: the names go, the numbers stay. The
most-recent end date disclosed when the current search began.

## Inputs, outputs, failure modes

- Inputs: the repository as it would be published.
- Outputs: `.gitignore` and `config/data-classification.json` covering every
  personal path; a root-anchored `data_dir()`; a scrubbed test suite, example
  config, and document set; a privacy pass whose scope includes the tree where
  the leak actually was.
- Failure mode this must not introduce: a check that reports success because
  its file set is empty. Both new scopes assert a non-empty file set first,
  the same guard the existing pass already carries.
- **The detector's input set is defined rather than assumed.** It reads every
  tracked Markdown file and every tracked Python file under the service source
  and test trees, which is where prose in this repository lives. Generated
  files are read like any other, because `docs/parity-checklist.md` is
  generated and carried an aggregate. The exempting qualifiers are enumerated
  in the test next to the pattern rather than described here, so the two
  cannot drift.
- The set is derived from `git ls-files` rather than listed, and a test fails
  when a tracked prose file is outside it. The earlier version hand-listed
  three directories, which silently excluded `README.md`, `CONTRIBUTING.md`,
  `SECURITY.md` and everything under `apps/`: the non-empty-file guard passes
  happily on an input set that is simply too small, which is the same
  "reports success while doing nothing" shape this work exists to remove.
- Honest limitation: the aggregate detector is a regular expression over
  prose. It reads the shape "number followed by a tracker noun" and cannot
  judge whether a number is an observation or a coincidence. It will miss an
  aggregate phrased in words, and it will flag a specification phrased without
  one of the exempting qualifiers. It is a floor, not a proof.
- Second honest limitation: no tree-level change reaches git history. The
  blobs and the commit bodies are spec 046's problem, and until that lands the
  history still holds what the tree no longer does.

## Acceptance criteria

Ticked here, in the pull request that carries the code. They were unticked on
the specs-only pull request, because a reviewer there could not see anything
that earned them.

| Criterion | Proof |
|---|---|
| every example config's real name is classified | `services/api/tests/test_classification_coverage.py::test_every_example_config_has_its_real_name_classified` |
| data lands inside the repository from any cwd | `services/api/tests/test_classification_coverage.py::test_data_dir_is_inside_the_repository_whatever_the_working_directory` |
| the test tree is actually scanned | `services/api/tests/test_demo.py::test_the_test_tree_is_actually_scanned` |
| the suite names only synthetic employers | `services/api/tests/test_demo.py::test_the_test_suite_names_only_synthetic_employers` |
| the suite addresses only reserved domains | `services/api/tests/test_demo.py::test_the_test_suite_addresses_only_reserved_domains` |
| no committed prose states a real aggregate | `services/api/tests/test_demo.py::test_no_committed_prose_states_an_aggregate_of_the_real_search` |

- [x] the six unclassified personal config paths are gitignored and classified,
      from a list derived from the tree rather than maintained by hand
- [x] `data_dir()` resolves against the repository root, and the never-in-git
      probe for it is gitignored
- [x] no real employer, recruiting mailbox, posting title, or posting id
      remains in the test suite
- [x] the resume timeline and the compensation figures are synthetic
- [x] the aggregates are removed from the ADRs, the specs, the parity matrix,
      and its regenerated checklist
- [x] each new check fails against the state that preceded it, executed rather
      than asserted
- [ ] All gates green on PR

## Proof / origin

The `open-source-readiness` agent team (spec 028), privacy, fresh-clone,
claim-auditor and publishability lenses, run 2026-08-13. Two lenses proved the
classification gap independently with different path sets; three converged on
the `data_dir` working-directory defect from three different symptoms.

Two corrections to the review's own findings, recorded rather than absorbed.
It reported nine example files with five unignored; there are ten and six. It
named three test files carrying the real employer; there are five, and the two
it missed carry a real posting id in a source docstring and in the assertion
that pins it.

## Out of scope

The absent log redaction filter, the guards that fail open, the untested
decisions, and the false claims in the documents (spec 045). Git history and
the published pull request bodies (spec 046).
