---
spec: 143
title: The synthetic test owners are erased from a project
status: accepted
approved: yes
milestone: M9
depends: [105, 111, 115]
---

# Spec 143: The synthetic test owners are erased from a project

## Problem

Spec 115's staging check, `services/api/tests/test_supabase_project.py`,
proves spec 105 on a real Supabase project. Each run makes owners by
inserting random ids into `auth.users` with no email
(`new_owner` in `services/api/tests/pg_support.py`). Its docstring says
they stay: the `owner_id` foreign keys, append-only events and tracks that
are never deleted keep them (spec 115, spec 111's Problem).

On the staging project on 2026-10-15 that left 167 such users beside the
one real account, and their rows in every owned table. A count read as the migrator, which bypasses row
security, is mostly test residue. Each sign-up trigger run and each future
migration also touches them.

Nothing can remove them today. Spec 111 designs `harrier operator erase
<user id>` and the function `harrier_erase_owner(uuid)` behind it. Neither
is built, and erasing 167 owners one command at a time, each with its id
typed twice, is not a reasonable operator task.

Staging will not grow more of them: spec 115's staging mode refuses a
project whose `auth.users` holds a user with an email, and staging now
holds the operator's own account.

## Scope

1. A command, `harrier operator erase-test-owners`, in spec 111's
   `harrier operator` group.
2. It finds the synthetic test owners by a rule that no real account can
   meet, reports them, and with confirmation erases each through spec 111's
   erase, unchanged.

## Behavior

### Which owners are synthetic

An `auth.users` row is a synthetic test owner when all of these hold:

- `email` is null and `phone` is null;
- `is_anonymous` is false;
- `last_sign_in_at` is null;
- no `auth.identities` row names it.

A real account fails at least one: an invited or signed-up user has an
email and an identity, and an anonymous user has `is_anonymous` true (and
the API refuses anonymous users anyway, spec 104). The rule reads only
these columns; it reads no row of any owned table to decide.

### Without `--confirm`: a report

```
harrier operator erase-test-owners
```

It prints the number of synthetic owners, then each owned table with the
total of their rows in it, then `nothing erased; repeat with --confirm <n>`.
Exit 0. It writes nothing. It prints no user id.

### With `--confirm <n>`

```
harrier operator erase-test-owners --confirm 167
```

`<n>` must equal the number the report finds at that moment, so a stale
number, or a real account the rule would somehow match, stops the run
before anything is erased. For each synthetic owner, in `auth.users` id
order, it runs spec 111's erase steps unchanged (ban, Storage, rows through
`harrier_erase_owner`, auth delete, stragglers, ledger). Before each owner
it checks the rule again for that id, and skips an owner that no longer
meets it.

