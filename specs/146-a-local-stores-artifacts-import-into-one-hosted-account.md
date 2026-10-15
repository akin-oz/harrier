---
spec: 146
title: A local store's artifacts import into one hosted account
status: accepted
approved: yes
milestone: M10
depends: [047, 107, 111, 141]
---

# Spec 146: A local store's artifacts import into one hosted account

This spec waits. Nothing here is built until spec 107's second pull request
(the hosted store and its operator binding) is built, because the
`SupabaseFileStore` this command writes through does not exist before it.
Approval of this text can come earlier.

## Problem

Spec 141 copied a local store's rows into one hosted account and left the
files out (its Open decision 2, taken 2026-10-15: artifacts move in a
follow-up once spec 107 is built). Akin decided on 2026-10-14 that artifacts
come too: the generated resumes, cover letters and answers under
`data/resumes`, `data/cover-letters` and `data/answers`.

After spec 141 alone, every imported job shows its artifacts as absent.
`artifacts_for_job` in `services/api/src/harrier/artifacts.py` lists seven
kinds for a job, present or not, and `read_artifact` in
`services/api/src/harrier_api/app.py` answers 404 with "run <operation> to
produce it" for one that is missing. Producing them again means running the
tailoring, letter and answers operations again. A regenerated resume may
differ from the one the operator already sent.

Spec 107 supplies the bucket and the seam but moves nothing. Its Migration
says so: no local file is uploaded, and moving a data directory to a hosted
owner "would be an import, which no spec offers". This spec is that import.

Three facts about the code decide its shape.

- **An artifact is found by name, not by job id.** `_paths_by_kind` in
  `services/api/src/harrier/artifacts.py` reads the job's `company` and
  `title`, and for a resume also the track's resume bundle name through
  `load_bundle`. It builds each name with the writers' own helpers:
  `resume_paths_for` (`services/api/src/harrier/resume/tailor.py`),
  `cover_letter_paths_for` (`services/api/src/harrier/apply/letters.py`),
  `answers_path_for` (`services/api/src/harrier/apply/answers.py`) and
  `report_path_for` (`services/api/src/harrier/offers/evaluate.py`). Nothing
  stores a job id in a file name or in a file. Spec 141's id rewrite
  therefore touches no artifact, and no artifact needs renaming.
- **The hosted object name is not the file name.** Spec 107 names an object
  `<owner>/<name>`, and for the resume, letter and answers families `<name>`
  is a SHA-256 of the key (its Behavior, "Keys and object names"), so that a
  candidate's name never reaches Storage. Only the seam can build that name.
  A person copying files in the dashboard would produce objects the app
  cannot find.
- **The policy confines every object to the owner's prefix.** Spec 107's
  `harrier_files_owner_only` matches the first path segment to
  `auth.uid()`. An import must therefore write under the account's own
  prefix and nowhere else, as spec 141's import writes only the owner's
  rows.

## Scope

1. A CLI command, `harrier store import-artifacts --owner <user id>`, run
   on the operator's machine after spec 141's `harrier store import-local`
   (Open decision 5 weighs a flag on that command instead).
2. It copies the regular files directly inside `data/resumes`,
   `data/cover-letters` and `data/answers` into the account's Storage
   prefix, through spec 107's seam, with a `LocalFileStore` on the reading
   side and an operator-bound `SupabaseFileStore` on the writing side.
3. It is idempotent: an object already present with the same bytes is
   skipped, and one present with other bytes is never overwritten.
4. It refuses an account whose rows have not been imported, before it
   uploads anything.
5. It reports counts and families, never a file name.
6. It deletes and changes nothing locally.

## Behavior

### Input

- `--owner` takes the account's Supabase user id, a UUID, as spec 141's and
  spec 144's `--owner` do. Not an email address.
- The hosted database is the URL in `HARRIER_IMPORT_DATABASE_URL`, exported
  in the operator's shell, with the login role spec 141 requires
  (`login_problems` in `services/api/src/harrier/tracker/importlocal.py`).
  `load_project_env` in `services/api/src/harrier_cli/main.py` already skips
  that key, so a line in `.env` is never read. The command uses the database
  only to read: it writes no row.
