---
spec: 120
title: The owner sees and sets their schedule
status: accepted
approved: yes
milestone: M10
depends: [050, 096, 106, 108, 114]
---

# Spec 120: The owner sees and sets their schedule

Split from spec 108 on 2026-10-10, when spec 108 was refined (its Scope
item 12). Spec 108 keeps the operator's half: the scheduler process, the
`schedule` configuration kind, the store and the operator's report.

The stub was approved on its headline alone; its Scope said it would be
refined into a real spec before approval was asked for. This is that
refinement, so approval returns to `no` until Akin reads it.

## Problem

Spec 108 lets a hosted owner choose which jobs run and in which timezone,
through a `user_config` row of kind `schedule`, and records each slot's
outcome in `schedule_state`. The owner can neither set nor see either:

- **No page writes the kind.** The Settings page's Configuration section
  edits `feeds`, `linkedin_searches`, `discovery` and `company_holds`, and
  shows every other kind read-only with "Change it with `harrier config
  set <kind>`" (`apps/web/src/features/config/ConfigSection.tsx`, `known`
  and `ReadOnlyKind`). A hosted owner cannot run that command: `--owner`
  does not take `config set` (`ACCOUNT_COMMANDS` in
  `services/api/src/harrier/accountstore.py`, spec 144). The only way to
  choose a schedule today is a hand-written `PUT /config/schedule` with
  the session token, which is how the staging check on 2026-10-16 had to
  do it.
- **No page shows what ran.** Hosted `GET /ops/schedule` reads the
  committed `config/schedule.json` and the local success records, and
  answers with launchd's wording and the host command `harrier schedule
  status` (`get_schedule` and `SCHEDULE_HOST_COMMAND` in
  `services/api/src/harrier_api/ops_routes.py`). It never reads the
  owner's choice or `schedule_state`. The Operations page prints "On the
  host, `harrier schedule status` reports it"
  (`apps/web/src/pages/operations/ScheduleSection.tsx`), a command a
  hosted owner cannot run.

So a hosted owner with no row is never scheduled and is told nothing about
it. On 2026-10-16 the staging owner had feeds and searches stored but no
`schedule` row, so the scheduler would never have started a discovery run
for them.

## Scope

1. A hosted-only Schedule section on the Settings page that reads and
   writes the owner's `schedule` kind: which hosted jobs run, and in which
   timezone.
2. Hosted `GET /ops/schedule` answers from the owner's choice and
   `schedule_state`, with wording that names the hosted scheduler.
3. The Operations page's Schedule section shows that answer hosted.

Locally nothing changes: launchd, `harrier schedule`, the local
`GET /ops/schedule` answer and the local Operations page stay as they are.

## Behavior

### The Settings section

Shown only in the hosted app (`appMode().kind === "hosted"`), between
Credentials and Telegram notifications, headed "Schedule". It reads the
`schedule` entry from `GET /config` and writes with
`PUT /config/schedule`. No new write route: the store's rules
(`validate_schedule` in `services/api/src/harrier/userconfig/store.py`)
stay the only rules.

- **Jobs.** One checkbox per hosted job, in `HOSTED_JOBS` order
  (`discovery`, `academic-discovery`), each with its committed cadence in
  words (`describe_cadence`), which reads "daily at 09:00, 13:00, 16:00,
  20:00" for discovery and "Mondays at 09:30" for academic discovery.
- **Credential note.** A job whose `JOB_NEEDS` names a credential the owner
  has not saved (`academic-discovery` needs `apify`) shows "Needs the
  Apify credential. Without it, each slot is recorded as not configured
  and nothing runs." beside its checkbox. The box can still be ticked: the
  credential may be saved afterwards.
- **Timezone.** A select of IANA zone names. With a stored row it shows the
  stored zone. Without one it preselects the browser's zone
  (`Intl.DateTimeFormat().resolvedOptions().timeZone`), and the section
  says "Not scheduled yet: nothing runs on a schedule until you save."
- **Save.** Sends `{"value": {"timezone": <zone>, "jobs": [<ticked>]}}`.
  On success the section shows "Saved. Changes take effect from the next
  slot." and the next slot of each chosen job, in the chosen zone.
- **No jobs ticked** saves `jobs: []`, which pauses the schedule
  (spec 108). The section then says "Paused: no job is chosen."
- **Refusal.** A 400 from the store shows its `detail` under the form,
  verbatim; it names the field (spec 108, "The owner's choice"). The form
  keeps what was entered.
- The section offers no delete. `DELETE /config/schedule` stays as spec
  108 defines it, for the CLI and the API, and pausing does what an owner
  needs.
- The read-only `schedule` entry leaves the Configuration section's
  read-only list in the hosted app, since this section edits it.

### Hosted `GET /ops/schedule`

The response shape gains hosted fields. Locally they are null, and every
existing field keeps its local value, so the local answer is unchanged.

`ScheduleOut` gains:

- `timezone: str | None`: the owner's stored zone. Null locally, and null
  hosted when there is no `schedule` row.

`host_command` becomes `str | None`: `harrier schedule status` locally,
null hosted.

`ScheduledJobOut` gains `hosted: HostedJobOut | None`, null locally:

| Field | Value |
|---|---|
| `last_slot_at` | `schedule_state.last_slot_at`, or null with no row for the job |
| `last_outcome` | `schedule_state.last_outcome`, or null with no row |
| `last_finished_at` | `schedule_state.last_finished_at`, or null |
| `last_ok_at` | `schedule_state.last_ok_at`, or null |
| `next_slot_at` | the job's next slot after now, from the committed cadence read in the owner's zone, as ISO 8601 UTC |

Hosted, the answer is:

- **`jobs`:** the owner's chosen jobs, in `HOSTED_JOBS` order, each with
  `cadence` from the committed file, `records` empty, and `hosted` filled.
  Empty with no row or with `jobs: []`.
- **`error`:** null; or, for a stored row the store refuses, the refusal,
  scrubbed as today (spec 133), with `jobs` empty.
- **`installed_state`:** "Hosted, the scheduler starts each chosen job at
  its slots in your timezone." when jobs are chosen; "You have not chosen a
  schedule. Nothing runs on a schedule; choose jobs in Settings." with no
  row; "Your schedule is paused: no job is chosen." with `jobs: []`.

The hosted read is the signed-in owner's: the route answers only with a
session (every hosted route but the public one does, spec 114), and it
reads `user_config` and `schedule_state` on the owner's connection, under
the `owner_only` policy (spec 108, "The store"). It reads no other owner's
rows and starts nothing.

### The Operations page, hosted

The Schedule section renders the hosted answer:

- a table of chosen jobs with Job, Cadence, Last run and Next slot;
- **Last run** is the outcome in words with its finish time, for example
  "OK, 16 Oct 20:03". `running` reads "Running since <started>". Any other
  outcome reads in words ("Failed", "Crashed", "Timed out", "Not
  configured", "Refused", "Interrupted", "Missed"), as a badge with a shape
  as well as a colour (as "Overdue" is today). No row reads "No run
  recorded yet.";
- times are shown in the owner's chosen zone;
- the note is `installed_state`, with no host command.

Locally the section renders exactly as today.

## Failure modes

| Case | Result |
|---|---|
| Hosted owner with no `schedule` row | Settings shows nothing ticked, the browser's zone preselected and "Not scheduled yet". Operations shows "You have not chosen a schedule". |
| A stored row the store now refuses (a zone this server no longer loads, a job leaving `HOSTED_JOBS`) | Settings shows the refusal from `GET /config` and a blank form, which saves a valid row over it. Operations shows the refusal in `error`; `jobs` is empty. |
| The browser's zone is not one the server's `zoneinfo` loads | The save answers 400 naming `schedule.timezone`; the form keeps the entry and shows the detail. |
| `academic-discovery` chosen with no Apify credential | Saved. Settings shows the credential note. Each slot is recorded `not_configured` (spec 108) and Operations shows "Not configured". |
| Saved at 15:00 with a 16:00 slot | Nothing starts for 15:00. Settings shows 16:00 as the next slot (spec 108: a slot is due only at or after the row's `updated_at`). |
| Two tabs save different choices | The later save wins; `PUT /config/{kind}` is an upsert. Each tab shows its own save until it reads again. |
| A slot is running when the owner changes the choice | The run continues. The change applies from the next slot. |
| The scheduler process is not running (staging's machine has no schedule; a production tick failed) | The page cannot know. Next slot shows when a slot is due, not that it will start; Last run stays as the store last recorded it. The operator's report (spec 108) is where a missing tick shows. |
| The local app | No Schedule section in Settings. `PUT /config/schedule` answers 404 as today (`kinds_for` offers `schedule` on Postgres only). `GET /ops/schedule` and the Operations section are unchanged. |
| `GET /ops/schedule` hosted with no session | 401, as every hosted route. |

## Acceptance criteria

- [x] Hosted, the Settings page shows a Schedule section with one checkbox
      per hosted job and a timezone select; saving sends the ticked jobs
      and the zone to `PUT /config/schedule` (`apps/web/src/pages/settings/ScheduleChoiceSection.hosted.test.tsx`:
      "settings schedule section saves the chosen jobs and zone").
- [x] With no stored row the section preselects the browser's zone and
      says "Not scheduled yet"; with `jobs: []` stored it says "Paused"
      (`apps/web/src/pages/settings/ScheduleChoiceSection.hosted.test.tsx`:
      "settings schedule section names an unscheduled and a paused owner").
- [x] A 400 from the save shows its detail and keeps the entry (`apps/web/src/pages/settings/ScheduleChoiceSection.hosted.test.tsx`:
      "settings schedule section shows the store's refusal").
- [x] `academic-discovery` shows the Apify note when no `apify` credential
      is saved, and none when one is (`apps/web/src/pages/settings/ScheduleChoiceSection.hosted.test.tsx`:
      "settings schedule section notes a job's missing credential").
- [x] Locally the Settings page has no Schedule section (`apps/web/src/pages/settings/SettingsPage.test.tsx`:
      "settings page shows no schedule section locally").
- [x] Hosted `GET /ops/schedule` lists only the owner's chosen jobs, each
      with `hosted` holding the `schedule_state` row's fields and the next
      slot in the owner's zone, `timezone` set and `host_command` null
      (`services/api/tests/test_owner_schedule.py::test_hosted_schedule_lists_the_owners_chosen_jobs_and_their_last_run`).
- [x] Hosted `GET /ops/schedule` with no row, with `jobs: []`, and with a
      row the store refuses answers the three `installed_state` texts and
      the refusal in `error` (`services/api/tests/test_owner_schedule.py::test_hosted_schedule_names_an_unscheduled_paused_or_unreadable_choice`).
- [x] Hosted `GET /ops/schedule` reads no other owner's `schedule_state`
      (`services/api/tests/test_owner_schedule.py::test_hosted_schedule_reads_only_the_owners_state`).
- [x] The local `GET /ops/schedule` body is unchanged apart from the new
      fields, which are null (`services/api/tests/test_ui_operations.py::test_local_schedule_answer_carries_null_hosted_fields`).
- [x] The Operations page shows the hosted Last run and Next slot and no
      host command (`apps/web/src/pages/operations/OperationsPage.test.tsx`:
      "operations schedule section shows the hosted last run").
- [x] The contract is regenerated (`just contract`) and the web app reads
      the new fields only through the generated types.
- [x] `just check` passes.

## Out of scope

- The scheduler, its store, its login and the operator's report: spec 108.
- Per-owner cadences or times (spec 108, Open decision 4). The section
  chooses jobs, not times.
- `digest` and `gmail-watch` as hosted jobs: they join `HOSTED_JOBS` with
  their own specs (spec 108, "The hosted job set").
- A delete button for the `schedule` row.
- Starting a run from the schedule section: run buttons stay where they
  are.
- Telling the owner when a scheduled run failed (Telegram per owner is spec
  109's; a failure alert is not specified).
- Giving staging's scheduler machine a schedule: it stays started by hand
  (spec 108, Hosting budget).
- The local schedule: launchd, `harrier schedule` and the local Operations
  page do not change.

## Honest limitations

- **Next slot is not a promise.** It is when a slot falls due. Whether the
  scheduler starts it depends on the scheduler machine running, which the
  owner's page cannot see.
- **The zone list is the browser's.** `Intl.supportedValuesOf("timeZone")`
  and the server's `zoneinfo` can disagree on a rare zone; the server's
  refusal is what the owner then sees.
- **Last run is one row per job.** `schedule_state` keeps the latest slot
  only, so the page shows the last run, not a history.

## Amendment (2026-10-16): what the build settled

Four places where the text above was short of what the build needed.
Each is built as written here.

- **The jobs on offer.** The form needs every hosted job, its cadence and
  the credentials it needs, chosen or not, and `jobs` lists only the
  chosen ones. So the hosted answer also carries `offered`, a list of
  `OfferedJobOut` (`name`, `cadence`, `needs`), every job of `HOSTED_JOBS`
  the committed file names, with `JOB_NEEDS` as `needs`. It is null
  locally. It is filled hosted whatever the owner's row holds, including a
  row the store refuses
  (`services/api/tests/test_owner_schedule.py::test_hosted_schedule_names_an_unscheduled_paused_or_unreadable_choice`).
- **Where the form reads.** The Settings section reads `GET /ops/schedule`,
  not `GET /config`: that answer carries the offered jobs, the owner's
  zone and chosen jobs, the next slots it shows after a save, and the
  store's refusal of a stored row in `error`. It still writes through
  `PUT /config/schedule`. "Not scheduled yet" is `timezone` null with no
  `error`; "Paused" is a `timezone` with no jobs.
- **When a running job started.** "Running since <started>" needs the
  start, so `HostedJobOut` also carries `last_started_at`. `last_outcome`
  is typed as spec 108's outcome classes, so the page's words for each
  are checked against the contract.
- **A stored row the store refuses** reads in Settings as "The stored
  schedule could not be read: <refusal>. Saving replaces it." above a form
  with nothing ticked and the browser's zone.

## Changes to approved specs

- **Spec 096** (Operations wording): hosted, the Schedule section names the
  hosted scheduler and no host command. Its local wording does not change.
- **Spec 050** lists `GET /ops/schedule` as tokenless. That stays true
  locally. Hosted it already needs a session (spec 114); this spec makes
  the hosted answer the owner's own.

## Decisions taken (2026-10-16)

Akin approved the spec as written on 2026-10-16. The hosted answer extends
`GET /ops/schedule` with nullable fields, rather than adding a separate
hosted endpoint.

## Migration

None. An owner with no row sees "Not scheduled yet" and is scheduled once
they save. Rows written by hand before this spec, such as staging's on
2026-10-16, show in the form as stored.

## Proof / origin

- Akin asked on 2026-10-16 for a Settings form for the schedule, after the
  staging discovery check needed a hand-written `PUT /config/schedule`.
- `specs/108-hosted-scheduling.md`: Scope item 12, "The hosted job set",
  "The owner's choice: kind `schedule`", "The store", Out of scope.
- `services/api/src/harrier_api/ops_routes.py`: `get_schedule`,
  `ScheduleOut`, `ScheduledJobOut`, `SCHEDULE_HOST_COMMAND`.
- `services/api/src/harrier/hostedschedule.py`: `HOSTED_JOBS`, `JOB_NEEDS`,
  `latest_slot`. `services/api/src/harrier/schedule.py`:
  `describe_cadence`, `next_run_after`.
- `services/api/src/harrier/userconfig/store.py`: `validate_schedule`,
  `kinds_for`.
- `apps/web/src/pages/settings/SettingsPage.tsx`,
  `apps/web/src/features/config/ConfigSection.tsx`,
  `apps/web/src/pages/operations/ScheduleSection.tsx`, read on `main` at
  `a263b94`.
- `config/schedule.json`: discovery daily at 09:00, 13:00, 16:00 and 20:00;
  academic discovery Mondays at 09:30.
