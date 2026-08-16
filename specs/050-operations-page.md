---
spec: 050
title: The Operations page, and the honest list of what it does not do
status: accepted
approved: yes
milestone: M8
depends: [019, 020, 025, 029, 030, 035, 042, 047]
---

# Spec 050: The Operations page, and the honest list of what it does not do

## Problem

Spec 042 phase 5, and the one unticked acceptance criterion left open in spec
042 itself: "the Operations page lists the CLI-only commands with their
reasons, asserted by a test so the list cannot drift from reality".

The UI can start a discovery run and read config. Everything else an operator
does to keep the system running is terminal-only: checking feed health,
reconsidering past rejections, seeing whether the launchd schedule is installed
and when it last succeeded, taking a backup, sending the digest, exporting, and
managing profile documents.

The schedule matters more than the rest. A scheduled job on this system failed
silently for two months. `schedule status` reports installed state, drift, and
next run, and an operator who has to remember to run it is an operator who will
not. That is the single strongest reason this page exists.

## Scope

### Routes

| Route | CLI verb | Domain function | Shape |
|---|---|---|---|
| `GET /ops/feeds` | `check-feeds` | `load_feeds_for_check`, `check_feeds` | run |
| `POST /ops/feeds/prune` | `check-feeds --prune` | `prune_dead` | request |
| `POST /ops/reconsider` | `reconsider` | `reconsider_source` | run |
| `GET /ops/schedule` | `schedule status` | `schedule_status` | request |
| `POST /ops/schedule/install` | `schedule install` | the installer | request |
| `POST /ops/schedule/uninstall` | `schedule uninstall` | the uninstaller | request |
| `POST /ops/backup` | `backup` | `create_backup` | run |
| `POST /ops/digest` | `digest` | `run_digest` | run |
| `GET /ops/parity` | `parity` | the parity report | run |
| `POST /ops/export` | `export` | the exporter | run |
| `GET /ops/profile` | `profile list` | the profile store reader | request |

Feed health is a run because it makes a network request per configured board.
Reconsideration, backup, digest, parity and export are runs because each walks
the whole tracker or the whole data directory. Schedule status is a read of
local state and answers as a request.

### Three operations that need marking, not just wiring

**Reconsideration defaults to a dry run.** `reconsider` without `--apply`
reports what would be cleared and changes nothing. The page keeps that default:
the operator sees the count first and applies it as a second, separate action.
The CLI distinguishes "nothing is eligible to clear" from "everything already
used the current rules", after a review finding that conflating them makes a
claim about the operator's own decisions. The page keeps both strings.

**The digest sends a Telegram message.** Telegram notifications are the only
outbound messages this system produces, so this is allowed rather than a
violation, but it is still the one button on this page that talks to the
outside world. It is marked as sending, and the dry-run flag the CLI has is
offered first.

**Pruning dead feeds edits stored configuration.** It removes boards from the
watchlist. It is a separate action from checking, never a checkbox on the
check, and it reports what it removed by URL.

### The CLI-only list

This is the criterion spec 042 left open, and the reason it is load-bearing:
a UI that quietly covers less than it appears to is the same defect class as a
document that overclaims.

The page lists every command that has no button, with the command to run and
the reason it has none. Spec 042 fixed the list and the reasons:

| Command | Why not |
|---|---|
| `cutover` | One irreversible sitting requiring an attestation the operator makes deliberately. A button invites a mis-click that stops the old scheduler. |
| `restore` | Destructive by design: it overwrites the tracker, and the case for running it is one where the operator should be reading carefully. |
| `gmail-oauth` | A browser consent flow that writes a token; it belongs where the operator can see the whole exchange. |
| `migrate-legacy`, `gmail-migrate-state` | One-shot migrations, already run. |
| `demo-run` | A test harness for the run machinery. |

**The list is asserted against reality, not typed into a page.** A test
enumerates the CLI's subcommands, subtracts the ones this page and specs 042,
047, 048 and 049 give routes to, and asserts the remainder equals the table
above. Adding a CLI verb without either a route or a line here fails that test.
Without it the list is a comment that rots, which is the failure this criterion
exists to prevent.