- The Storage project and credential are `HARRIER_SUPABASE_URL` and
  `HARRIER_SUPABASE_SECRET_KEY` from the shell, validated as `read_admin`
  validates them (`services/api/src/harrier/operator.py`): the URL is an
  https origin and nothing else, and a secret key defined in `.env` is
  refused, because the local container reads that file. Open decision 1
  weighs using the owner's own access token instead.
- The local files are the data directory's three directories, as every
  host command finds them (`HARRIER_DATA_DIR`, else `data/`).

### What it checks before writing

In this order, each refusal naming what failed and writing nothing:

1. The container does not own the data directory. The command is a host
   command in the CLI's command table, never delegated into the container,
   as `import-local` is (`services/api/src/harrier_cli/main.py`). It calls
   `check_database_ownership` (`services/api/src/harrier/db.py`) on the
   default database path, as `import_local` does, so a running container
   refuses it: a run inside it could be writing an artifact as the command
   reads it. The command never opens the SQLite file.
2. The Storage variables above are present and valid.
3. The database login passes spec 134's checks, then the owner connection
   opens (spec 106), at the code's schema version (spec 103's check).
4. **Rows first.** Inside the owner's read-only transaction, the owner sees
   a track, and sees at least one job. An owner that sees no track is
   refused as an account that does not exist or has not been invited. An
   owner that sees a track and no job is refused with a sentence that says
   to run `harrier store import-local` first. Without jobs no name resolves
   and every artifact would be copied unreachable.
5. At least one of the three directories exists. None existing is refused
   as probably the wrong machine.
6. **The files fit the account.** The command builds the set of names the
   app would look for, from the account's own rows: for every job the owner
   sees, with its track's scope, every key that `resume_paths_for`,
   `cover_letter_paths_for` and `answers_path_for` return (every sidecar, not
   only the served kinds). If files exist and none of them is in that set,
   it refuses before the first upload: the account holds none of these
   artifacts' jobs, so `--owner` is probably not the account `import-local`
   filled.

### What it copies

For each family in the order `resumes`, `cover-letters`, `answers`, and each
regular file directly in its directory, in name order. A subdirectory is not
entered.

