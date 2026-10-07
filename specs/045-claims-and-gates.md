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

Ticked here, in the pull request that carries the code, for the same reason as
spec 044.

| Criterion | Proof |
|---|---|
| identity values are read from candidate and contacts | `services/api/tests/test_logging.py::test_identity_values_reads_candidate_and_contacts` |
| a missing profile store degrades rather than raises | `services/api/tests/test_logging.py::test_identity_values_survives_a_database_without_the_tables` |
| a short identity is redacted on a word boundary | `services/api/tests/test_logging.py::test_a_short_identity_is_redacted_on_a_word_boundary` |
| a short value inside another word is left alone | `services/api/tests/test_logging.py::test_a_short_value_inside_another_word_is_left_alone` |
| a single-character value is ignored | `services/api/tests/test_logging.py::test_a_single_character_value_is_still_ignored` |
| a contact added after startup is redacted | `services/api/tests/test_logging.py::test_a_contact_added_after_startup_is_redacted` |
| the candidate name does not reach a log line | `services/api/tests/test_logging.py::test_the_candidate_name_does_not_reach_a_log_line` |
| a contact address does not reach a log line | `services/api/tests/test_logging.py::test_a_contact_address_does_not_reach_a_log_line` |
| an unrelated line is untouched | `services/api/tests/test_logging.py::test_an_unrelated_line_is_left_alone` |
| the longest value is redacted first | `services/api/tests/test_logging.py::test_the_longest_value_is_redacted_first` |
| the API configures logging | `services/api/tests/test_logging.py::test_the_api_configures_logging_when_the_app_is_created` |

- [x] the redaction filter exists, is installed by `configure_logging`, and
      the privacy plan names the test that proves it
- [x] `create_app` configures logging, and a test fails if the call is removed
- [x] each guard denies the bypass its proof used and still allows normal use
      (`services/api/tests/test_guards.py::test_the_commit_guard_denies_every_proven_bypass`,
      `services/api/tests/test_guards.py::test_the_commit_guard_allows_ordinary_work`,
      `services/api/tests/test_guards.py::test_the_turn_gate_gates_a_shell_guard_change`)
- [x] the spec gate runs on push to `main`, not only on pull requests, and
      refuses a base it cannot resolve rather than checking the tip alone
      (`services/api/tests/test_guards.py::test_the_spec_gate_refuses_a_base_it_cannot_resolve`,
      `services/api/tests/test_guards.py::test_a_null_base_checks_every_commit_not_on_main`)
- [x] each mutation-proven decision has a test that fails when it is mutated
      (`services/api/tests/test_cli_decisions.py::test_a_corrupt_archive_is_reported_as_a_failure`,
      `services/api/tests/test_cli_decisions.py::test_cutover_refuses_when_the_old_repo_is_absent`,
      `services/api/tests/test_cli_decisions.py::test_an_unreviewed_pull_request_exits_two`,
      `services/api/tests/test_cli_decisions.py::test_an_unanswered_finding_exits_three`)
- [x] every corrected claim names the file or test that now proves it
      (`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`, which also
      verifies the named file defines the named symbol, and
      `services/api/tests/test_demo.py::test_no_committed_file_names_an_absolute_home_directory`)
- [x] web test citations are checked in every shape the specs use, each
      name against the file it names, and a name marked planned is exempt
      only while no test has it (the 2026-10-06 amendment below;
      `services/api/tests/test_spec_structure.py::test_a_web_citation_naming_no_test_fails`,
      `::test_a_web_citation_naming_a_real_test_passes`,
      `::test_a_web_name_must_be_in_the_file_it_names`,
      `::test_a_web_citation_resolves_to_exactly_one_file`,
      `::test_planned_exempts_a_web_test_only_until_it_exists`)
- [x] a quoted name of an existing web test that no shape reads fails the
      check
      (`services/api/tests/test_spec_structure.py::test_a_quoted_web_test_name_with_no_file_fails`)
- [x] a Python continuation, a code span holding `::` and a symbol, is
      checked like a bare symbol
      (`services/api/tests/test_spec_structure.py::test_a_python_continuation_is_checked`)
- [x] the citations the wider check finds broken are corrected in specs 047,
      056 and 080, and
      `services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
      passes over every committed spec
- [x] every Python test that specs 016 and 018 to 026 cite outside a code
      span is cited in one, and spec 023's citation of a removed test says
      what replaced it (the amendment below on Python citations;
      `services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
      passes over every committed spec)