### The page

Sections in the order an operator checks them: schedule and last-success ages
first, because that is the silent-failure surface; then discovery runs, which
exist today; then feed health, reconsideration, backup; then config, which
exists today; then the CLI-only list, last and plainly.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser, carrying flags the CLI verbs
  already take.
- Outputs: the same reports, archives, exports and messages the CLI produces.

Failure modes that must reach the operator:

- **launchd is absent or the plists are not loaded.** `schedule_status` reports
  installed, loaded, drift and next run as separate facts, and the page shows
  them separately. "Installed but not loaded" is the state that produced a
  two-month silent failure and it must be legible as its own condition, not
  folded into a single green badge.
- **Drift between the installed plist and the rendered one.** Reported as
  drift, naming what differs.
- **No boards are configured.** `check-feeds` treats it as an error and exits
  nonzero. The page says no boards are configured rather than showing an empty
  healthy list.
- **A backup fails verification.** `create_backup` raises `BackupError` rather
  than leaving an unverified archive. The page reports the archive was not
  written. A backup that looks successful and is not is the worst outcome on
  this page.
- **The digest fails to send.** `run_digest` returns a nonzero code. The digest
  text was still produced and the page shows it, distinguishing "no digest" from
  "digest not delivered".
- **Parity fails.** It compares against the old system, which may be absent on
  a fresh clone. Absence is reported as not-applicable rather than as a failure.

Failure modes this must not introduce:

- A button for anything in the CLI-only table.
- A schedule badge that reads healthy while a job has not succeeded recently.
- A destructive operation without a separate, deliberate second action.
- A second implementation of any report.

## Acceptance criteria

Proving symbols are named at implementation.

- [ ] every verb in the route table has a route calling the same domain
      function as the CLI verb, asserted in the shape spec 042 established
- [ ] the CLI-only list is derived by a test that enumerates the CLI's
      subcommands and the routed ones, and fails when a new verb is added with
      neither a route nor a line in the table
- [ ] no route exists for any command in the CLI-only table, asserted by the
      same test
- [ ] reconsideration defaults to reporting and requires a separate action to
      apply, proven by a test that the default changes nothing
- [ ] "nothing is eligible to clear" and the all-current-rules case remain
      distinct strings on both sides
- [ ] `installed`, `loaded`, `drifted`, `last_exit_status` and `next_run` reach
      the response as separate fields per job, and the `problem` property
      decides the badge, so a test asserts an installed-but-not-loaded schedule
      does not render as healthy
- [ ] a failed backup verification reports that no archive was written, and a
      test asserts no archive is left behind
- [ ] a digest that is produced but not delivered is distinguishable from one
      that was never produced
- [ ] pruning dead feeds is a separate action from checking them, and reports
      each removed URL
- [ ] absence of the old system renders parity as not-applicable rather than
      failed
- [ ] every operations write requires the token; schedule status, feed results
      and the profile list are reads and do not
- [ ] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`
- [ ] no personal data enters a committed fixture, a test name, or a
      screenshot
- [ ] spec 042's open criterion is marked satisfied, citing the test above
- [ ] all gates green on PR

## Proof / origin

The CLI-only table and its reasons are copied from spec 042, which set them.
The domain functions are the imports in `_cmd_check_feeds`, `_cmd_reconsider`,
`_cmd_backup`, `_cmd_digest`, `_cmd_parity`, `_cmd_export`, `_cmd_schedule` and
the profile handlers in `services/api/src/harrier_cli/main.py`. The
"nothing is eligible to clear" distinction is a review finding recorded in a
comment in `_cmd_reconsider`. `schedule_status` is in
`services/api/src/harrier/schedule.py`. The silent-failure history is the
stated motivation of the operability review lens in
`.ai/agents/review-operability.md`.

## Out of scope

Any change to what a domain function does. Authentication design, spec 035.
Buttons for the CLI-only commands. A general job scheduler in the UI: this page
reports on launchd and installs the rendered plists, and inventing schedules in
the browser is a different product. Editing profile documents in the browser;
the page lists them and the CLI imports and exports them.

## Migration

None.
