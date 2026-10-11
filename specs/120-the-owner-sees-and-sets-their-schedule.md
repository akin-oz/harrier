---
spec: 120
title: The owner sees and sets their schedule
status: accepted
approved: yes
milestone: M10
depends: [108, 114]
---

# Spec 120: The owner sees and sets their schedule

Split from spec 108 on 2026-10-10, when spec 108 was refined (its Scope
item 12). Spec 108 keeps the operator's half: the scheduler process, the
`schedule` configuration kind, the store and the operator's report.

## Problem

Spec 108 lets an owner choose hosted jobs and a timezone through a
`user_config` row of kind `schedule`, and records each run's outcome in
`schedule_state`. The owner has no way to see or set either in the
browser:

- Hosted `GET /ops/schedule` still reads the committed file and launchd's
  wording (`services/api/src/harrier_api/ops_routes.py:37-41`,
  `:257-296`).
- The browser prints "On the host, `{host_command}` reports it"
  (`apps/web/src/pages/operations/ScheduleSection.tsx:71`), which names a
  host command a hosted owner cannot run.
- The `schedule` kind is served through the existing `/config` routes,
  but no page writes it.

Line numbers are from `main` at `44ac0d7`.

## Scope

Stub created by spec 108's refinement to sequence the hosted build. It is
refined into a real spec, with behavior and failure modes, before approval
is asked for.

## Acceptance criteria

Headline, to be expanded into checkable criteria when refined: hosted, a
browser form reads and writes the owner's `schedule` kind (which hosted
jobs run, and in which timezone), showing the store's refusals by field.
Hosted `GET /ops/schedule` lists the owner's chosen jobs, the committed
cadence of each, and the last outcome from `schedule_state`, with wording
that names the hosted scheduler instead of launchd. Locally the schedule
section does not change.

## Proof / origin

- `specs/108-hosted-scheduling.md`: Scope item 12, Behavior ("The owner's
  choice: kind `schedule`", "The store"), Honest limitations ("The owner
  sees nothing new yet") and Out of scope.
- `specs/114-the-browser-signs-in-to-the-hosted-api.md`: the hosted SPA
  this page runs in.
- `services/api/src/harrier_api/ops_routes.py`,
  `apps/web/src/pages/operations/ScheduleSection.tsx`, read on `main` at
  `44ac0d7`.

## Out of scope

- The scheduler, its store and the operator's report: spec 108.
- Per-owner cadences (spec 108, Open decision 4).
- The local schedule: launchd and `harrier schedule` do not change.