It prints one progress line per ten owners and a final line with the
number erased, the number skipped, and the per-table row totals deleted.
Exit 0 only when every matched owner was erased or skipped. A failure
stops at that owner, exits 1 naming the step (spec 111's wording), and a
re-run continues: an erased owner no longer exists, so it is not found
again.

The environment and refusals are spec 111's: the operator login in
`HARRIER_DATABASE_URL`, `HARRIER_SUPABASE_URL`, and
`HARRIER_SUPABASE_SECRET_KEY` from the shell, refused when defined in
`.env`.

## Failure modes

| Case | Result |
|---|---|
| No synthetic owner | The report prints 0; `--confirm 0` erases nothing; exit 0 |
| `--confirm` differs from the count found | Exit 1 before any erase, naming both numbers |
| A real user signs in mid-run | Its row gains `last_sign_in_at`; the per-owner recheck skips it |
| Spec 111's erase fails for one owner | Exit 1 at that owner, naming the step; earlier owners stay erased; a re-run continues |
| The admin API answers 404 for a test owner | Spec 111 treats a missing user as already gone for the auth steps; the rows step still runs |
| Spec 111 is not built | The command does not exist yet; this spec waits for it |

## Acceptance criteria

- [ ] On a store with synthetic owners, an invited user with an email, an
      anonymous user and a user who signed in, the report counts only the
      synthetic owners, and the confirmed run erases only them
      (`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_matches_only_synthetic_owners`).
- [ ] Without `--confirm` nothing is written, and the output holds counts
      and table names only, no user id
      (`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_reports_without_writing`).
- [ ] A `--confirm` number that differs from the count exits 1 and erases
      nothing (`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_refuses_a_stale_count`).
- [ ] An owner that stops matching between the report and its turn is
      skipped (`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_rechecks_each_owner`).
- [ ] A failure partway exits 1, and a re-run finishes the rest
      (`services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_resumes_after_a_failure`).
- [x] After a confirmed run on staging, the migrator counts no row in any
      owned table for an owner outside the real accounts, and `auth.users`
      holds the real accounts only. Recorded in the pull request that runs
      it, as counts only: see "Staging run (2026-10-15)" below.

Tests use the same shimmed Postgres as spec 111's erase tests, with the
admin API faked at its boundary, and synthetic ids only.

## Proof / origin

- Akin asked on 2026-10-15, after the first import into staging (spec 141),
  for the test accounts to be cleaned up.
- The counts above were read as the migrator on 2026-10-15: 168 auth users,
  one with an email; rows in every owned table for owners other than the
  real account.
- Where the owners come from: `new_owner` in
  `services/api/tests/pg_support.py`, used by
  `services/api/tests/test_supabase_project.py` in staging mode (spec 115).
- Why they cannot be deleted today, and the erase this spec reuses: spec
  111, "Problem" and "Deletion: what is erased, in what order".
- Why staging grows no more: spec 115's staging mode refuses a project
  whose `auth.users` holds a user with an email.

## Open decisions for Akin

1. **Build spec 111's erase first.** This spec reuses it unchanged and is
   blocked until it exists. Spec 111 also carries `harrier operator invite`
   and the operator login. Recommended: build spec 111 whole, then this.
   Alternative: split spec 111 so its erase lands first.
2. **The rule above, rather than a list of ids.** Recommended: the rule
   can be checked again per owner and needs no file of ids. A list would
   come from the test runs, which never recorded their ids.

## Decisions taken (2026-10-15)

Akin approved the spec on 2026-10-15. Decision 1: build spec 111 whole
first, then this. Decision 2: the rule, as recommended.

## Amendment (2026-10-15), found while building

Building the command settled four points the text above left open. None
changes a decision taken above.

- The report's lines are `synthetic test owners: <n>`, then one
  `rows held: <table> <count>` line per owned table in
  `harrier.tracker.schema.OWNED_TABLES` order, then the instruction line.
  The test is `services/api/tests/test_operator_erase_test_owners.py::test_erase_test_owners_reports_without_writing`.
- The rule reads `is_anonymous IS FALSE`, so a null does not match. It errs
  toward keeping a user.
- The counts and the rule are read as the operator's login, not as
  `service_role`: `service_role` holds no privilege on the owned tables
  (migration 10). The login must be able to read
  `auth.users` and `auth.identities`; the migrator's login on a Supabase
  project is expected to. When it cannot, the command exits 1 with a fixed
  text that names no column value.
- A failure also prints `stopped after <e> erased and <s> skipped; run the
  command again to continue` on standard error, after spec 111's step line,
  so the operator sees how far the run got. It prints no user id.
- The shim's `auth.users` still holds only an id. The test file adds the
  four columns and `auth.identities` in its own fixture, because
  `test_supabase_project.py` adds an `email` column to the shim itself.
- Both modes need spec 111's environment, including the secret key, though
  the report makes no admin call: one refusal path for the command.
- **The login is the migrator's.** Checked on staging on 2026-10-15, read
  only. A dedicated login made as spec 111's runbook describes
  (`LOGIN NOINHERIT`, a member of `service_role`) can switch to
  `service_role` but reads neither `auth.users` nor `auth.identities`, and
  no owner's rows. The project's `postgres` role can do all three: it can
  `SET ROLE service_role`, holds `SELECT` on both auth tables, and bypasses
  row security. So `HARRIER_DATABASE_URL` for this command is the
  migrator's URL. `harrier operator erase` for one account still works with
  the dedicated login. `docs/deploy.md`, "Erasing the synthetic test
  owners", says so.

## Out of scope

- Erasing any account that has an email, a phone, an identity or a sign-in.
- Changing spec 111's erase steps.
- Where spec 115's staging check runs next. It can no longer run against
  staging, which now holds a real account; a separate throwaway project is
  a decision for spec 115.
- Production, which has never run spec 115's check.

## Honest limitations

- The rule is a property of how the tests made owners, not a mark they
  carry. A future way of creating real users with no email, no phone and
  no identity would match it. The count confirmation and the per-owner
  recheck narrow the window; they do not close it.
- Spec 111's ban and Storage steps are written for users GoTrue made. For
  owners inserted by SQL they are expected to find nothing to do; the
  first staging run confirms or refutes that.

## Migration

None. The operator runs the command once per project that ran spec 115's
staging check.

## Note (2026-10-15): the first staging run

The first run with `--confirm 167` exited 0, printed "erased 167", and
deleted nothing: every row count was 0, `auth.users` still held all 167, and
the ledger recorded each as complete. The auth server answers 404 for these
owners, and spec 111's erase read that as "already erased", which is the
case this spec's failure table says must still erase the rows. Spec 111's
amendment of 2026-10-15 fixes the erase; the run is repeated after it lands.
The ledger's 167 lines from that run are false; the repeated run appends a
true line for each id.

## Staging run (2026-10-15)

The repeated run, after spec 111's amendment of 2026-10-15 landed (PR #353),
exited 0 and reported 167 owners erased and none skipped, with rows deleted
from every owned table those owners held. Read afterwards as the migrator:

- `auth.users` holds one user, the operator's own account.
- No owned table holds a row whose `owner_id` is another user.
- The real account's rows were not touched by the run.

The first run's 167 false ledger lines stay in `data/hosted/erasures.jsonl`;
the repeated run appended a true line for each id after them.
