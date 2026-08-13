---
spec: 045
title: The repository's claims are true and its gates actually gate
status: in-progress
approved: yes
approved-note: >
  Approved by Akin in session on 2026-08-13, verbally rather than by editing
  this file. Recorded here because the agent normally never sets this flag.
milestone: M7
depends: [029, 035, 039, 043, 044]
---

# Spec 045: The repository's claims are true and its gates actually gate

## Problem

Spec 044 removed what the repository said about one person. This one removes
what it says about itself that is not true, and repairs the checks that report
success while doing nothing. The two are the same defect seen twice: a claim
with nothing behind it.

**A documented privacy control that was never built.** `docs/privacy-plan.md`
and the compiled privacy rule both stated that logging loads identity values at
startup and installs a redaction filter. There was no `logging.Filter` anywhere
in the tree. `logsetup` also said it was called by the CLI and by the API; only
the CLI called it, so the process serving the browser had no configured root
logger at all.

**Guards that fail open**, each proven by construction:

- `verify-on-stop.sh` uses `git diff --name-only HEAD`, which omits untracked
  files, so a turn that only adds new files runs no gate.
- `guard-commit.sh` denies `--no-verify` and a standalone `-n` but allows the
  bundled `-nm` form git accepts, and never inspects `-c core.hooksPath`,
  which disables the whole chain.
- `guard-source-of-truth.sh` pauses on CI workflows but not the scripts they
  run, nor the justfile, lefthook config, or specs; a `./` prefix defeats the
  match; and because the settings file matches only `Edit|Write|MultiEdit`,
  every guarded path stays writable through Bash.
- `check_spec_structure.py` uses a non-recursive glob and reports success on
  zero specs found.
- `spec-gate.yml` triggers on `pull_request` only, so a direct push to `main`
  skips trailer resolution and structure entirely.

**Decisions no test executes**, proven by mutation with the suite staying
green: the `except BackupError` arm of `_cmd_verify_backup` can return 0 so a
corrupt archive reports success; `_cmd_cutover` can be a no-op;
`review-followup`'s exit codes 2 and 3 can be disabled, which is the mechanism
the review-response rule is built on; and `validate_rendered_pdf`, the artifact
gate named in the product invariants, is replaced by a fake in every test, so
its replacement-character, placeholder and page-count checks were all disabled
at once without a failure.

**Claims contradicted by the code**: spec criteria naming tests that do not
exist; parity rows marked `keep` that later specs deliberately changed; README
and architecture statements about auth, config scope, milestones and package
names that shipped work has since falsified; assertions that cannot fail.

## Scope

The redaction filter is built rather than the sentence deleted, because a
showcase repository that admits a gap is better than one that claims a control,
and a control here is cheap. Each guard is closed at the mechanism the proof
used, not at the symptom. Each untested decision gets a test that exercises the
decision rather than the helper it calls. Each false claim is corrected against
the code, or the code is corrected against the claim, whichever is right, and
the choice is stated.

Out of scope by deliberate split: anything requiring a history rewrite.

## Inputs, outputs, failure modes

- Inputs: the repository, the readiness findings, and the mutations that
  proved them.
- Outputs: `harrier/logredact.py` and its installation; repaired guard
  scripts and workflow triggers; tests over the previously unexecuted
  decisions; corrected documents.
- Failure mode this must not introduce: a guard that is stricter than the
  workflow it protects and blocks ordinary work. Each guard change is
  exercised against both the bypass it now denies and a normal invocation it
  must still allow.
- **Redaction covers late contacts and short identities**, both of which an
  earlier draft of this spec accepted as limitations. Neither is acceptable:
  the compiled privacy rule says logs redact candidate and contact identity
  values, with no exemption for a two-letter name or for a contact added five
  minutes after the process started, and the API is a long-running process
  where "added later" is the normal case rather than the edge.
  - The identity set is refreshed from the one tracker write path (ADR-003)
    rather than read once at startup, so a contact is redactable from the
    moment it exists. Refreshing there rather than per log record keeps the
    database off the logging path, where a query that failed would log and
    recurse.
  - Short values are matched on word boundaries instead of being skipped, so a
    two-letter name is redacted where it stands alone and does not shred every
    unrelated line that happens to contain those letters. That was the real
    reason for the length floor, and a boundary match answers it without
    giving up the redaction.
- Honest limitation that remains: redaction matches literal values. A
  paraphrase or a different spelling of the same name is not caught, and no
  value-matching filter would catch it. Logs are never-in-git regardless; this
  defends the log that leaves the machine by hand.
- Honest limitation on the untested decisions: a test proves the decision runs
  and returns what it should. It does not prove the surrounding command is
  correct, and 26 CLI handlers still have no executed lines.

## Acceptance criteria

Unticked for the same reason as spec 044: this is proposed alone so the gate
has an approved spec on the base, and the implementation with its proofs lands
in the stacked pull request, where the boxes are ticked.

| Criterion | Proof |
|---|---|
| identity values are read from candidate and contacts | `services/api/tests/test_logging.py::test_identity_values_reads_candidate_and_contacts` |
| a missing profile store degrades rather than raises | `services/api/tests/test_logging.py::test_identity_values_survives_a_database_without_the_tables` |
| a short identity is redacted on a word boundary | `services/api/tests/test_logging.py::test_a_short_identity_is_redacted_on_a_word_boundary` |
| a short value inside another word is left alone | `services/api/tests/test_logging.py::test_a_short_value_inside_another_word_is_left_alone` |
| a contact added after startup is redacted | `services/api/tests/test_logging.py::test_a_contact_added_after_startup_is_redacted` |
| the candidate name does not reach a log line | `services/api/tests/test_logging.py::test_the_candidate_name_does_not_reach_a_log_line` |
| a contact address does not reach a log line | `services/api/tests/test_logging.py::test_a_contact_address_does_not_reach_a_log_line` |
| an unrelated line is untouched | `services/api/tests/test_logging.py::test_an_unrelated_line_is_left_alone` |
| the longest value is redacted first | `services/api/tests/test_logging.py::test_the_longest_value_is_redacted_first` |
| the API configures logging | `services/api/tests/test_logging.py::test_the_api_configures_logging_when_the_app_is_created` |

- [ ] the redaction filter exists, is installed by `configure_logging`, and
      the privacy plan names the test that proves it
- [ ] `create_app` configures logging, and a test fails if the call is removed
- [ ] each guard denies the bypass its proof used and still allows normal use
- [ ] the spec gate runs on push to `main`, not only on pull requests
- [ ] each mutation-proven decision has a test that fails when it is mutated
- [ ] every corrected claim names the file or test that now proves it
- [ ] All gates green on PR

## Proof / origin

The `open-source-readiness` agent team (spec 028), claim-auditor and
test-integrity lenses, run 2026-08-13. The redaction gap was ranked P0 by the
claim auditor and reduced to P1 on merge, on the grounds that logs are
never-in-git so nothing leaks on publication; it is fixed rather than
downgraded further because the claim itself was the defect.

## Out of scope

Git history, commit bodies, and published pull request descriptions (spec
046). Coverage for the remaining CLI handlers beyond the decisions named here.
