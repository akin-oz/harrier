# ADR-006: Scheduling

- Status: accepted
- Date: 2026-08-08

## Context

The cadence is fixed and carries over: discovery at 09:00, 13:00, 16:00, 20:00; Apify
LinkedIn only on weekday mornings; digest at 20:30; Gmail watch every 5 minutes. Today
launchd runs three plists calling shell wrappers (`launchd/*.plist`,
`scripts/run-all-intake.sh`). Known defects in the current setup: two plists point at
`~/Documents/projects/job-hunt-local` while the repo lives at `~/job-hunt-local`, paths
and username are hardcoded, and the weekday-morning Apify gate lives in shell
(`run-all-intake.sh` checks weekday and hour<12) with an Apify count (50) that
contradicts both CLAUDE.md (150) and the GUI default (200).

## Options

### In-app scheduler (APScheduler or similar inside the API service)

Pros: schedules visible and editable in the GUI, one config source. Cons: scheduled
discovery then depends on the API server running, and this is a daily driver on a
laptop where the server will not always be up; missed-run semantics, catch-up, and
sleep/wake handling all have to be reimplemented; launchd already solves persistence,
wake behavior, and logging at the OS level. An in-app scheduler makes the GUI a
single point of failure for the pipeline, inverting the current reliability story.

### launchd calling the new CLI (recommended)

Pros: proven for this exact workload; runs regardless of whether the API or GUI is up;
OS-level logs; zero new dependencies. Cons: plists are macOS-only (acceptable: the
daily driver is a Mac; the demo needs no scheduler) and are not self-documenting
(mitigated below).

## Decision

launchd retained, calling the new CLI directly. Changes from the old setup:

1. **Plists are generated, not hand-maintained**: `harrier schedule install` renders
   the three plists from templates with the real repo path and user, writes them to
   `~/Library/LaunchAgents/`, and loads them. `harrier schedule status|uninstall`
   complete the lifecycle. This kills the wrong-path bug class permanently.
2. **Policy moves out of shell into the CLI**: the weekday-morning Apify gate and the
   Apify count become logic and config inside `harrier discover` (single flag
   `--scheduled` selects policy-gated behavior). Wrappers reduce to one line or
   disappear; the count is defined once in config.
3. **The GUI reads, and controls, but does not own**: schedule status shown in the GUI
   comes from parsing the installed plists and `launchctl print` (the capability that
   exists today in `gui/scheduling.py` and `gui/page_logs.py` carries over), including
   enable/disable/restart controls.

The cadence itself is config in one place (`config/schedule.json`), consumed by the
plist templates, so changing a time is a config edit plus `harrier schedule install`.

## Consequences

- Discovery keeps running when the API server is down; the run manager (ADR-004) is
  for interactive runs, launchd for scheduled ones, both through the same CLI code
  path, so behavior cannot fork.
- Linux/demo environments simply have no scheduler; documented, with the CLI runnable
  manually or via cron if a user wants it.
- Cutover (see `docs/cutover-plan.md`) is `launchctl unload` of the three old plists
  and `harrier schedule install`; both systems never write concurrently because the
  tracker stores differ until the final switch.