1. **Skipped without a request**, each counted under its reason and never
   named:
   - a name that begins with a dot (the PDF gate's temporary name,
     `render_validated_pdf` in `services/api/src/harrier/resume/pdf.py`, and
     the operating system's own files) is not counted at all;
   - a name outside spec 107's key grammar (`[A-Za-z0-9._-]+` per segment),
     or an extension outside the five the bucket allows (`.pdf`, `.md`,
     `.html`, `.json`, `.jsonl`; the extension is the text after the last
     dot): counted as "other type or name";
   - a file of zero bytes: counted as "other type or name";
   - a file larger than the seam's limit, 6,000,000 bytes (spec 107): counted
     as "over the size limit". The command reads the limit and the extension
     table from the seam and restates neither.
   Nothing is truncated, converted or split. The run goes on to the next
   file.
2. The key is `<directory>/<file name>`, the key the writers use, and the
   command never builds an object name: `SupabaseFileStore` does.
3. `stat(key)` on the hosted store.
   - Absent: the local bytes are read and sent with `put`. Afterwards
     `stat(key)` must report the local size. A different size stops the run
     (Failure modes).
   - Present: `read(key)` and compare bytes. Equal: counted "already
     there". Different: the object is left as it is and counted "differs
     from the hosted object". The command never overwrites, because a
     hosted run may have produced a newer artifact under the same key
     (Open decision 7).
4. Each file is classified by the account's key set from check 6 as
   attached (its key is in the set) or unattached. An unattached file is
   copied like any other (Open decision 6).

The PDF gate is not run again. Spec 067 guarantees no PDF sits at a local
key unless the gate passed when it was written, and the import copies bytes
unchanged. It does not guarantee that for a file placed by hand.

### How an imported artifact stays attached to its hosted job

Attachment is computed at read time, from three things the import already
carries or never touches:

- the hosted job's `company` and `title`, copied as written by spec 141
  (`services/api/src/harrier/tracker/importlocal.py`, writes step 3), so the
  slug of `<company>-<role>` is the local one;
- for a resume, the candidate name in the track's resume bundle, a
  `profile_documents` row copied by spec 141 with only its `track_id`
  rewritten, so `<name>-<company>-<role>` is the local one;
- the key, which the seam turns into the same hosted object name for the
  import's write and for the artifact route's read, because both go through
  `files()` or its constructor with the same owner (spec 107, "Keys and
  object names").

So no id is rewritten and no file is renamed. The routes in
`services/api/src/harrier_api/app.py` (`list_artifacts`, `read_artifact`)
find the object for hosted job `J` exactly as they find the file for local
job `J'`. Two jobs of two tracks at one company and role share one file,
locally and hosted (spec 107, Out of scope).

Attachment breaks only when an input to the name differs: a job's company or
title edited after the import, a bundle name edited, or a `slugify` that
changed between the local and the deployed code. Resumes build their slug
with `resume/markdown.py`'s `slugify`, which transliterates, and letters and
answers with `apply/answers.py`'s, which does not, so a change to either
shows in one family first. The "unattached" count is how the operator sees
it.

### Output

One line per family, then the reasons, then attachment, then the verdict:

```
resumes: <number> copied, <number> already there
cover-letters: <number> copied, <number> already there
answers: <number> copied, <number> already there
not copied, over the size limit: <number>
not copied, other type or name: <number>
not copied, differs from the hosted object: <number>
attached: <number>
unattached: <number>
imported
```

The last line is `imported` when no file was left uncopied, and
`incomplete` otherwise (Open decision 4). `attached` and `unattached` count
files copied or already there. Nothing printed names the owner, a file, a
company, the candidate, the host or a key. A refusal prints one line to
stderr and exits 1; a usage error exits 2. The command sets up no log file,
and writes no log line that carries a key (spec 144, amendment 3, does the
same).

### Interruption

There is no transaction. Each upload is whole or absent (spec 107: Storage
stores the whole object or nothing). A run killed at any point leaves the
objects it finished, each complete, and nothing local changed and no row
written. Running the command again skips every object already there with the
same bytes and continues. The only state is in Storage.

## Failure modes

| Case | Result |
|---|---|
| The local container is running | Refused by spec 061's check; nothing read, nothing written |
| `HARRIER_IMPORT_DATABASE_URL` unset, set only in `.env`, or the migrator's | Refused before connecting as the owner, exit 1 |
| `HARRIER_SUPABASE_URL` or `HARRIER_SUPABASE_SECRET_KEY` unset or malformed, or the secret key defined in `.env` | Refused naming the variable, never its value, exit 1 |
| `--owner` that is not a UUID, or none | Usage error, exit 2 |
| No such user (no track visible) | Refused as an account that does not exist or has not been invited, exit 1 |
| The account has a track and no job | Refused: run `harrier store import-local` first, exit 1 |
| None of the three directories exists | Refused as probably the wrong machine, exit 1 |
| Files exist and none fits a job of the account | Refused before the first upload, exit 1 |
| One of the three directories is absent | Its line reads zero copied and zero already there; the run goes on |
| A file over the limit, of another type, with another name, or empty | Skipped and counted; the run goes on; the verdict is `incomplete` |
| The hosted object differs from the local file | Left as it is, counted; the verdict is `incomplete` |
| The hosted object is the same bytes | Skipped, counted "already there" |
| Storage unavailable (`FileStoreUnavailable`: 5xx, timeout, refused) | The run stops at that file; the lines so far print; one line on stderr names the step and the family, exit 1; running again resumes |
| Storage refuses the credential (`FileStoreDenied`) | The run stops the same way; the message says the key or the project is wrong |
| The upload's size read back differs from the local file | The run stops, exit 1; the object is left as Storage holds it and a rerun finds it differs |
| A local file cannot be read (permission, vanished) | The run stops, naming the family, exit 1 |
| The process is killed | Objects finished stay, each whole; a rerun resumes |
| Two copies of the command at once | Each uploads the same bytes under the same key; the second `put` changes nothing. No lock is taken |
| `harrier operator erase` for the account afterwards | Refuses while any object remains under the prefix, until spec 107's build replaces its list-only check (spec 107, Note of 2026-10-15) |

## Acceptance criteria

All of these run against spec 107's fake Storage server and the CI Postgres
service, with synthetic names and bytes only, like spec 141's.

- [ ] With rows imported, files in the three directories copy into the
      account's prefix, and the artifact route for each hosted job then
      lists and serves them with the same bytes and the same media types
      (planned test_import_artifacts_copies_the_three_families).
- [ ] An imported artifact is found for its hosted job whose id differs from
      the local one, with no rename and no rewrite: the resume, letter and
      answers for one company and role resolve for the job that holds
      them, and a job with another role does not see them (planned
      test_import_artifacts_stay_attached_to_the_hosted_job).
- [ ] A second run uploads nothing, counts every file "already there" and
      prints `imported` (planned test_import_artifacts_is_idempotent).
- [ ] A hosted object with other bytes under a key is left unchanged,
      counted "differs from the hosted object", and the verdict is
      `incomplete` (planned test_import_artifacts_never_overwrites_a_different_object).
- [ ] A run stopped by an unavailable Storage after some uploads leaves
      those objects whole, and a second run uploads only the rest (planned
      test_import_artifacts_resumes_after_an_interruption).
- [ ] A file over 6,000,000 bytes, a file with an extension outside the
      bucket's five, a name outside the key grammar and an empty file are
      each skipped without a request, counted under their reason, and the
      run finishes the other files and ends `incomplete` (planned
      test_import_artifacts_skips_what_storage_would_refuse).
- [ ] A dot file, a subdirectory, and the other `data/` directories (logs,
      LLM logs, caches, reports, the corrupt backup, and
      `resumes-corrected`) are never read or counted (planned
      test_import_artifacts_leaves_other_directories_alone).
- [ ] An account with a track and no job is refused with the
      `import-local` sentence and nothing uploaded; an unknown user id is
      refused; files that fit no job of the account are refused before the
      first upload (planned test_import_artifacts_needs_the_rows_first).
- [ ] Every upload goes under `<owner>/` for the `--owner` given and no
      other prefix, and no object name contains a local file name or a
      slug (planned test_import_artifacts_writes_only_under_the_owners_prefix).
- [ ] The output and the captured log carry counts and family names only: no owner id, host, key, file name, company or candidate name
      appears (planned test_import_artifacts_prints_no_file_name).
- [ ] The local files' bytes and modification times are unchanged and none
      is deleted (planned test_import_artifacts_never_writes_the_local_files).
- [ ] A running container, an unset database URL, the migrator's URL,
      unset or malformed Storage variables and a secret key defined only in
      `.env` each exit 1 with nothing read from the data directory and
      nothing uploaded; a malformed `--owner` exits 2 (planned
      test_import_artifacts_refuses_before_writing).
- [ ] A Storage that denies the credential stops the run with exit 1 and a
      message that names neither the key nor the object (planned
      test_import_artifacts_stops_when_storage_denies_it).
- [ ] `docs/deploy.md` says how to run it against staging and production,
      after `import-local`, in the section "Importing a local store into an
      account".
- [ ] `just check` passes.

## Proof / origin

- Akin's decision of 2026-10-14: artifacts come too, meaning generated
  resumes, cover letters and answers; logs, LLM logs, discovery caches,
  reports and the corrupt backup do not. Spec 141's Open decision 2 left
  `data/resumes-corrected` undecided; it is Open decision 2 here.
- `specs/141-a-local-store-imports-into-one-hosted-account.md`: Open
  decision 2 and Decisions taken (2026-10-15); the copy of every column as
  written, and the id maps.
- `specs/107-artifacts-and-run-state-in-storage.md`: the seam, key grammar,
  hashed object names, the bucket `harrier-files`, the 6,000,000 byte limit
  and five types, the errors, and its Migration; its Note (2026-10-15) asks
  for an operator binding of the seam outside a request.
- `specs/111-sign-up-export-and-deletion.md` and
  `services/api/src/harrier/operator.py`: the
  operator's secret key, its refusal when the key is in `.env`, and the
  list-only Storage check in `erase` that imported objects will trip.
- `services/api/src/harrier/artifacts.py`, `resume/tailor.py`,
  `apply/letters.py`, `apply/answers.py`, `offers/evaluate.py` and
  `harrier_api/app.py`: how an artifact is named and found.
- `specs/067-failed-render-leaves-no-pdf.md` and
  `services/api/src/harrier/resume/pdf.py`: no PDF at a key unless the gate
  passed.
- `services/api/src/harrier_cli/main.py`: `_store_import_local`, the
  command table's `_HOST` class, and `load_project_env`.

## Out of scope

- Everything outside the three directories: logs, LLM logs, discovery
  caches and seen state, job descriptions, reports (Open decision 3), the
  2026-09-18 corrupt backup, `resumes-corrected` (Open decision 2), scoring
  models, mail state, outreach files, run files and every other `data/`
  entry.
- Importing rows: spec 141, which this command follows.
- Overwriting a hosted object that differs, a refresh flag, and any delete
  of a hosted or local object.
- Resumable uploads or a larger limit: spec 107's Open decision 7.
- A web route or Settings page for the import.
- Re-running the PDF gate, re-slugging, or renaming files.
- Importing hosted files back to local: spec 117's export covers leaving.
- Changing the artifact routes, the keys or the object names.
- Production. The same command runs there once its project exists; that is
  a run, not a change.

## Open decisions for Akin

1. **Which identity writes to Storage.** Recommended: the operator's secret
   key, `HARRIER_SUPABASE_SECRET_KEY`, through the operator binding that
   spec 107's Note of 2026-10-15 already asks of the seam for `harrier
   operator erase`, with every object confined to `<owner>/` by code and
   proved by a test. The operator already holds that key in the shell for
   `invite` and `erase`. The cost: the policy is not the boundary during the
   import, since a secret key bypasses it; only the seam and its test are.
   Objects it writes carry no `owner_id` in Storage's metadata, and the
   policy matches the folder name, not that column, so the owner's own token
   should read them (Honest limitations). Alternative: the owner's own access
   token, copied from a signed-in session into an environment variable, with
   the publishable key. The policy stays the boundary, as the row policy is
   for spec 141, and no new binding is asked of spec 107. The cost: an
   account made by `invite` has no password, so the operator takes the token
   from a browser session, and it expires in an hour (spec 104), so a long
   copy needs a rerun with a fresh one.
2. **`data/resumes-corrected`.** Left open by spec 141. Recommended: do not
   copy it. Nothing under `services/api/src`, `apps/web` or `config` names
   that directory (a search over the repository outside `data/`), spec 107's
   family table has no key family for it, and so no hosted route could read
   it. Alternative: copy it under a new family, which needs spec 107 to name
   the family, its keys and whether its names are hashed. That is a change to
   spec 107, not to this one.
3. **Reports.** Your decision of 2026-10-14 left reports out, and this spec
   follows it. One thing you may not have weighed: `artifacts_for_job`
   serves an `evaluation` kind from `data/reports` through `report_path_for`,
   so the Apply page lists it, and after the import it reads as absent for
   every job. Recommended: keep it out as you decided, and accept that one
   of the seven kinds shows absent until an evaluation runs hosted.
   Alternative: bring `reports` in as a fourth family; it is the same
   generated Markdown, the same route, and one more line in the output.
4. **The exit status when a file is not copied.** Recommended: exit 1 and
   `incomplete` when any file was over the limit, of another type or name,
   or differs from the hosted object, so a lost artifact is never a quiet
   success. Cost: a stray file the operator does not care about keeps the
   command at exit 1 on every rerun. Alternative: exit 0 and `imported`,
   with the not-copied counts printed.
5. **The command's shape and variables.** Recommended: a sibling,
   `harrier store import-artifacts`, because `import-local` refuses a
   non-empty account and this command requires one, and the two ask for
   different credentials. It reuses `HARRIER_IMPORT_DATABASE_URL`,
   `HARRIER_SUPABASE_URL` and `HARRIER_SUPABASE_SECRET_KEY` by their
   existing names. Alternative: an `--artifacts` flag on `import-local`, with
   its checks inverted by the flag.
6. **Copy files no hosted job resolves to.** Recommended: copy them and
   count them as unattached. Nothing local is lost when the operator
   retires the local store, and a job added later at the same company and
   role finds its file. Cost: unreachable personal documents sit in the
   bucket. Alternative: copy only attached files.
7. **Never overwrite.** Recommended: an object that differs is left and
   counted. Alternative: a flag that overwrites, which spec 107's seam
   already allows (its `put` upserts) and which risks replacing an artifact
   a hosted run made since the rows were imported.

## Decisions taken (2026-10-15)

Akin approved the spec with every open decision taken as recommended: the
operator's secret key writes, `data/resumes-corrected` is not copied,
reports stay out, a file not copied exits 1 with `incomplete`, the command is
a sibling of `import-local`, files no hosted job resolves to are copied, and
a differing hosted object is never overwritten.

## Honest limitations

- **The Storage identity is unproven.** Spec 107 lists, for its staging
  project, that Storage runs a token's request as `authenticated` and that
  its policy reads back what was written. This spec adds one more: an object
  uploaded with a secret key (no `owner_id`) is read and listed by the
  owner's own token under `harrier_files_owner_only`. The fake server in
  tests cannot show it. Until staging does, the claim that an imported
  artifact is served is a claim about the fake.
- **Attachment is by name.** An edit to a job's company or title, to the
  resume bundle's name, or to either `slugify` after the import detaches
  files, as it would locally. The command checks the account's names once,
  at the start. A partial mismatch passes the check that refuses a total one.
- **No conditional write.** The command checks that an object is absent and
  then uploads with the seam's upsert. A hosted run that writes the same key
  between the two steps is overwritten. The window is one request.
- **A rerun downloads every object already present** to compare bytes. Each
  is at most 6,000,000 bytes. No hash is stored.
- **The limit and the types are spec 107's guidance, not a measurement.** No
  file in the local directories was measured for this spec, and no count of
  them is stated. The first run is the measurement; a large PDF is reported
  as not copied.
- **Skipped files are not named.** The count says some exist and the family
  says nothing about which. The operator finds them in the local directory.
- **No undo.** The command deletes no object. Removing imported objects is
  spec 111's erase once spec 107 builds its delete, or the owner's own
  Storage calls. Until then `harrier operator erase` refuses an account that
  holds one.
- **Supabase's database backups do not include Storage objects** (spec 107).
  The command deletes no local file, and the operator should keep the local
  directories until spec 110 gives the bucket a backup.
- **A file placed by hand copies if its name and type fit.** Its PDF was
  never gated.
- **The import reads the local files with no lock on them.** The container
  check refuses a running container; another process on the machine writing
  a file during the run is the operator's to avoid.
- **Line numbers are not cited.** Spec 107's text says its own are refreshed
  when it is built, and this spec names functions instead.

## Migration

None for the local product or for any account that has no artifacts. The
operator runs the command once per account, after `harrier store
import-local` and after spec 107's hosted store is deployed to that
project, and may run it again at any time. Production gets the same command
once its project exists. Spec 141 stays as written.

## Changes to approved specs

None is written into those specs here. Each is an amendment Akin approves
with this spec.

- **Spec 107:** its Note of 2026-10-15 binds the seam to one owner outside a
  request for the operator's list and delete. This spec needs the same
  binding to read, stat and put under that prefix, and to be constructed
  alongside a `LocalFileStore` rather than through `files()`, since the
  command runs in local mode. It also needs the seam to export its size
  limit and its extension table. Its acceptance criterion that no local
  command reads `HARRIER_SUPABASE_PUBLISHABLE_KEY` or imports `httpx` at
  runtime gains an exception for `harrier operator` and `harrier store
  import-artifacts`, which send Storage requests from the operator's machine
  (`services/api/src/harrier/operator.py` imports `httpx` already).
- **Spec 141:** none. Its Open decision 2 is answered here.
