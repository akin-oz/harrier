---
spec: 144
title: The operator writes one hosted account from the command line
status: proposed
approved: no
milestone: M9
depends: [091, 099, 103, 106, 112, 134, 137, 138, 141]
---

# Spec 144: The operator writes one hosted account from the command line

## Problem

The operator's search now runs on staging (ADR-013). Spec 141 copied the
local store into the operator's hosted account once, and refuses a second
copy into an account that holds rows. After that, three kinds of change
have no way into the hosted account:

- **A focused track.** The browser cannot add one: `BROWSER_KINDS` in
  `services/api/src/harrier_api/tracks_routes.py` leaves the focused kind
  to the command line (spec 137). On a Postgres store the command line
  refuses `tracks add` with `POSTGRES_NOT_YET`
  (`services/api/src/harrier_cli/main.py`, `main`).
- **A track's documents.** A screening document, a resume framing and an
  application profile are stored by `harrier profile put` (specs 099,
  137). The API lists profile documents and writes none
  (`list_profile_documents` in `services/api/src/harrier_api/ops_routes.py`).
  The command is refused on Postgres for the same reason.
- **A move between tracks.** `harrier move` (spec 138) has no route and is
  refused on Postgres.

The shared truth documents have a second gap, on both stores. `resume_facts`
and `resume_truth` are written only by `profile import` from the old
repository (`services/api/src/harrier/profile/store.py`) and by
`profile split-resume`. No command stores an edited copy, so adding a
verified line (a GitHub link, a contracting entity) means editing the
database by hand. On the hosted store that is a write outside the one write
path (ADR-003).

Writing the hosted database directly as the migrator would bypass the row
policy and the write path, so it is not an answer. Today the operator's
focused-scope track (specs 137 to 140) exists only as drafts.

## Scope

1. A global option, `--owner <user id>`, that runs a short list of
   commands against one hosted account instead of the local store.
2. That list: `tracks add`, `tracks list`, `profile put`, `profile list`,
   `profile check` and `move`.
3. Two new `profile put` kinds, `resume_facts` and `resume_truth`, on
   both stores.

## Behavior

### Input

- `--owner` takes the account's Supabase user id, a UUID, as spec 141's
  `--owner` does (its Open decision 1). It goes before the command:
  `harrier --owner <user id> --track focused-scope profile put screening --file PATH`.
- The hosted store is the URL in `HARRIER_ACCOUNT_DATABASE_URL`, exported in
  the operator's shell. It is read from the process environment only. A line
  for it in `.env` is skipped by `load_project_env`, as
  `HARRIER_IMPORT_DATABASE_URL` is (spec 141, amendment 4).
- Its login role must pass the run login's checks
  (`login_role_problems`, `services/api/src/harrier/loginrole.py`, spec
  134). The run login, `harrier_run`, is the documented choice. The
  migrator's URL is refused.
- Without `--owner`, every command runs as it does today.
  `HARRIER_ACCOUNT_DATABASE_URL` is never read without `--owner`.

### What runs

With `--owner`, the command opens `open_store(owner=...)` (spec 106) on
that URL. Every transaction runs as that owner under the owner's row
policy. The command then calls the same domain function it calls locally:

| Command | Domain function |
|---|---|
| `tracks add` | `add_track` (`services/api/src/harrier/tracks.py`) |
| `tracks list` | the local command's reader |
| `profile put` | `put_framing`, `put_application_profile` (`services/api/src/harrier/resume/framing.py`), `put_screening` (`services/api/src/harrier/screening/focused.py`), and the new facts and truth writers |
| `profile list` | the local command's reader |
| `profile check` | the local command's check |
| `move` | `move_job` (`services/api/src/harrier/tracker/store.py`) |

The rules each one enforces locally hold unchanged: one active focused
track (spec 137), a screening document only on a focused track, a focused
profile of at most three sentences (spec 140), and the move refusals and
conflicts (spec 138). No SQL is written for the hosted case alone. If a
function is not portable today, it is made portable under spec 112's rules
in this change.

`--track` names a track of that account by slug, as it does locally.
`move` takes the hosted job id, the one the web app shows. The import gave
jobs new ids (spec 141), so a local id names a different job.

With `--owner`, a command takes no host lease and asks the Docker engine
nothing (specs 061, 075). Those rules protect the local SQLite file, and
this command never opens it. The local container may run.

Any other command given `--owner` is refused before anything opens:
`harrier <command> does not run against a hosted account (spec 144)`,
exit 1.

### `profile put resume_facts` and `profile put resume_truth`

- `resume_facts` takes a JSON file and `resume_truth` a Markdown file. Both
  are shared documents, owned by no track, so `--track` is refused for them
  (exit 2).
- The new document replaces the stored one only if the content check that
  `profile check` runs passes for every track of the account with the new
  document in place. A failure prints the check's lines, exits 2, and stores
  nothing. So a truth edit that leaves a track's bullet without evidence is
  refused, naming the track and the bullet.
- On the local store both run as the other `profile put` kinds do: delegated
  into the container when it runs (spec 074).

### Output

Each command prints what it prints locally. With `--owner`, no line names
the owner id, the URL or the host. A refusal prints one line to stderr. Exit
statuses are the local command's: 0, 1 for a refusal, 2 for usage and
document errors.

## Failure modes

