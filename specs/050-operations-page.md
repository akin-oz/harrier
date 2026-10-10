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

The schedule matters more than the rest. A scheduled job can stop running
without any error appearing anywhere: the plists are installed but not
loaded, and nothing says so until somebody thinks to ask. `schedule status` reports installed state, drift, and
next run, and an operator who has to remember to run it is an operator who will
not. That is the single strongest reason this page exists.

As amended by spec 096 (see the note at the end), the page cannot ask launchd:
the API runs in a container that has no `launchctl`. It shows when each
scheduled job last succeeded, which is where a job that stopped shows up, and
says the installed and loaded state is the host's to report.

## Scope

### Routes

| Route | CLI verb | Domain function | Shape |
|---|---|---|---|
| `POST /ops/feeds` | `config check-feeds` | `load_feeds_for_check`, `check_feeds` | run |
| `POST /ops/feeds/prune` | `config check-feeds --prune` | `check_feeds`, then `prune_dead` | run |
| `POST /ops/reconsider` | `reconsider` | `reconsider_source` | run |
| `GET /ops/schedule` | none: what the container can read in place of `schedule status` | the last-success records (spec 029) and the schedule definition | request |
| `POST /ops/backup` | `backup` | `create_backup` | run |
| `POST /ops/digest` | `digest` | `run_digest` | run |
| `GET /ops/profile` | `profile list` | the profile store reader | request |

Feed health is a run because it makes a network request per configured board.
Pruning is a run for the same reason: `config check-feeds --prune` probes every
board again and prunes what that probe finds dead, so it never prunes on a
stale result. Reconsideration, backup and digest are runs because each walks
the whole tracker or the whole data directory. The schedule read is a read of
the database and the schedule definition, and answers as a request.

Starting a run is never a GET (amended at implementation, 2026-10-10). Any
page the operator has open can make a GET to this API without the token, so
`GET /ops/feeds` would have let it start network probes. The check is
`POST /ops/feeds` with the token. Its results are the run's log, read through
`GET /runs/{id}/events`, which needs no token; that is the "feed results are
reads" this spec states.

`POST /ops/reconsider` takes the `track` parameter (spec 094) and passes a
non-default track to the CLI as `--track`, because `reconsider` works on an
academic track's own seen state (spec 097). The digest is the default
track's; any other is refused with 409.

`GET /ops/schedule` reads the last-success records and the schedule
definition (amendment 2 in the note at the end). It reports, for each
scheduled job, its cadence and when it last succeeded, and says the installed
and loaded state is the host's to report, naming the command that reports it
(`harrier schedule status`). It does not call `schedule_status`, which asks
`launchctl`.

The badge rule, stated at implementation (2026-10-10): a record is overdue
when the job has never recorded a success, when the time is unreadable, or
when the last success is older than twice the longest gap between the job's
scheduled runs (`OVERDUE_GAPS` and `job_health` in `harrier.schedule`). One
missed run is a laptop asleep at the scheduled minute; two in a row is a job
that stopped. A record within that limit reads "within its cadence", never
"healthy", because whether launchd still has the job is not known here.
Which records a job writes is read from its command: `discover` writes
`discovery`, `discover --configured-tracks` one record per configured
academic track, `digest` writes `digest`, and `gmail-watch` is expected to
write `mail-watch`. Nothing writes a `mail-watch` record today, which is the
same gap the digest's schedule line has, so the page shows the watch as never
having succeeded. That is what the records hold; the gap is recorded as out
of scope in the pull request rather than fixed here.

Removed by spec 096 before this spec was built: `POST /ops/schedule/install`
and `POST /ops/schedule/uninstall` (the container cannot load a launchd job),
`GET /ops/parity` (repository upkeep, terminal only), and `POST /ops/export`,
which becomes the two downloads spec 096 builds (`GET /ops/export/jobs.csv`,
`GET /ops/export/contacts.csv`).

### Request fields, and defaults that do nothing

Four of these routes can change something or spend something, so their
request shapes are named here rather than left to the implementation. With an
empty body, none of them applies a change, sends a message or prunes stored
configuration.

`POST /ops/backup` is the one that still does something on an empty body: it
writes an archive. That is deliberate rather than an exception smuggled in.
A backup is additive and a refusal to take one is the more dangerous default,
so it takes no confirmation, and the criterion below says what it must not do
rather than pretending it is inert.

