---
spec: 011
title: Discovery orchestrator, summaries, Telegram notify
status: in-progress
approved: yes
milestone: M2
depends: [008, 009, 010]
---

# Spec 011: Discovery orchestrator, summaries, Telegram notify

Refined from the stub before implementation; scope below is the real scope.

## Problem

One run over all sources in priority order with one aggregated notification.
This is where the sources, the screening pipeline, the tracker, and the run
manager converge: discovery becomes a live-streamed run in the GUI.

## Scope

### harrier.notify

- send_telegram_message port: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from
  env, form-encoded POST, the 0/1/2 return-code semantics. The token rides
  in the URL path per the Bot API; the URL is never logged.
- build_telegram_message port: the 8-item prospect summary format.

### harrier.discovery

- Priority order: greenhouse, ashby, lever, remoteok, apify_linkedin,
  wellfound, wttj (feeds.txt routes the first three; batch sources run only
  when files are passed).
- Per-source run: in-batch dedupe, screen_jobs against live tracker indexes
  and per-source seen state, tracker writes through the single write path,
  seen-state save and per-source summary JSON (data/incoming/
  <source>_latest.json) skipped on dry runs. Summary keys match the old
  run_source_import shape.
- Apify extras: cache_job_descriptions for everything fetched (the spec 009
  cost-saver, wired here), dataset-file mode passthrough.
- Aggregate: the old run-job-imports.py totals and shape, written to
  data/incoming/job_imports_run.json; exactly one Telegram message when
  new_prospects > 0 and notify is on (parity note: notify is independent of
  dry_run, as before; scheduled wrappers pass --no-notify with --dry-run).
- Scheduled policy (--scheduled): Apify runs only on weekday mornings
  (hour < 12, Monday to Friday), with the count from config/discovery.json.
  This resolves the old count discrepancy: the CLI default stays 150 (the
  old pinned default), the scheduled value is 50 (what production ran, a
  per-search ceiling under the 24h search window), the GUI's 200 dies with
  the Streamlit GUI.
- Progress callback: the orchestrator reports per-source begin/end; the CLI
  renders the ::harrier:: protocol so the run manager streams it.

### CLI and run manager

- `harrier discover` with --dry-run, --no-notify, --only-source
  (repeatable), --apify-count, --dataset-file, --wellfound-file,
  --wttj-file (repeatable), --scheduled. The CLI entry point loads .env
  from the working directory (the spec 009 amendment lands here).
- KIND_COMMANDS gains "discovery": a plain `harrier discover` (the GUI
  button bills Apify exactly as the old GUI's Run discovery now did).
  StartRunIn widens to demo or discovery; the RunPanel gains a Run
  discovery button next to the demo one.

## Acceptance criteria

- [ ] Old pins pass: CLI default apify count 150, override passes through
      to the Apify fetch
- [ ] A full orchestrated dry run over monkeypatched sources produces the
      aggregate shape with correct totals and sends nothing
- [ ] Exactly one Telegram message for N sources when prospects exist and
      notify is on
- [ ] Scheduled policy: Apify included on a weekday morning, excluded on an
      evening and on a weekend (injected clock)
- [ ] Dry runs write no tracker rows, no seen state, no summaries
- [ ] notify returns 2 without network when the token is missing
- [ ] Discovery is startable from the GUI and streams per-source progress
      (browser-verified with a dry-run)
- [ ] All gates green on PR

## Proof / origin

Old repo: scripts/run-job-imports.py, scripts/job_sources.py
run_source_import, scripts/run-all-intake.sh (the count=50 rationale
comment), scripts/send_telegram.py, tests/test_run_job_imports.py.

## Out of scope

launchd wiring (spec 020 calls `harrier discover --scheduled --no-notify`
or with notify per the old wrappers), the daily digest (spec 019), the
rejected-debug CSV (dropped: its consumer was ad hoc debugging; the run
summaries carry rejected_counts, and ScreenResult still exposes debug rows
for a future spec if wanted).
