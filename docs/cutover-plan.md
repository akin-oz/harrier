# Cutover plan

The old system at `~/job-hunt-local` keeps running untouched until harrier reaches
verified parity. Cutover is a planned event, not a drift. Spec 022 implements this
plan.

## Phase 0: baseline (now until M5)

- Old repo: fully operational, read-only as a codebase (no modifications), launchd
  jobs running, tracker accumulating.
- Harrier: developed against migrated snapshots and fixtures. Any harrier discovery
  run against real sources is dry-run only; it never notifies and never writes the
  old tracker.
- Rule: at no point do both systems write application state. They have separate
  stores until the final migration.

## Phase 1: parity checklist

Generated from `docs/parity-matrix.md`: every keep row becomes a checklist item
(behavior identical, proof named); every change row becomes an item stating the
intended difference (verified intentional, not accidental). Drop rows are confirmed
dropped on purpose. Owner: Akin checks or explicitly waives each item.

Key clusters, from the matrix:

- Screening decisions: same accept/reject/score on identical inputs (see phase 2).
- Tracker verbs and transitions, selector semantics, PDF gates.
- Artifact outputs: resume, cover letter, answers render and validate.
- Outreach staging discipline; Gmail classification kinds; digest sections.
- Schedule cadence and the Apify cost gate.

## Phase 2: dual-run period (minimum one full week)

- Old system remains the system of record: its launchd jobs run and notify as today.
- Harrier runs the same discovery on its own schedule in shadow mode: real source
  fetches, dry-run semantics (no Telegram, own store).
- Daily diff: harrier's screening decisions versus the old run's
  `incoming/job_imports_run.json` and per-source summaries. Every divergence is
  explained: either a bug (fix in harrier) or an intended change (must map to a
  parity-matrix change row).
- The period must include at least one weekday morning so the Apify path and its
  cost gate are exercised for real.
- Exit criteria: one full weekday cycle with zero unexplained divergences, and the
  parity checklist fully checked or waived.

## Phase 3: switchover (one sitting)

1. Quiesce: `launchctl unload` the three old plists
   (`com.akinoztorun.jobsearch.discovery`, `.daily-digest`, `.gmail-watch`).
2. Snapshot the old repo's `tracker/`, `state/`, `gmail_handler.log` (dated copies).
3. Final migration refresh: re-run the spec 004 migration against the snapshot so
   tracker rows added during dual-run are captured; migrate discovery seen-state,
   the description cache, and gmail seen-state.
4. Verify: row counts and spot checks; `harrier schedule status` clean;
   one manual `harrier discover --dry-run` and one `harrier digest --dry-run` green.
5. Go live: `harrier schedule install`; watch the next scheduled run end to end.
6. Fallback window (one week): the old repo stays runnable; reverting is
   `launchctl unload` of harrier plists and reloading the old ones. The old tracker
   is stale past the switchover point; any rows harrier added in that window would
   need manual re-entry, which is why the window stays short.

## Phase 4: archive

- Old repo: final commit of any uncommitted state, tag `archived-pre-harrier`,
  set the GitHub repo (if any) archived; local directory kept read-only.
- The old repo's git history contains PII (`docs/privacy-plan.md` §2); it stays
  private forever regardless of archival.
- Remove the old plists from `~/Library/LaunchAgents/`.
- Record cutover date and final row counts in `docs/cutover-log.md`.
