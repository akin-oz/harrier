---
spec: 147
title: Interim, tailor and cover-letter run locally against a hosted account
status: accepted
approved: yes
milestone: M10
depends: [107, 141, 144]
---

# Spec 147: Interim, tailor and cover-letter run locally against a hosted account

## Problem

The operator's tracker rows and profile documents now live in a hosted
account (spec 141's import, spec 144's account commands). The hosted API
refuses `tailor` and `cover-letter` runs until spec 107's PR 3 binds a run to
Storage: `_until_107` in `services/api/src/harrier_api/runs.py` answers
"tailor runs are not available on the hosted API until spec 107". The local
CLI cannot reach the account for them either: spec 144's `--owner` runs only
`ACCOUNT_COMMANDS` (`services/api/src/harrier/accountstore.py`), and its Out
of scope names `tailor` and `cover-letter`. So until PR 3 lands, the operator
cannot produce a tailored resume or a cover letter for a job that exists only
in the hosted account.

This is an interim measure, by Akin's decision of 2026-10-16: not a feature,
and removed once PR 3 lets the hosted API run both.

## Scope

1. `harrier tailor` and `harrier cover-letter` join `ACCOUNT_COMMANDS`, so
   `--owner <user id>` runs them against the account, through the same
   login and checks spec 144's commands use.
2. Both open their store through `_open_store()` in
   `services/api/src/harrier_cli/main.py`, which already returns the account
   when `--owner` is given, instead of `connect()`, which always opens the
   local file.
3. Their files stay local: nothing here binds Storage.

## Behavior

With `--owner` and `HARRIER_ACCOUNT_DATABASE_URL` in the shell, as spec 144
requires:

- **Rows.** Each command reads the job, its track, the profile documents
  and the brief from the account, and records its status change and events
  there, through the tracker's write path under the owner's row policy.
- **Files.** On the operator's machine nothing is bound, so `files()`
  (`services/api/src/harrier/filestore.py`) returns the local data
  directory, as it does today. The tailored resume, its metadata and the
  cover letter are written under the local `data/`, at the paths a local run
  uses. The description cache is read from the local `data/` too.
- **Description.** A job whose description is in neither the local cache
  nor `--jd-file`/`--jd-text` is handled as a local run handles it today:
  `load_cached_description` reads as empty and the command proceeds
  without one. A job discovered by a hosted run has no local cache entry,
  so `--jd-file` is the way to give it one.
- **AI provider.** The local `AI_PROVIDER` setting applies, so the CLI
  providers (`claude-cli`, `codex-cli`) work, as they do not hosted.
- **Without `--owner`.** Nothing changes: both open the local store.

The hosted web app does not show files written this way: they are local.
Spec 146's `import-artifacts` copies them to Storage when wanted.

## Failure modes

| Case | Result |
|---|---|
| `--owner` without `HARRIER_ACCOUNT_DATABASE_URL` | Spec 144's refusal, exit 1, before anything opens |
| A job id the account does not hold | The tracker's unknown-job error, exit 1, as locally |
| The account's profile documents are missing a kind the resume needs | The resume bundle error a local run gives, exit 1 |
| The connection drops mid-run | Spec 144's "could not connect" refusal, exit 1. Files already written stay local; the status change is in one transaction and is either recorded or not |
| The local container is running | No conflict: these commands with `--owner` never open the local SQLite file. They write files under `data/`, as the container may; the same family's writers already tolerate that |

## Acceptance criteria

- [ ] `harrier tailor --owner <id> <job>` against a synthetic account reads
      the account's job and profile documents, writes its files under the
      test's data directory, and records the status change in the account
      and not in any local store
      (planned test_tailor_with_owner_reads_and_records_in_the_account).
- [ ] The same holds for `harrier cover-letter --owner`
      (planned test_cover_letter_with_owner_reads_and_records_in_the_account).
- [ ] Without `--owner`, both still open the local store
      (planned test_tailor_and_cover_letter_without_owner_stay_local).
- [ ] With `--owner`, neither opens the local SQLite file
      (planned test_tailor_with_owner_never_opens_the_local_store).
- [ ] `ACCOUNT_COMMANDS` holds exactly spec 144's commands plus `tailor`
      and `cover-letter`.

Tests use synthetic owners, rows and documents only, on the CI Postgres
service, with the AI step off (`--no-ai` or the test provider).

## Removal

Once spec 107's PR 3 lifts the hosted refusal for both kinds, the change
after it removes `tailor` and `cover-letter` from `ACCOUNT_COMMANDS` and
records the removal here. From then the hosted API runs them and writes to
Storage. Akin may instead keep them; that is a decision for that change,
not this one.

## Proof / origin

- Akin, 2026-10-16: local `tailor` against the hosted account, files local
  until PR 3, as an interim solution and not a feature; `tailor` and
  `cover-letter` only.
- The hosted refusal: `_until_107` in `services/api/src/harrier_api/runs.py`.
- The account commands and their store: `ACCOUNT_COMMANDS` in
  `services/api/src/harrier/accountstore.py`, `_open_store` and
  `_run_for_account` in `services/api/src/harrier_cli/main.py` (spec 144).
- `tailor` opens `connect()` today: `_cmd_tailor` in
  `services/api/src/harrier_cli/main.py`; so does `_cmd_cover_letter`.
- Local files when nothing is bound: `files()` in
  `services/api/src/harrier/filestore.py` (spec 107).

## Out of scope

- Writing to Storage, or any Storage identity on the operator's machine.
- `answers`, `brief`, `evaluate`, `discover` or any other command.
- The hosted API's own `tailor` and `cover-letter` runs: spec 107's PR 3.
- Copying the local files up: spec 146.

## Honest limitations

- Files written this way are invisible to the hosted web app until they are
  imported, and the Apply page shows those jobs with no artifact.
- A job found by hosted discovery has no local description cache entry; the
  operator supplies one with `--jd-file`.

## Migration

None. The operator runs `harrier tailor --owner <id> <job>` with the account
login in the shell, as spec 144's commands are run.