| Route | Field | Default | What the default means |
|---|---|---|---|
| `POST /ops/reconsider` | `apply` | `false` | Report what would be cleared, clear nothing. Applying is a second, separate request the operator makes after reading the count. |
| `POST /ops/reconsider` | `source` | absent | Every source, as the CLI does. |
| `POST /ops/digest` | `dry_run` | `true` | Produce the digest text and send nothing. Sending is the explicit `false`. |
| `POST /ops/digest` | `date` | absent | Today, as the CLI does. |
| `POST /ops/digest` | `resend` | `false` | Refuse a day already delivered, with the time it went. `true` is the explicit request that sends it again. |
| `POST /ops/feeds/prune` | `confirm` | `false` | Refuse with 409: it edits stored configuration. |
| `POST /ops/backup` | `prune` | `false` | Take the archive and delete nothing (`harrier backup --no-prune`). `true` applies the CLI's own retention, which is the CLI's decision, not a second copy of it here. |

`dry_run` defaulting to `true` on the digest inverts the CLI's default,
which sends. That is deliberate: a request whose body was dropped, or a
client that forgot the field, must not send a message to a real person.

The backup row was `keep`, defaulting to the CLI's own value, when this spec
was approved. That contradicted the criterion that an empty-body backup
deletes nothing, because `create_backup` always pruned to `keep`. Amended at
implementation (2026-10-10): `create_backup` takes `keep=None` to delete
nothing, `harrier backup` gains `--no-prune` to pass it, and the route sends
`--no-prune` unless the body asks for `prune`. The CLI's default is unchanged.

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

**It sends at most once for a given day.** A browser retry, a double click,
or a second operator tab can all deliver the same digest twice, and the
recipient cannot tell a duplicate from a real second digest. So a non-dry
digest is single-flight and idempotent per target date: the run machinery's
per-target lock already refuses a second concurrent run for the same key
(spec 047), and a digest already delivered for that date is refused with a
message saying when it went, rather than sent again. Re-sending deliberately
is a separate explicit request.

Nothing recorded a delivery by day when this spec was approved, so the
refusal had nothing to read. Amended at implementation (2026-10-10):
`run_digest` records each delivery under `digest:<day>` in the run-outcome
records (spec 029) beside the `digest` success it already wrote, and
`delivered_at` in `harrier.digest` reads it. The CLI still sends without a
per-day limit; only the route refuses. The lock key for a digest run is the
day and whether it sends, so a double click joins the send in flight while a
preview of the same day is a separate run.

`harrier digest` prints two progress steps on the run protocol (ADR-004):
step 1 once the digest exists, step 2 once Telegram accepted it. The exit
status alone cannot tell "produced, not delivered" from "never produced",
because a failed send and a crash both exit 1.

**Pruning dead feeds edits stored configuration.** It removes boards from the
watchlist. It is a separate action from checking, never a checkbox on the
check, and it reports what it removed by URL.

### The CLI-only list

This is the criterion spec 042 left open, and the reason it is load-bearing:
a UI that quietly covers less than it appears to is the same defect class as a
document that overclaims.

Amended by spec 096 (amendments 5 and 6): the list moves to the Settings page
and becomes spec 096's three-way list, in which every subcommand is routed,
run on the host, or terminal only. Spec 096's test (planned
test_every_cli_subcommand_has_exactly_one_place) replaces the test described
below. The table and the test are kept here as the record of what spec 042
fixed.

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
exist today; then feed health, reconsideration and the digest.

Amended by spec 096 (amendment 5): configuration, backups and the CLI-only
list move to the Settings page, which spec 096 builds. The Operations page
keeps runs and reports.

## Inputs, outputs, failure modes

- Inputs: HTTP requests from the local browser, carrying flags the CLI verbs
  already take.
- Outputs: the same reports, archives, exports and messages the CLI produces.

Failure modes that must reach the operator:

- **launchd is absent or the plists are not loaded.** The container can see
  neither (amended by spec 096). The schedule section says the installed and
  loaded state is the host's to report, and names `harrier schedule status`
  as the command that reports it. It shows when each job last succeeded,
  because a job that is installed but not loaded stops recording successes,
  and that absence is what the container can see.
- **Drift between the installed plist and the rendered one.** The host's to
  report, by the same command.