- [x] spec 023's Problem, Scope, resolution order and honest limitations
      describe `user_config` as spec 041 left it, one row per kind with no
      scope column, and each corrected sentence names the file or test that
      proves it (the amendment below on spec 023;
      `services/api/tests/test_userconfig.py::test_the_schema_carries_no_scope_column`,
      `::test_a_kind_is_unique_on_its_own`)
- [x] an existing Python test named outside a code span fails the check, and
      so does a name marked planned once its test exists, while file names,
      paths, longer words, fenced examples and names no test has pass (the
      amendment below on Python tests named outside a code span;
      `services/api/tests/test_spec_structure.py::test_a_python_test_named_outside_a_code_span_fails`,
      `::test_planned_exempts_a_python_test_only_until_it_exists`,
      `::test_a_fenced_example_is_never_a_citation`)
- [x] the committed specs name no existing Python test outside a code span,
      and
      `services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
      passes over every committed spec
- [x] spec 023's honest limitations say that config writes through the API
      need the local API token and a trusted Host header since spec 035,
      without calling the token authentication, and name the tests that
      prove it (the amendment below on API auth;
      `services/api/tests/test_api_exposure.py::test_a_state_changing_request_without_the_token_is_refused`,
      `::test_a_request_with_a_foreign_host_is_refused`)
- [x] an `-n` that belongs to another command in the same chain, or to
      commit message text, is allowed, while `git commit -n`,
      `git commit -nm "..."`, `--no-verify`, and
      `git -c core.hooksPath=... commit` stay denied (the amendment below on
      the commit guard;
      `services/api/tests/test_guards.py::test_an_n_outside_the_commit_does_not_block_it`,
      `::test_the_commit_guard_denies_every_proven_bypass`)
- [ ] a commit whose subcommand follows git global options (`-C <path>`,
      `-c <k=v>`, `--no-pager`, `--git-dir`, `--work-tree`, `-P`, `-p`), or
      whose `commit` is quoted, is checked like an adjacent one (the
      amendment below on git's global options;
      `services/api/tests/test_guards.py::test_the_commit_guard_denies_every_proven_bypass`,
      `::test_the_commit_guard_allows_ordinary_work`, planned
      test_a_commit_after_global_options_still_requires_a_spec_trailer,
      planned test_the_reuse_exemption_reads_only_the_commits_own_words)
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

## Amendment (2026-10-06): the reference check reads web tests

`test_every_test_a_spec_names_actually_exists` collects the names of the web
tests under `apps/web/src`, but its reference pattern matches only a Python
symbol in a code span. A web test cited in a spec is never checked, so a
renamed web test breaks a spec's proof without a failure, which is the defect
the check exists to catch. It has already happened: spec 047 still cites "an
absent artifact says which operation would produce it", a name commit 8e689a8
changed.

The same pattern misses a Python continuation, a code span holding `::` and a
symbol, which cites a test in a file named earlier. The pattern expects a path
or the symbol straight after the backtick, so it skips every continuation, and
specs use them often. Spec 047 cites
test_every_parameterized_kind_is_reachable_from_the_page that way, a name
commit 5ee1f62 changed.

The name collection has a defect of its own. It reads a web test's name up to
the first quote mark of any kind, so a double-quoted name holding an
apostrophe is cut short there, and a correct citation of it would fail once
citations are read.

### What the check reads

**Web test names.** Every `test(` and `it(` call in a `*.test.ts` or
`*.test.tsx` file under `apps/web/src`. A name runs to the quote mark that
closes the one it opened with, so an apostrophe inside double quotes is part
of the name.

**Citations.** Specs cite a web test in five shapes, and the check reads all
five, in a spec's text outside fenced code blocks. Below, `<file>` is a web
test file, written as a path from the repository root or as a bare file name,
and `<name>` is a test's name. A name may wrap across lines; a run of
whitespace compares as one space.

```text
`<file>::<name>`              file and name in one code span (specs 042, 047, 048, 049)
<file>::"<name>"              the same outside a code span (spec 026)
`::<name>` or ::"<name>"      a continuation: the name is in the last <file>
                              before it (specs 026, 042, 047, 048, 049)
`<file>`: "<name>", "<name>"  a file in a code span, a colon or a comma, then
                              names separated by commas or "and" (specs 072, 075, 080)
"<name>" in `<file>`          a name, then "in", then the file (specs 056, 072, 080)
```

In the last two shapes a name may be double-quoted or in a code span. A name
in a code span is read only when it holds a space, which a Python symbol and a
path never do.

**What it checks.** A path must name a web test file, and a bare file name
must match exactly one. The name must be a test in that file: two files can
hold tests of the same name, so a test elsewhere does not count. A
continuation with no file before it fails.

**Planned.** The word planned directly before a name exempts that name while
no test has it. Where the file and the name share one code span, planned goes
before the code span. This is the convention commit 851c7fa set for spec 080.
A name marked planned that does exist fails: the marker has outlived its
reason, and left in place it would hide the next rename. The change that
writes the test removes the word.

**A quoted test name with no file fails.** A double-quoted string or a code
span whose text is exactly the name of an existing web test must be read by
one of the five shapes, and not as planned. Spec 080's amendments, and one
sentence of spec 056's, quote web test names with no file, and none of those
is checked today. Without this rule each new way of writing a citation would
go unread until its test was renamed, the way qualified Python names did
(review of PR #49) and web names did.

**Python continuations.** A code span holding `::` and a symbol is read like a
bare symbol: the symbol must be a test somewhere under `services/api/tests`.
It is not tied to the file named before it, because specs name that file in
prose as often as in a code span, and tying it would fail correct citations.

### What changes

- `services/api/tests/test_spec_structure.py`: the check above becomes a
  function that the existing test calls over the committed specs and the new
  tests call over fixtures. The fixtures are synthetic: invented test names in
  a temporary directory.
- Spec 047: the two renamed citations name their tests as they are now,
  "an absent artifact is listed with the operation that would produce it" in
  `ApplyPage.test.tsx`, and
  `test_every_parameterized_kind_is_reachable_from_a_page`.
- Spec 080: its amendment from the review of PR #122 gives as its proof a test
  that a later amendment renamed and rewrote. The proof sentence says so, and
  every quoted web test name in its amendments gains its file.
- Spec 056: one quoted web test name gains its file.

No new file, so `config/data-classification.json` does not change.

**Output.** The test fails with one line per broken citation: the spec, the
line, the name, and what is wrong (not a test in its file, no single file,
planned but present, or quoted with no file).

**Failure modes this must not introduce.** A quoted phrase that is not the
name of a test never fails: the rule on quoted names fires only on the exact
name of a test that exists. A web test file named with no test after it, as
in a Scope list, is not a citation.

### Limitations

- A citation that was wrong when it was written, in a shape the check does
  not read, stays unread. The rule on quoted names sees only names that
  exist. It holds every citation of an existing test, so a rename is caught.
- A web test whose name is built at run time, from a template string or a
  loop, has no fixed name to compare. None exists today.
- Several specs from 016 to 026 cite Python tests outside code spans, and the
  check does not read those. The Python planned convention relies on exactly
  that: a planned name is left out of a code span so the check skips it. A
  rule for Python like the one on quoted web names would need those specs
  edited first, so it is not part of this amendment.

## Amendment (2026-10-06): Python citations in specs 016 to 026 are read

The last limitation above is this amendment. Specs 016 and 018 to 026 cite
Python tests outside code spans, so the check reads none of those citations,
and a renamed test breaks their proof without a failure. One already has:
spec 023 cites test_the_schema_carries_a_scope_column_for_later_tenancy,
which commit 3defbdf (spec 041) removed along with the scope column. The test
that replaced it, `test_the_schema_carries_no_scope_column`, asserts the
opposite.

### What changes

- Specs 016 and 018 to 026: each Python test cited outside a code span moves
  into one, in the form it already has: a bare symbol, a path and symbol, or
  a continuation. Spec 026 splits a path and symbol across two lines after
  the `::`, and those join. No other text changes.
- Spec 023: the scope column criterion stays ticked, because it held when the
  spec shipped. It gains a sentence saying spec 041 removed the column, and
  it cites the replacing test in a code span. The removed name stays out of
  one, the way spec 065 writes a removed test.
- A name marked planned stays out of a code span, as before. Specs 016 to
  026 mark none.

No code changes and no new file.

**How to know it worked.** The check passes over every committed spec.
Renaming a test that one of these specs cited only outside a code span makes
it fail and name the spec; before this change the same rename passed.

### Limitations

- Nothing fails a Python test cited outside a code span, so a new spec can
  repeat the gap. A rule like the one on quoted web names would catch it. It
  can be written once these specs are edited, and it is its own change.
- Spec 023's other sentences about the scope column, in its Problem, Scope
  and honest limitations, have been stale since spec 041. Correcting them is
  not a citation change, so it is left to its own change.

## Amendment (2026-10-06): spec 023 describes the table spec 041 left

The last limitation above is this amendment. Spec 041 removed the `scope`
column from `user_config` in commit 3defbdf and amended ADR-009 to match.
Spec 023 still describes the column in four places, one more than that
limitation names:

- its Problem gives ADR-009's aim as a data layer "a tenant scope can
  partition later"
- its Scope keys the table on (scope, kind) and calls `scope` the tenancy
  seam
- step 1 of its resolution order reads the store "when a row exists for the
  scope"
- its honest limitations say "the scope column exists and partitions"

### What changes

- Spec 023: each of those sentences is corrected against the code and names
  the file or test that proves it. A Python test goes in a code span, so the
  reference check reads it. The scope column criterion, which the amendment
  above marked superseded, does not change.

No code changes and no new file.

**How to know it worked.** `grep -n scope specs/023-user-configuration-in-db.md`
finds the column only in sentences that describe its removal and in the
criterion the amendment above marked superseded, and
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
passes over every committed spec, so each test the corrections cite exists.

### Limitations

- No check compares a spec's prose with the code. The reference check
  proves that a cited test exists, not that the sentence citing it is true,
  so a later change can leave a spec's prose stale without a failure.
- Spec 023's sentence on the API write path says it has no auth because the
  service binds to localhost. It predates spec 035, which requires the
  session token on config writes
  (`services/api/tests/test_api_exposure.py::test_a_state_changing_request_without_the_token_is_refused`).
  It is not about the scope column, so it is left to its own change.

## Amendment (2026-10-06): a Python test named outside a code span fails

The first limitation of the amendment on Python citations in specs 016 to 026
is this amendment. The check reads a Python test citation only in a code
span, so a Python test named anywhere else is never read, and a rename breaks
that citation without a failure. Specs 016 and 018 to 026 cited tests that way
until that amendment, and nothing stops a new spec from doing it again. Web
tests had the same gap, and the rule on quoted names closed it.

### What the check reads

**An existing Python test named outside a code span fails.** In a spec's text
outside fenced code blocks and code spans, the exact name of a test defined
under `services/api/tests` fails the check. Cited in a code span instead, the
name is checked as before. A name no test has passes, so the removed tests
that specs 023, 045 and 065 name stay as they are.

**A name stands alone.** A name is a whole word: a longer identifier that
contains one is not that name. A `/` or a `.` joins a name to a path or a file
name, and then it is part of that path, so `services/api/tests/test_x.py`
names a file and not a test. A full stop that ends a sentence joins nothing.

**Planned.** The word planned directly before a name, or before the path and
`::` joined to it, marks a test not yet written. The name stays out of a code
span, as before, and passes while no test has it. Once a test has it, the
citation fails as marked planned but exists, as on the web side: the marker
has outlived its reason, and the change that writes the test removes the word
and moves the name into a code span. Planned before a code span fails the
same way once its test exists. A planned Python name never belongs in a code
span: with no test, the span already fails as naming nothing.

### What changes

- `services/api/tests/test_spec_structure.py`: `unproven_citations` applies
  the rule, and new tests run it over invented repositories with synthetic
  names.
- The committed specs: a draft of the rule finds no existing Python test
  named outside a code span in them, so none is expected to change. Any name
  the rule does find moves into a code span in this change, and the pull
  request lists it.

No new file, so `config/data-classification.json` does not change.

**Output.** One line per name: the spec, the line, the name, and what is
wrong (named outside a code span, or marked planned but exists).

**How to know it worked.** The check passes over every committed spec.
Writing an existing Python test's name in a spec, outside a code span, makes
it fail and name the spec and the line. Before this change it passed.

**Failure modes this must not introduce.** A file name, a path, a longer
word, a fenced example and a name no test has never fail.

### Limitations

- The rule sees only names that exist, as the rule on quoted web names does.
  A name that was already wrong when it was written stays unread outside a
  code span.
- A code span that holds more than a citation, such as a command, is still
  not read. None holds the name of an existing test today.
- Planned is read only directly before a name or its path. In "planned
  test_x and test_y" only the first name carries it, so once both tests
  exist the second fails as named outside a code span instead. Both fail.

## Amendment (2026-10-06): spec 023 and the architecture doc on API auth

The second limitation of the amendment "spec 023 describes the table
spec 041 left" is this amendment. Spec 023's honest limitations say "The
API write path has no auth either, because the service binds to localhost
(unchanged from every other endpoint)." Spec 035 made that untrue. In
`services/api/src/harrier_api/app.py`, `PUT` and `DELETE /config/{kind}`
declare `dependencies=[Depends(require_token)]`, so a request without the
local API token gets 403. `TrustedHostMiddleware` wraps the whole app, so
a request whose Host header is not in `TRUSTED_HOSTS`
(`services/api/src/harrier_api/localauth.py`) gets 400 before it reaches
any route.

The token is not authentication, and the correction does not call it that.
`load_or_create_token` in `services/api/src/harrier_api/localauth.py`
creates one token per install and stores it readable only by its owner. It
tells the harrier UI apart from a page on another origin, not one user from
another. The README's honest limitations call this a same-machine boundary,
not a user model, and spec 023's correction uses the same words. Spec 035
says the token is "bound to the local session", and that limitation calls
it the session token. The code creates it once per install and keeps it in
a file, so spec 023's correction says per install.

`docs/architecture.md` makes the same claim in its honest limitations: "No
auth on the API; it binds to localhost." This spec's Problem already lists
architecture statements about auth among the claims to correct.

### What changes

- Spec 023: that sentence is corrected against the code. It says that since
  spec 035 a config write needs the local API token and a trusted Host
  header, names the tests that prove it in code spans so the reference
  check reads them, and calls the token a same-machine boundary rather than
  authentication. The paragraph's sentence "There is no authentication, no
  tenant resolution, and no isolation" does not change: it is about user
  accounts, and there are none.
- `docs/architecture.md`: that line is corrected against the same code. It
  keeps "Single user, single machine", says there are no user accounts, and
  says the local API token and the trusted-host check stop a web page in
  another tab from driving the API, but not a process running as the
  operator. It names `services/api/tests/test_api_exposure.py` as the proof,
  as the criterion on corrected claims requires.

No code changes and no new file.

**How to know it worked.** `grep -n "binds to localhost" specs/023-user-configuration-in-db.md docs/architecture.md`
finds nothing, and
`services/api/tests/test_spec_structure.py::test_every_test_a_spec_names_actually_exists`
passes over every committed spec, so each test the correction cites exists.

### Limitations

- Nothing reads `docs/architecture.md` or a spec's prose against the code,
  so either can go stale again without a failure, the way these two did
  after spec 035.

## Amendment (2026-10-06): the commit guard reads only the commit's words

`guard-commit.sh` looks for a hook bypass anywhere in the command string. An
`-n` that belongs to another command, or that sits in the commit message,
counts. So a chain with `sed -n`, `grep -n`, `head -n`, `echo -n`,
`git log -n 3` or `find -name` beside the commit is denied. On 2026-10-06 it
denied this ordinary commit for PR #148:

```text
sed -n 1,6p specs/045-claims-and-gates.md && git add specs/045-claims-and-gates.md && git commit -q -F - <<'EOF'
...
Spec: 045
EOF
```

That is the failure mode this spec says a guard change must not introduce.
The same string match misses forms that git reads as `-n`. Each of these
skipped a failing pre-commit hook under git 2.43 in a throwaway repository,
and passed the guard with a trailer:

```text
git commit "-n" ...         git commit -nm"a b" ...
git commit ... -n;          git commit --no-veri ...
(git commit ... -n)         x=$(git commit ... -n)
```

### What the guard reads

**The words of each git commit.** The check reads the words after
`git commit` to the end of that command, as the shell passes them to git:
quotes removed, ending at an unquoted `;`, `&`, `|` or newline, or at the
close of the subshell or command substitution that holds the commit. A
command substitution or a redirection among the words does not end them.
Commands inside a subshell or a command substitution are read the same way.

**A word git reads as skipping the hooks denies.** That is `--no-verify`, an
abbreviation of it that git accepts (`--no-veri`, `--no-verif`), or a short
option cluster with an `n` before the first option that takes a value. So
`-n`, `-an`, `-nm "..."` and `-nm"..."` deny, and `-am "..."` and `-m"note"`
do not.

**Other commands' options and message text are not read.** An option of
another command in the chain, such as `sed -n`, is not read, unless that
command also receives `git commit` in its text (below). Nor is a heredoc
with a quoted delimiter that gives the commit its message: one read by git
commit itself, as in `-F - <<'EOF'`, or one read by `cat` inside a command
substitution among the commit's words, as in `-m "$(cat <<'EOF' ...)"`. A
message given with `-m` is one word, so text inside it, such as
`fix sed -n`, does not read as an option.

**Text another command may run is checked as before.** When a word, a
heredoc or a here-string that another command receives contains
`git commit`, that text and the command's own words go through the old
whole-string pattern together, because the text may run with those words as
arguments. So `bash -c "git commit -n ..."`, a heredoc fed to `bash`, and
`bash -c 'git commit "$@"' _ -n` stay denied. A commit whose words hold
`"$@"` or another positional parameter takes them from elsewhere in the
string, as a function that runs `git commit "$@"` does, so the whole string
goes through the old pattern. So does a command the guard cannot read, such
as one with an unclosed quote.

The hooksPath and git dir check, the trailer check, and the test for whether
a command commits at all still read the whole string. The trailer sits in
the heredoc, so it has to.

### What changes

- `.claude/hooks/guard-commit.sh`: the `-n` check reads words as above. The
  reader is POSIX awk inside the script, so no file is added.
- `services/api/tests/test_guards.py`: the forms above join the bypass list,
  with the nested and argument-passing forms that must stay denied. A new
  test, `test_an_n_outside_the_commit_does_not_block_it`, runs the chains and
  message texts above, the PR #148 command first.

No new file, so `config/data-classification.json` does not change.

**Output.** Unchanged: exit 2 and the same message on a bypass, exit 0
otherwise.

**How to know it worked.** The PR #148 command passes the guard, and every
case in the bypass list is denied. Run against the guard before this change,
the new test fails on every case.

**Failure modes this must not introduce.** A bypass the old pattern denied
that the new check allows. Every case already in the bypass list stays
denied, and so does `-n` on a commit inside a subshell, a command
substitution, `bash -c "..."` or a heredoc a shell reads, and `-n` handed as
an argument to such text or to a function that runs `git commit "$@"`.

### Limitations

- The guard reads text. A commit whose words are built at run time, from a
  variable, `eval` or a script file, is not read, as before.
- A command commits only when `git` and `commit` are adjacent, as before, so
  `git -C dir commit -n` is not checked at all. Closing that is its own
  change.
- A command that receives text holding `git commit` is still checked whole
  with its words. So `echo "git commit -n"` and `grep -n "git commit" file`
  in a chain with a commit stay denied. A message that mentions `git commit`
  is still checked by the old pattern when it reaches git through a pipe or
  through a heredoc with an unquoted delimiter, so an `-n` in it can deny the
  commit.
- A commit whose words hold `"$@"`, `$1` or the like is checked whole, so
  `git commit -m "$1"` chained beside a `sed -n` stays denied.
- A word after `--`, or an `-m` value that starts with a dash and an `n`, is
  denied, though git reads it as a path or as message text.

## Amendment (2026-10-06): the commit guard reads git's global options

The second limitation of the amendment "the commit guard reads only the
commit's words" is this amendment. `guard-commit.sh` decides whether a command
commits at all with `COMMITS`, a pattern that needs `git` and `commit` side by
side on one line. A git global option between them, or a quote around either
word, hides the commit, and then no check runs. Each of these skipped a
failing pre-commit hook under git 2.43 in a throwaway repository, and the
guard at e7696e9 (PR #151) allowed each with a trailer:

```text
git -C . commit -n ...               git -c user.name=x commit -n ...
git --no-pager commit -n ...         git --work-tree . commit -n ...
git -P commit -n ...                 git -p commit -n ...
git "commit" -n ...                  "git" commit -n ...
bash -c "git -C . commit -n ..."
```

So did `git \` with `commit -n ...` on the next line. The same guard allowed
`git -C . commit -m "x"`, which has no trailer, and
`git -C . commit -m "x" .env -m "Spec: 045"`. `--git-dir` is denied wherever
it appears, so `git --git-dir .git commit -n ...` was denied already.

### What the guard reads

**The subcommand.** In each command the reader reads, the subcommand is the
first word after `git`, or after a word that ends in `/git`, that is neither
a global option nor the value of one. A global option is a word that starts
with `-`. Eight take the next word as their value, as git 2.43 reads them:
`-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--config-env`,
`--attr-source` and `--shallow-file`. Written as one word with `=`, as in
`--work-tree=.`, an option takes no further word. Words are read as the shell
passes them, so `git "commit"`, `"git" commit` and a line continuation
between the two give the subcommand `commit`. The words after it are then the
commit's words, checked as the amendment on the commit guard says. The global
options are not among them.

**What counts as a commit.** A command commits when the whole string matches
`COMMITS`, as before, when the reader reads a commit in it as above, or when
text another command receives holds one. Text is not read word by word, so it
is matched with a pattern that allows global options, and the values of the
eight above, between `git` and a `commit` that may be quoted. A command the
reader cannot read is matched whole with the same pattern. The reader runs on
any command whose text holds `commit`. A command that commits gets every check
an adjacent `git commit` gets: the `.env` check, the bypass check and the
trailer check.

**The `-C` exemption reads the commit's own words.** The trailer check exempts
`-C <commit>`, which reuses a message that already carries its trailer. It
matches `-C` between spaces anywhere in the string, which cannot tell the
global `-C <path>` from it, so every commit made with `git -C` would skip the
trailer check. It now holds only when `-C` is one of the commit's own words.
So a `-C` in another command or in message text no longer exempts a commit.
Before this change, `git commit -m "pass -C to tar"` and
`tar -C /tmp -cf /dev/null . && git commit -m "x"` passed with no trailer.
`--amend --no-edit` is still matched in the whole string.

The hooksPath and git dir check still reads the whole string, as before.

### What changes

- `.claude/hooks/guard-commit.sh`: the reader finds the subcommand as above,
  and reports a commit it reads and a `-C` among a commit's words. The gate,
  the `.env` check and the `-C` exemption use what it reports. No file is
  added.
- `services/api/tests/test_guards.py`: the forms above join `BYPASSES`, each
  with a valid trailer, so a deny proves the bypass check and not the trailer
  check. `git -C dir commit -m "..." -m "Spec: 045"`,
  `git -C dir commit -C HEAD` and `git -C dir log -n 3` join `ORDINARY`. The
  `.env` test gains `git -C . commit ... .env`. Two new tests, planned
  test_a_commit_after_global_options_still_requires_a_spec_trailer and
  planned test_the_reuse_exemption_reads_only_the_commits_own_words, run
  commits with no trailer: after global options, and with a `-C` outside the
  commit's own words.

No new file, so `config/data-classification.json` does not change.

**Output.** Unchanged: exit 2 and the same message on a deny, exit 0
otherwise.

**How to know it worked.** Every form above is denied, and the guard at
e7696e9 allows each new one. `git -C dir commit -m "..."` with no trailer is
denied. `git -C dir commit -m "..." -m "Spec: 045"`,
`git -C dir commit -C HEAD` and `git -C dir log -n 3` are allowed.

**Failure modes this must not introduce.** A git command whose subcommand is
not `commit` is not checked, whatever options precede it, so
`git -C dir log -n 3` stays allowed. Every case already in the bypass list
stays denied, and every ordinary case stays allowed.

### Limitations

- An alias is not read as a commit. `git -c alias.ci=commit ci -n ...`
  skipped the hook and passed the guard here, and an alias set in git config
  would too.
- The hooksPath and git dir check matches `-c core.hooksPath` only as
  written. A lowercase key, a quoted value, `--config-env`, and the
  `GIT_CONFIG_COUNT` or `GIT_CONFIG_PARAMETERS` environment each skipped the
  hook and passed the guard here. Closing that is its own change.
- `git -C dir add .env` still passes the `.env` check, whose test for
  `git add` needs the two words side by side.
- The options that take a value are git 2.43's. An option that another git
  version reads with a value is read here as taking none.
- The reader runs only on a command whose text holds `commit`, so a
  subcommand with a quote or a backslash inside the word, such as
  `co"mm"it`, is not read.
- A commit in text another command receives is matched, not read, so `-C`
  never exempts it: `bash -c "git commit -C HEAD"` now needs a trailer. A
  command the reader cannot read keeps the whole-string `-C` exemption, so a
  global `-C` still exempts it.