| Case | Result |
|---|---|
| `--owner` without `HARRIER_ACCOUNT_DATABASE_URL` in the environment | Refused, naming the variable, exit 1; nothing opens |
| The variable set only in `.env` | Treated as unset, exit 1 |
| `--owner` that is not a UUID | Usage error, exit 2 |
| The migrator's URL, or a login past the row policy | Refused before connecting as the owner, exit 1 |
| A user id with no account (no track visible) | Refused as an account that does not exist or has not been invited, exit 1 |
| The hosted store behind the code's schema version | Refused by spec 103's check, exit 1 |
| A command outside the list, such as `discover` | Refused before anything opens, exit 1 |
| A second active focused track | Refused as locally (spec 137), exit 1 |
| `move` with a job id not on the source track | Refused, `no job <id> on track <slug>`, exit 1, as locally |
| `move` with a local job id that names another hosted job on the source track | Runs on that job. The id is the operator's to check; see Honest limitations |
| `move` into a track that holds the same posting | Refused as a conflict, as locally (spec 138) |
| A truth edit that leaves a bullet without evidence | Refused, naming track and bullet, exit 2; nothing stored |
| `--track` with `resume_facts` or `resume_truth` | Usage error, exit 2 |
| The connection drops mid-command | The transaction rolls back; the account is as it was |
| Two commands for one account at once | Each is one transaction under the store's own locking, as two web requests are |

## Acceptance criteria

- [ ] `tracks add --kind focused` with `--owner` creates the track in that
      account and in no other, and a second active focused track is refused
      (planned test_account_writes_adds_a_focused_track_to_one_account).
- [ ] `profile put screening`, `resume_framing` and `application_profile`
      with `--owner` store the documents for the named track of that
      account, and a focused framing of four profile sentences is refused
      with nothing stored
      (planned test_account_writes_puts_track_documents).
- [ ] `move` with `--owner` moves a hosted job and writes its `moved`
      event, on the CI Postgres service as on SQLite
      (planned test_account_writes_moves_a_job_on_both_stores).
- [ ] `profile put resume_facts` and `profile put resume_truth` replace the
      shared document on both stores, and a truth edit that leaves a
      track's bullet without evidence is refused with exit 2 and nothing
      stored (planned test_profile_put_shared_documents_checks_every_track).
- [ ] With `--owner`: an unset variable, a variable set only in `.env`,
      the migrator's URL and an unknown user id each exit 1 with nothing
      written; a malformed `--owner` exits 2
      (planned test_account_writes_refuses_before_writing).
- [ ] `discover` given `--owner` exits 1 with the spec 144 sentence and
      opens nothing (planned test_account_writes_refuses_other_commands).
- [ ] No output line of a run with `--owner` contains the owner id, the URL
      or the host (planned test_account_writes_prints_no_identity).
- [ ] With `--owner`, no host lease is taken and the Docker engine is not
      asked, so the commands run while the local container runs
      (planned test_account_writes_ignores_the_local_container).
- [ ] `docs/deploy.md` says how to run these commands against staging, in
      a section beside "Importing a local store into an account".

Tests use synthetic owners and rows only, on the CI Postgres service, like
spec 141's.

## Out of scope

- Any other command against a hosted account: `tailor`, `cover-letter`,
  `discover`, `digest`, outreach and Gmail. Hosted runs stay spec 106's.
- Browser controls for focused tracks, profile documents or moves.
- An API route for any of these writes.
- Editing `candidate`, `story_bank` or the other shared kinds by command.
- Resolving an account by email.
- Copying documents between the local store and an account.
- Production. The same command works there once its project exists, as
  spec 141's does; that is a run, not a change.

## Honest limitations

- `move` trusts the job id it is given. After the import the local and
  hosted ids differ, and a local id can name a real hosted job. Two things
  limit the damage: a job not on the source track is refused (`no job <id>
  on track <slug>`), and a wrong move can be moved back. The command prints
  the id and the two tracks, not the company, so the operator reads the id
  from the web app first.
- The truth check guards against an edit that breaks a track's bullets. It
  cannot tell whether a new truth line is true. That stays the operator's
  judgement, as it is for every truth source.
- The run login reaches every table under the policy. A bug in a domain
  function could write any of the owner's rows, as one in a web route
  could. It cannot reach another owner's rows.

## Open decisions for Akin

1. **The variable's name.** Recommended: `HARRIER_ACCOUNT_DATABASE_URL`,
   separate from spec 141's `HARRIER_IMPORT_DATABASE_URL`, so a shell left
   holding one cannot run the other command by accident.
2. **The truth and facts kinds.** Recommended: in this spec, because the
   focused track's documents need a GitHub link and an entity line
   (spec 140) that only a truth edit can add. The alternative is a separate
   spec for them first.
3. **`tracks list`, `profile list` and `profile check` with `--owner`.**
   Recommended: included. They only read, and they are how the operator
   checks a write without opening the browser.

## Migration

None. Without `--owner` every command behaves as today. The two new
`profile put` kinds are additions.

## Proof / origin

- Akin's decision of 2026-10-15: the focused-scope track and its documents
  live on staging only.
- The refusal today: `POSTGRES_NOT_YET` in `main`,
  `services/api/src/harrier_cli/main.py`.
- The browser's kinds: `BROWSER_KINDS`,
  `services/api/src/harrier_api/tracks_routes.py`.
- The owner connection: `open_store(owner=...)`,
  `services/api/src/harrier/connection.py` (spec 106). The login checks:
  `services/api/src/harrier/loginrole.py` (spec 134).
- Spec 141's refusal of an account with rows:
  `services/api/src/harrier/tracker/importlocal.py`.