- **The schedule definition is missing or malformed.** Reported in the
  definition loader's words, never as a healthy schedule.
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

Failure modes this must not introduce:

- A button for anything in the CLI-only table.
- A schedule badge that reads healthy while a job has not succeeded recently.
- An installed or loaded state shown as healthy, which the container cannot
  know (spec 096).
- A destructive operation without a separate, deliberate second action.
- A second implementation of any report.

## Acceptance criteria

Proving symbols are named at implementation. Python tests are in
`services/api/tests/test_ui_operations.py` unless named otherwise; web tests
are in `OperationsPage.test.tsx`.

- [x] every verb in the route table has a route calling the same domain
      function as the CLI verb, asserted in the shape spec 042 established
      (`services/api/tests/test_ui_operations.py::test_every_operations_route_calls_the_cli_verbs_function`,
      `::test_the_schedule_reads_the_definition_the_cli_installs`,
      `::test_the_profile_list_is_the_one_the_cli_prints`)
- [ ] moved to spec 096 (amendments 5 and 6): the CLI-only list is derived
      by a test that enumerates the CLI's subcommands and the routed ones, and
      fails when a new verb is added with no place (spec 096's planned
      test_every_cli_subcommand_has_exactly_one_place)
- [ ] moved to spec 096 with it: no route exists for any command in the
      terminal-only list, asserted by the same test
- [x] reconsideration defaults to reporting and requires a separate action to
      apply, proven by a test that the default changes nothing
      (`services/api/tests/test_ui_operations.py::test_reconsideration_reports_by_default_and_applies_on_request`,
      `OperationsPage.test.tsx::reconsideration reports first and offers clearing only after the report`)
- [x] with an empty body no route applies a change, sends a message or
      prunes stored configuration, proven by a test that posts an empty body
      to each and asserts it
      (`services/api/tests/test_ui_operations.py::test_an_empty_body_applies_sends_or_prunes_nothing`,
      run with an empty object and with no body at all)
- [x] `POST /ops/backup` on an empty body writes an archive and deletes
      nothing, proven by a test that asserts the retention prune did not run
      (`services/api/tests/test_ui_operations.py::test_an_empty_backup_writes_an_archive_and_deletes_nothing`;
      the retention still applies when asked,
      `::test_pruning_is_the_cli_s_own_retention_when_asked`)
- [x] a second non-dry digest for the same target date is refused with a
      message saying when the first went, proven by a test that posts twice
      and asserts one delivery
      (`services/api/tests/test_ui_operations.py::test_a_second_digest_for_the_same_day_is_refused_and_one_is_delivered`,
      `::test_a_double_click_joins_the_send_already_in_flight`,
      `::test_resending_is_its_own_explicit_request`, and
      `OperationsPage.test.tsx::a day already sent is refused in the server's words and resending is explicit`)
- [x] "nothing is eligible to clear" and the all-current-rules case remain
      distinct strings on both sides
      (`services/api/tests/test_ui_operations.py::test_nothing_eligible_keeps_the_domain_s_words`,
      `OperationsPage.test.tsx::the report keeps the CLI's words, so the two zero outcomes stay apart`;
      the CLI side is
      `services/api/tests/test_seen_policy.py::test_nothing_eligible_is_not_reported_as_everything_current`)
- [x] `GET /ops/schedule` reports, for each scheduled job, its cadence and
      its last-success time from the records, and says the installed and
      loaded state is the host's to report; a test asserts a job with no
      recent success does not render as healthy, and that no installed or
      loaded state renders as healthy (amended by spec 096)
      (`services/api/tests/test_ui_operations.py::test_the_schedule_marks_a_job_with_no_recent_success_and_claims_no_install_state`,
      `::test_an_unreadable_schedule_is_reported_in_the_loader_s_words`,
      `::test_overdue_is_twice_the_longest_gap`,
      `::test_a_success_just_inside_the_limit_is_not_overdue`,
      `OperationsPage.test.tsx::a job with no recent success reads as overdue and nothing reads as healthy`,
      `OperationsPage.test.tsx::the installed and loaded state is the host's to report, with the command`)
- [ ] a failed backup verification reports that no archive was written, and a
      test asserts no archive is left behind. The API half holds: the run
      fails in the domain's words and leaves no archive
      (`services/api/tests/test_ui_operations.py::test_a_backup_that_fails_verification_leaves_no_archive`).
      The sentence that no archive was written belongs to the backups
      section, which spec 096 builds on the Settings page (amendment 5), so
      this stays open until it lands there.
