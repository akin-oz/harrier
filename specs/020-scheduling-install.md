---
spec: 020
title: Schedule install CLI and launchd templates
status: in-progress
approved: yes
milestone: M5
depends: [011, 019]
---

# Spec 020: Schedule install CLI and launchd templates

## Problem

Generated plists kill the wrong-path bug class; policy moves into the
CLI (ADR-006). Two live defects in the old setup motivate this, both
verified on this machine while drafting:

1. The committed launchd/*.plist templates point at
   ~/Documents/projects/job-hunt-local while the repo lives at
   ~/job-hunt-local. The installed copies were hand-corrected at some
   point, so the committed templates and the installed reality have
   drifted; the committed ones would silently reinstall the break.
2. Every scheduled job runs through a shell wrapper that sources .env
   with `set -a; . .env`. A value spanning a line break makes bash try
   to execute the continuation as a command, which returns 127. The
   daily digest has not logged a run since 2026-06-29 for exactly this
   reason, while firing (and dying) at 20:30 nightly.

Generated plists that invoke the CLI directly remove both classes: the
path is resolved at install time, and there is no shell sourcing step
because harrier reads .env in Python (its loader skips lines without an
`=`, so a malformed line cannot abort a run).

## Scope

- config/schedule.json (public, no personal data): the cadence in one
  place. Three jobs, each with a label suffix, the harrier CLI command
  and arguments, and a trigger (calendar times for discovery and
  digest, an interval for the Gmail watch). Defaults carry the current
  cadence: discovery 09:00, 13:00, 16:00, 20:00 with --scheduled;
  digest 20:30; Gmail watch every 300 seconds.
- harrier.schedule:
  - load_schedule(path) parses and validates the config (unknown
    trigger kinds, missing commands, and out-of-range times are named
    errors)
  - render_plist(job, repo_root, python_path) produces the plist dict
    and its XML: Label, ProgramArguments invoking the resolved
    interpreter with -m harrier_cli.main and the job's arguments,
    WorkingDirectory at the repo root, the trigger, log paths under
    data/logs/, RunAtLoad false
  - install_schedule(...) writes each plist to ~/Library/LaunchAgents
    and loads it (bootout then bootstrap, tolerating a not-loaded
    first run); --dry-run renders and reports without writing or
    loading
  - schedule_status(...) reports, per job: whether the plist is
    installed, whether the installed content matches what would be
    rendered now (drift detection, the defect above), whether launchd
    has it loaded, its last exit status, and the next scheduled time
  - uninstall_schedule(...) unloads and removes the plists
- CLI: harrier schedule install [--dry-run], harrier schedule status,
  harrier schedule uninstall

## Inputs, outputs, failure modes

- Inputs: config/schedule.json, the repo root (resolved from the
  running module, not hardcoded), the interpreter path (sys.executable
  by default), and the LaunchAgents directory (overridable for tests).
- Outputs: three plists, their loaded state, and a status table.
- Failure modes: a malformed config raises ValueError naming the job
  and field; a missing LaunchAgents directory is created; launchctl
  failures surface with the command's stderr and a non-zero CLI exit;
  a non-macOS host reports that launchd is unavailable rather than
  writing anything (the demo path has no scheduler, per ADR-006).

## Stated changes from the old code

- No shell wrappers: plists invoke the CLI directly, so .env is read by
  harrier's Python loader and a malformed line can no longer abort a
  scheduled run.
- Paths are resolved at install time from the actual repo location;
  neither the path nor the username is ever written into a committed
  file.
- The Apify weekday-morning gate and count live in the CLI behind
  --scheduled (already shipped in spec 011), not in shell.
- Logs land under data/logs/ (never-in-git), not a repo-root logs/.
- Status detects drift between the installed plist and the rendered
  one, which is what would have caught defect 1 above.

## Acceptance criteria

- [ ] install writes and loads three plists pointing at the harrier
      repo, with no hardcoded home directory in the rendered output
      (test_install_writes_three_plists_at_the_real_repo_path)
- [ ] the rendered plists invoke the CLI directly, with no shell
      wrapper anywhere in ProgramArguments
      (test_rendered_plists_invoke_the_cli_without_a_shell)
- [ ] dry-run writes nothing and loads nothing
      (test_dry_run_writes_nothing)
- [ ] status reports installed, loaded, drift, and next run time, and
      flags a hand-edited plist as drifted
      (test_status_detects_drift)
- [ ] uninstall removes every plist it installed
      (test_uninstall_removes_plists)
- [ ] a malformed cadence config fails with the job and field named
      (test_invalid_schedule_config_is_rejected)
- [ ] All gates green on PR

## Proof / origin

Old repo launchd/*.plist (including the stale-path defect),
scripts/run-all-intake.sh, run-daily-digest.sh, run-gmail-watch.sh;
docs/adr/ADR-006-scheduling.md. Proving file:
services/api/tests/test_schedule.py. Honest limitations: launchctl is
not invoked in tests (the loader is injected), so load and unload are
proven only for the command lines they build and their error handling;
next-run computation is proven for the calendar and interval triggers
this config supports, not for arbitrary launchd trigger shapes.

## Out of scope

The GUI schedule page (a later surface spec), non-macOS schedulers,
and migrating the old plists (spec 022's cutover unloads them).