- [x] a digest that is produced but not delivered is distinguishable from one
      that was never produced
      (`services/api/tests/test_ui_operations.py::test_a_digest_produced_and_not_delivered_says_so`,
      `OperationsPage.test.tsx::a digest produced and not delivered is told apart from one never produced`,
      `OperationsPage.test.tsx::a delivered digest says so`)
- [x] pruning dead feeds is a separate action from checking them, and reports
      each removed URL
      (`OperationsPage.test.tsx::checking feeds changes nothing and pruning is a separate confirmed action`,
      a prune without `confirm` refused in
      `services/api/tests/test_ui_operations.py::test_an_empty_body_applies_sends_or_prunes_nothing`,
      and each removed URL printed by the verb the run executes,
      `services/api/tests/test_feed_health.py::test_prune_names_every_board_it_removed`)
- [x] every operations write requires the token; schedule status, feed results
      and the profile list are reads and do not
      (`services/api/tests/test_ui_operations.py::test_every_operations_write_requires_the_token`,
      `::test_the_schedule_and_the_profile_list_are_tokenless_reads`,
      `OperationsPage.test.tsx::every operations write carries the token and no read does`)
- [x] the generated client carries every new route and no hand-written request
      or response shape appears in `apps/web`
      (`services/api/tests/test_ui_operations.py::test_every_operations_route_is_in_the_contract`,
      and the contract drift gate, which regenerates and fails on a diff)
- [x] no personal data enters a committed fixture, a test name, or a
      screenshot. Every fixture is an invented company, chat or archive.
      Limitation: this is a property of the diff rather than something a test
      can assert.
- [ ] moved to spec 096 with the list: spec 042's open criterion is marked
      satisfied, citing spec 096's test
- [x] all gates green on PR (`just check` passes: 2354 Python tests, 116 web
      tests, the contract regenerated with no diff, `aie check` and the spec
      structure check)

## Proof / origin

The CLI-only table and its reasons are copied from spec 042, which set them.
The domain functions are the imports in `_cmd_check_feeds`, `_cmd_reconsider`,
`_cmd_backup`, `_cmd_digest` and the profile handlers in
`services/api/src/harrier_cli/main.py`. The
"nothing is eligible to clear" distinction is a review finding recorded in a
comment in `_cmd_reconsider`. The last-success records are
`harrier.runoutcome` (spec 029); the schedule definition is
`config/schedule.json`, read by `load_schedule` in
`services/api/src/harrier/schedule.py`. The container boundary behind spec
096's amendments: ADR-010, specs 061 and 074.

## Out of scope

Any change to what a domain function does, apart from the two additions this
spec needed and states above: `create_backup(keep=None)` and the per-day
delivery record `run_digest` writes. Each leaves every existing caller's
behaviour as it was. Authentication design, spec 035.
Buttons for the CLI-only commands. A general job scheduler in the UI: this page
reports what the container can read about the schedule and installs nothing,
and inventing schedules in the browser is a different product. Editing profile
documents in the browser;
the page lists them and the CLI imports and exports them.

## Migration

None.

## Note (2026-10-10): amended by spec 096 before it was built

Spec 096 amended this spec before any of it was built. Spec 050 was written
for an API on the host; ADR-010 then moved the API into a container that has
no `launchctl`, cannot write `~/Library/LaunchAgents`, and cannot see a host
path. The six amendments, applied in the text above:

1. `POST /ops/schedule/install` and `POST /ops/schedule/uninstall` are
   removed. The container cannot load a launchd job.
2. `GET /ops/schedule` reads the last-success records and the schedule
   definition, and says the installed state is the host's to report.
3. `GET /ops/parity` is removed. Parity is repository upkeep, which stays
   terminal-only by the decision of 2026-10-08.
4. `POST /ops/export` becomes the two downloads spec 096 builds.
5. Configuration, backups and the CLI-only list move to the Settings page;
   the Operations page keeps runs and reports.
6. The CLI-only list becomes spec 096's three-way list: routed, run on the
   host, or terminal only.

The acceptance criteria name the routes as amended. The criteria about the
CLI-only list, and spec 042's open criterion, are marked as moved to spec 096,
which builds the list, its page and its test.
