---
spec: 107
title: Artifacts and run state in Storage
status: accepted
approved: yes
milestone: M10
depends: [040, 047, 067, 104, 105, 112]
---

# Spec 107: Artifacts and run state in Storage

Refined 2026-10-10 after approval of the stub. This text is an amendment
to the approved stub and goes to Akin before implementation.

## Problem

Resumes, letters, answers, drafts, summaries, seen-state and scoring models
are files on the local disk. A hosted API machine has no per-owner disk.
ADR-013 decision 6 moves them to a private Supabase Storage bucket under
owner-prefixed paths.

A track id is no longer unique across owners on Postgres (spec 105), so
every path built from one, such as academic discovery's run and incoming
directories (`services/api/src/harrier/academic/discovery.py:153`, `:172`),
must sit under the owner's prefix hosted.

Line numbers below are from `main` at commit `44ac0d7` (2026-10-10),
except the `harrier_cli/main.py` and `harrier_api/app.py` ones, refreshed
to `main` at `524dd8a` on 2026-10-11. Counts say how they were taken.

### Every file the domain writes today

Every domain file lives under one directory, `data_dir()`
(`services/api/src/harrier/db.py:35-48`): `HARRIER_DATA_DIR` when set, the
demo's temporary directory in demo mode, otherwise `data/` at the
repository root. 39 lines in 26 files under `services/api/src` call
`data_dir()` (`grep -rn "data_dir()"`, the definition excluded).

Writes were found with three greps over `services/api/src`:

- `write_text(`, `write_bytes(`, and `open(...)` or `.open(...)` with a
  mode of `w`, `wb`, `a`, `ab`, `x`, `xb` or `w+`: 40 lines.
- Calls of the two atomic helpers, `write_json_atomic(` and
  `write_bytes_atomic(` (`services/api/src/harrier/atomicio.py:41-79`):
  9 lines.
- Other writers: Playwright's `page.pdf(` (one line,
  `services/api/src/harrier/resume/pdf.py:38`) and `shutil` copies
  (`backup.py`, `cutover.py`, `mail/run.py:161`).

They fall into two groups.

**Personal artifacts and run state (this spec moves them).** Each row is a
key family: the path relative to `data_dir()`.

| Family | Writer | Read by | Write kind today |
|---|---|---|---|
| `resumes/<slug>.{md,html,pdf,metadata.json,evaluation.md}` | `resume/tailor.py:51-76`, `:135-176` | `artifacts.py:85-144`, `harrier_api/app.py:921-981` | plain write; the PDF through `render_validated_pdf` (`resume/pdf.py:87-111`) |
| `cover-letters/<slug>.{md,html,pdf}` | `apply/letters.py:197-215`, `:573-599` | the same artifact routes | plain write; PDF as above |
| `answers/<slug>.md` | `apply/answers.py:169-170`, `:695-699` | the artifact routes | plain write |
| `reports/<slug>.md` | `offers/evaluate.py:107-117`, `:227-229` | the artifact routes | plain write |
| `evaluations/audit.jsonl` | `offers/batch.py:34-62` | nothing in the code | append |
| `outreach/<slug>-candidates.json` | `outreach/discovery.py:423-442`, `:471-520`, `outreach/backfill.py:69-107` | `harrier_api/outreach_routes.py:166-190`, `outreach/discovery.py:446-520` | plain write, read-modify-write |
| `outreach/<slug>.{json,md}` | `outreach/drafts.py:122-123`, `:523-531` | nothing in the code | plain write |
| `outreach/messages/<slug>.{json,md}`, `outreach/targets.json` | `outreach/messages.py:450-494` | `outreach/messages.py:474-488` | plain write, read-modify-write |
| `descriptions/<hash>.json` | `screening/descriptions.py:62-85`, called from `capture.py:108`, `:155`, `screening/pipeline.py:330`, `screening/linkedin.py:266` | `tracker/store.py:495-508` (inside `set_status`), `resume/tailor.py:99`, `offers/batch.py:89`, `scoring/export.py:84` and others | atomic |
| `linkedin-page-verdicts/<hash>.json` | `screening/linkedin.py:126-147` | `screening/linkedin.py:131-141` | plain write |
| `discovery/<source>_seen.json`, `discovery/<track id>/<source>_seen.json` | `screening/seen.py:79-85`, `:154` | `screening/seen.py:100-160` | atomic |
| `incoming/<source>_latest.json`, `incoming/job_imports_run.json` | `discovery.py:75-76`, `:226-228`, `:485-487` | nothing in the code | plain write |
| `discovery/<track id>/<source>/runs/<run id>.json` | `academic/discovery.py:152-168`, pruned by modification time at `:165-167` | the pruning itself | atomic |
| `incoming/<track id>/<source>_latest.json` | `academic/discovery.py:171-172`, `:346-347` | nothing in the code | atomic |
| `scoring/active-model.json`, `scoring/models/*.json`, `scoring/reports/*.json` | `scoring/train.py:102-107`, `:333-335`, `:451-462` | `scoring/model.py:70-76`, `:284-313`, cached by `stat` at `:255`; `active_model_info` (`:337`) for the settings page (`harrier_api/settings_routes.py:514-516`) | atomic |
| `scoring/exports/[<track slug>/]features-<date>.jsonl` | `scoring/export.py:42`, `:140-147` | `scoring/train.py:110-112` (newest by name); `newest_export_date` (`scoring/export.py:49`) for the settings page (`harrier_api/settings_routes.py:536-542`) | atomic |
| `gmail-watch/seen_messages.json`, `gmail-watch/events.jsonl` | `mail/watch.py:163-172`, `:459-460`, `:513-523` | `mail/watch.py:448-456`, `harrier_api/mail_routes.py` | atomic; append and rotate |

All paths above are under `services/api/src/harrier/` unless they start
with `harrier_api/`.

The same modules read, test for, list or delete these files on 34 lines
(`grep -nE` for `read_text(`, `read_bytes(`, `is_file()`, `.exists()`,
`read_json_mapping(`, `.unlink(`, `glob(` and `.stat()` over 19 files:
the writer modules in the table except `mail/watch.py`, plus
`resume/pdf.py` and `artifacts.py`). Some of
those lines read a template or a run input rather than data; the pull
request lists each line and which it is.

**Files that stay where they are.** Each is local by nature or belongs to
another spec:

| Writer | Why it stays |
|---|---|
| `harrier_api/runs.py:302-331` (run inputs), `:576`, `:792-812` (run journal) | the run manager's own files; hosted runs are spec 106's |
| `harrier_api/localauth.py:64-95` (local API token) | hosted mode has no token file (spec 104) |
| `harrier/llm/providers.py:46-54` (LLM debug log, opt-in by `*_DEBUG`, `llm/config.py:161-167`) | hosted log rules are spec 110's |
| `harrier/logsetup.py:52-53` (log file) | hosted log rules are spec 110's |
| `harrier/mail/watch.py:645`, `harrier_cli/main.py:950` (Gmail OAuth token) | a credential: specs 106 and 109 |
| `harrier/mail/run.py:153-162` (copy of the old system's seen state) | local only |
| `harrier/tracker/export.py:85-104`, `harrier/profile/store.py:156-158`, `:196-212` (CSV and profile exports to a chosen directory) | local CLI; a hosted export is spec 111's |
| `harrier/backup.py`, `harrier/cutover.py`, `harrier/hostlease.py:91-98`, `harrier/schedule.py:369-371`, `harrier/reviewfollowup.py:404-503`, `harrier/parity/checklist.py:173-174`, `harrier_api/export_openapi.py:19-20`, `harrier_api/demo.py:64-68`, `harrier/db.py` | the local machine, development tools, or the SQLite store itself |

### What breaks hosted

- **Every writer names a local path.** A hosted API on Fly would write
  every owner's resumes into one machine's `data/`, readable by any code
  path that builds the same name, and gone when the machine is replaced.
- **Names collide across owners.** Artifact names are built from company
  and role (`resume/tailor.py:55-76`, `apply/letters.py:201-215`), and
  run state from a track id (`screening/seen.py:85`,
  `academic/discovery.py:153`, `:172`). Two owners applying to the same
  role, or two owners' track 2, would share one file.
- **One cache is keyed by path.** `scoring/model.py:255` caches the
  parsed model by the file's path string. With one key per family and no
  owner in it, owner A's model would score owner B's jobs.
- **PDF rendering needs a local file.** Chromium writes the PDF to a path
  (`resume/pdf.py:38`) and the gate runs `pdfinfo` on a path (`:70-71`).
- **File names carry people's names.** The resume name includes the
  candidate's name (`resume/tailor.py:69`), and outreach draft and message
  names include the contact's name (`outreach/drafts.py:508-520`,
  `outreach/messages.py:90-94`). Hosted, an object name travels in request
  paths to a vendor.

## Scope

**Delivery.** One spec, two pull requests in order (Open decision 6), as
spec 112 does. PR 1 is a refactor with no change in behavior. PR 2 is the
hosted behavior.

PR 1 (refactor, local behavior unchanged):

1. **One storage seam.** A new module,
   `services/api/src/harrier/filestore.py`, defines `FileStore`, the key
   rules, `files()`, `bind_files()`, `LocalFileStore` and the errors
   (Behavior).
2. **Every writer and reader in the first table goes through it.** Each
   family's directory function (`resumes_dir()` and its siblings) becomes
   a key prefix; each `write_text`, `write_*_atomic`, `open(..., "a")`,
   `read_text`, `is_file`, `glob`, `stat` and `unlink` on those paths
   becomes a call on `files()`. `atomicio.py` keeps its rules and is called
   by `LocalFileStore`.
3. **The PDF gate takes a key.** `render_validated_pdf` writes through the
   store; locally it renders beside the target and renames, as today.
4. **The artifact routes read through the seam.** Locally they still
   answer with `FileResponse` on the same path.
5. **The model cache is keyed by the store's qualified name** (Behavior).
6. **A characterization test** pins, for every family, the relative path,
   the bytes and the file mode a local write produces, written before the
   refactor and passing after it.

PR 2 (hosted):

7. **`SupabaseFileStore`** in `filestore.py`: the private bucket, owner
   prefixed object names, the request's own token.
8. **Binding.** Spec 104's authentication middleware, after a token
   verifies, binds a `SupabaseFileStore` for that owner for the request
   (Changes to approved specs). Hosted, an unbound `files()` refuses.
9. **Hosted PDF rendering** in a private temporary directory, uploaded
   only after the gate passes.
10. **A new Postgres-only migration (its version is the next free one
    when implemented), called "the storage migration" below,** in
    `services/api/src/harrier/tracker/schema.py`: the bucket and one policy
    on `storage.objects`. `SINGLE_DIALECT_MIGRATIONS` declares it, with
    its reason.
11. **The test shim gains Supabase's storage objects.**
    `services/api/tests/pg_support.py` installs a `storage` schema with
    `buckets`, `objects` and `foldername()` before migrating. No migration
    creates them.
12. **Configuration.** Hosted mode requires
    `HARRIER_SUPABASE_PUBLISHABLE_KEY`, one more of spec 104's start-up
    checks (Changes to approved specs). `.env.example` gains it, commented
    out, with a placeholder.
13. **Dependencies.** `httpx` joins spec 104's `hosted` group in
    `services/api/pyproject.toml`. It is already in the dev group
    (`services/api/pyproject.toml:40`).
14. **Contract.** Routes that read or write through the seam can answer
    503 when Storage is unavailable; the body joins the existing 503
    declaration, and `just contract` regenerates
    `packages/contract/openapi.json` and `packages/contract/src/schema.d.ts`.
    A guarded path, approved under this spec.
15. **Documents.** ADR-013 decision 6 gains an "Amended (spec 107)" note
    with the text under Behavior. `docs/architecture.md`'s hosted section
    names the seam, the bucket, the policy and its role.
16. **Findings that belong to other specs are written into them**, specs
    only, no code: the line under "Written into other specs" (Behavior)
    for spec 117, which is not approved, and the lines under "Changes to
    approved specs" for approved ones.

Classification: the new module and tests sit under `services/api/src/`
and `services/api/tests/`, already public in
`config/data-classification.json`. No new directory is added, and every
local file keeps its path under `data/**`, already never-in-git. The
bucket is not a repository path.

## Behavior

### The seam

`harrier.filestore` is the only module that turns a key into a file or an
object. A key is a relative POSIX path: one or more segments joined by
`/`, each matching `[A-Za-z0-9._-]+` and neither `.` nor `..`. Anything
else raises `InvalidFileKey` (a `ValueError`) on both stores, before any
file or request is touched. Every key built today passes: names come from
`slugify` or a hex digest, and the rest are literals in the code.

| Operation | `LocalFileStore` | `SupabaseFileStore` |
|---|---|---|
| `read(key) -> bytes \| None` | the file's bytes; `None` when absent | the object's bytes; `None` when Storage reports it absent |
| `put(key, data, *, atomic)` | `atomic=True`: `write_bytes_atomic` as today; `atomic=False`: a plain write, as today; parents created | one upload with upsert; whole or not at all whatever `atomic` says |
| `append(key, text)` | `open(..., "a")` as today | read, concatenate, upload |
| `delete(key)` | `unlink(missing_ok=True)` | remove; an absent object is not an error |
| `stat(key) -> Entry \| None` | size, modification time, and a version `(st_ino, st_mtime_ns, st_size)` | size, update time, and Storage's version of the object |
| `list(prefix) -> list[Entry]` | the files directly in that directory | the objects directly under that prefix, every page |
| `local_path(key) -> Path \| None` | `data_dir() / key` | `None` |
| `qualified_name(key) -> str` | the absolute path, as a string | `<owner>/<key>` |

Each call site passes `atomic` as it writes today: true where it calls
`write_json_atomic` or `write_bytes_atomic`, false where it calls
`write_text` or `open(..., "w")`. JSON stays serialized by the caller, so
the bytes do not change.

### Which store a call uses

- **Local mode** (`HARRIER_AUTH` unset or empty, spec 104): `files()`
  returns a `LocalFileStore` rooted at `data_dir()`, read at each call as
  today. `HARRIER_DATA_DIR`, demo mode (`harrier/demo.py:16-17`) and
  every test that points the data directory elsewhere behave as before.
- **Hosted mode** (`HARRIER_AUTH=supabase`): `files()` returns the store
  bound to the current context by `bind_files()`. With none bound it
  raises `FileStoreUnbound`. It never returns a `LocalFileStore`, so a
  path that forgot to bind fails instead of writing a shared disk.
- **Binding a request.** Spec 104's middleware, once the token verifies,
  binds `SupabaseFileStore(owner=<sub>, token=<the bearer token>)` with a
  context variable for the rest of that request and resets it when the
  response ends. The binding happens in the middleware, before routing,
  so synchronous routes that FastAPI runs in a thread pool inherit it.
- **Binding a run.** Spec 106's (decision D3, 2026-10-11). Binding a run
  to its owner includes the identity the run uses for Storage. This spec
  consumes that binding: in a hosted run, `files()` returns the store spec
  106's binding sets. This spec defines no run token and no run binding.
- **No domain function names an owner.** No function outside
  `harrier.filestore` and `harrier_api` takes an owner or a store
  parameter, and no domain module builds an object name. ADR-013
  decision 2 holds for files as it does for SQL.

### Keys and object names

Locally, the file for key `k` is `data_dir() / k`, exactly today's path.

Hosted, the object for key `k` is in bucket `harrier-files` under the
owner's prefix: `<owner>/<name>`, where `<owner>` is the verified `sub`
in its canonical lowercase form, and `<name>` is:

- for a **listed family**, the key itself. Listed families are those the
  code lists or orders by name: `discovery/<track id>/<source>/runs/`,
  `scoring/exports/`, `scoring/models/`, `scoring/reports/`. Their names
  are run ids, dates and digests.
- for every other family, the key's directory followed by the SHA-256 of
  the whole key in hex and the key's extension, so
  `resumes/<slug>.pdf` becomes `resumes/<64 hex digits>.pdf`. File names
  that carry a candidate's or a contact's name never reach Storage, its
  logs or its request paths (Open decision 5).

The name a person sees does not change. `ArtifactOut.filename` and the
download's `Content-Disposition` use the key's last segment, as they use
the path's name today (`harrier_api/app.py:945`, `:980`).

Track-qualified keys are safe hosted. `tracks` has the primary key
`(owner_id, id)` on Postgres (spec 105), so a track id is unique inside
one owner's prefix, and two owners' track 2 are two prefixes.

### The hosted store

- **Bucket.** `harrier-files`, private, created by the storage migration
  (below).
  Harrier creates no public URL and no signed URL. Every read goes through
  the API.
- **Requests.** Each call goes to `HARRIER_SUPABASE_URL` plus
  `/storage/v1`, with `Authorization: Bearer <the request's token>` and
  `apikey: <HARRIER_SUPABASE_PUBLISHABLE_KEY>`. No secret key, service
  role key or S3 access key is read or sent. Spec 104 already refuses to
  start with a `SUPABASE_SERVICE_ROLE_KEY` or a value beginning
  `sb_secret_` in the environment.
- **Uploads** set upsert, a content type from the key's extension, and
  ask for no caching. Extensions and types: `.pdf` `application/pdf`,
  `.md` `text/markdown`, `.html` `text/html`, `.json`
  `application/json`, `.jsonl` `application/x-ndjson`. Any other
  extension raises `FileStoreError` before a request.
- **Size.** A `put` or `append` larger than 6,000,000 bytes raises
  `FileTooLarge` before any request, naming the key's family. The bucket
  carries the same limit. Supabase recommends standard uploads up to 6 MB
  and resumable uploads above; the seam implements standard uploads only.
  The number is that guidance, not a measurement of harrier's files.
- **Timeout.** 30 seconds per request, chosen, not measured.
- **Errors.** Every hosted error is a `FileStoreError`, which subclasses
  `OSError`, so each caller's existing `except OSError` keeps its meaning:

| Storage answer | Raised |
|---|---|
| object absent, on read or stat | nothing; `read` and `stat` return `None` |
| 401 or 403 | `FileStoreDenied` |
| object over the bucket's limit | `FileTooLarge` |
| 5xx, timeout, connection refused | `FileStoreUnavailable` |
| anything else | `FileStoreError` |

  A message names the operation and the key's first segment only, never
  the owner, the token, the full key or the object name.

- **Append** reads the object, adds the text and uploads the whole. Two
  appenders at once can lose one append (Honest limitations).

### Callers keep their rules on read failure

| Caller | Absent | Damaged or unreadable, hosted unavailable included |
|---|---|---|
| seen state (`screening/seen.py:100-118`), mail state (`mail/watch.py:448-456`) | first run, empty | `DamagedStateError`, raised as spec 040 requires; never read as empty |
| description cache (`screening/descriptions.py:88-112`) | empty | empty, logged; a decision is never refused for it (spec 079 amendment, `services/api/tests/test_job_events.py::test_a_damaged_description_file_does_not_block_a_decision`) |
| LinkedIn verdict cache, staged candidates, outreach targets | empty | empty, as on `OSError` today |
| active model (`scoring/model.py:284-313`) | rules score (`MODEL_MISSING`) | rules score (`MODEL_INVALID`) |
| artifact routes (`harrier_api/app.py:921-981`) | listed as absent; read answers 404 as today | 503 when unavailable; 500 when denied |

`read_json_mapping` (`atomicio.py:84-100`) turns any `OSError` into
`DamagedStateError`, so the first row needs no new code beyond reading
through the seam.

### The model cache

`scoring/model.py` keys `_cache` and `_warned` by
`files().qualified_name(key)` and uses `stat(key).version` where it used
`(st_ino, st_mtime_ns, st_size)`. Locally the qualified name is the path
string it uses today. Hosted it includes the owner, so one owner's model
is never served for another's jobs, and each owner's missing model is
reported once.

### PDF rendering

`render_validated_pdf(html_text, key, render, validate)` keeps spec 067's
rule: the key never holds a PDF the gate has not passed.

- **Local** (`local_path(key)` is a path): unchanged. The earlier PDF is
  deleted, Chromium renders to a hidden temporary name beside it, the gate
  runs on that file, and a rename moves it in (`resume/pdf.py:87-111`).
- **Hosted** (`local_path(key)` is `None`): the earlier object is deleted
  first. Chromium renders into a new temporary directory created with mode
  0700 on the machine's disk, `pdfinfo` validates it there, and only a
  passing PDF is uploaded with `put`. The directory is removed in a
  `finally`, pass or fail. Nothing is uploaded on failure, so the key
  holds no PDF.

The markdown, HTML and metadata beside the PDF keep their order and their
failure handling (`resume/tailor.py:142-176`, `apply/letters.py:585-610`);
only the calls change.

### The artifact routes

`list_artifacts` reports `exists` from `stat`. `read_artifact` answers
locally with `FileResponse(local_path)`, as today, and hosted with the
object's bytes, the same media type (`artifacts.py:55-65`) and the same
`Content-Disposition` file name. A 404 for an absent artifact keeps its
text (`harrier_api/app.py:972-976`).

### The storage migration (Postgres only)

In one transaction, under spec 103's advisory lock:

1. **Precheck Supabase's storage objects.** If `storage.buckets`,
   `storage.objects` or `storage.foldername(text)` is missing, raise
   `migration <N> needs Supabase's storage schema (storage.buckets,
   storage.objects, storage.foldername()); this store has none`, where
   `<N>` is the migration's version. `harrier store migrate` exits 1
   through `StoreMigrationRefused`, as spec 105's amendment made it do,
   and the store stays at the version it had.
2. **The bucket.** Insert `harrier-files` with `public = false`,
   `file_size_limit = 6000000` and `allowed_mime_types` the five types
   above. If a bucket of that id exists with other settings, raise
   `migration <N> found bucket harrier-files with other settings; it does
   not change a bucket it did not create`.
3. **The policy.**

```sql
CREATE POLICY harrier_files_owner_only ON storage.objects
    AS PERMISSIVE FOR ALL TO authenticated
    USING (
        bucket_id = 'harrier-files'
        AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
    )
    WITH CHECK (
        bucket_id = 'harrier-files'
        AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
    );
```

No `ALTER TABLE` on `storage.objects`: Supabase enables row security
there itself, and since 2025-04-21 restricts other DDL in that schema
while still permitting policies.

The SQLite side of the new version is empty, declared in
`SINGLE_DIALECT_MIGRATIONS` with the reason `the Storage bucket and its
policy (spec 107); the local store has no Storage`.

### Why the policy names `authenticated`, not `harrier_tenant`

Spec 105 polices tables as `harrier_tenant` so that Supabase's Data API,
which runs as `authenticated`, reaches no harrier table. Storage cannot
follow it:

- The Storage server checks access with row-level policies on
  `storage.objects`, and Supabase's own examples name `authenticated`.
  A request reaches another role only through a token whose `role` claim
  names it. Supabase's custom role example signs that token with the
  project's JWT secret.
- Harrier signs no tokens. Spec 104 accepts only `ES256` and `RS256`
  tokens with `role` equal to `authenticated`, and never configures the
  legacy shared secret.
- A token with `role` `harrier_tenant` would also be accepted by the Data
  API, which would then run as `harrier_tenant` and write harrier's tables
  directly. That is the path spec 105's amendment closed.

So the bucket's one policy is `TO authenticated`, scoped to the bucket and
to the first path segment equal to the caller's `auth.uid()`. `anon`
matches no policy and is refused. `service_role` bypasses row security, and
harrier holds no key for it.

The policy lives in harrier's migrations, not in the Supabase dashboard,
because ADR-013 decision 5 keeps every hosted policy in the one schema
definition.

### The ADR note

ADR-013 decision 6 gains:

> **Amended (spec 107).** The storage policy matches decision 1 in shape
> (owner from the session, every command, fails closed) but not in role.
> Storage switches to the role in the caller's token, so the policy on
> `storage.objects` is `harrier_files_owner_only`, `FOR ALL TO
> authenticated`, limited to bucket `harrier-files` and to object names
> whose first segment is the caller's `auth.uid()`. A tenant can therefore
> reach their own objects with their own token and the publishable key,
> without harrier; they cannot reach another owner's. File access goes
> through one seam, `harrier.filestore`: local files locally, the bucket
> hosted, bound to the request's owner. Object names that would carry a
> person's name are hashed.

### Configuration

Hosted mode (spec 104) requires one more variable:

| Variable | Hosted value | Use |
|---|---|---|
| `HARRIER_SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` | the `apikey` header on Storage requests |

It must begin `sb_publishable_`. A legacy key, which is a JWT, is refused,
because a legacy `service_role` key has the same shape as a legacy `anon`
key. Local mode ignores it.

### Written into other specs

Spec 117 is a stub, not approved. The implementing branch adds this line,
and nothing else, to it:

- **Spec 117:** export covers every object under `<owner>/` in
  `harrier-files`.

What this spec asks of approved specs is under "Changes to approved
specs".

## Failure modes

- **Hosted, no store bound.** `files()` raises `FileStoreUnbound`. A route
  answers 500 with a generic detail, and the log names the route, never a
  key. Nothing is written to the machine's disk.
- **Owner A asks for owner B's object.** The seam cannot build B's name:
  the prefix comes from A's verified `sub`. A crafted request to Storage
  with A's token and B's prefix matches no policy and is refused.
- **A tenant writes their own prefix directly**, with their token and the
  publishable key the SPA holds. Accepted by Storage. They can replace
  their own seen state, model or artifacts. Every reader parses JSON or
  serves bytes back to that same owner; the model is JSON with no pickle
  (`scoring/model.py:1-12`). The bucket refuses files over the limit and
  types outside the list. No other owner is affected (Honest
  limitations, Open decision 3).
- **Storage unavailable.** `FileStoreUnavailable`. Seen state and mail
  state raise `DamagedStateError`: an outage never reads as "nothing seen
  yet". The description cache reads as absent, and the decision proceeds.
  An artifact route answers 503.
- **Storage refuses the owner's own token** (`FileStoreDenied`). The
  policy or the key is misconfigured, not the user's doing: 500, logged
  with the operation and the family.
- **An object over the limit.** `FileTooLarge` before any request. The
  operation fails; the earlier object stays.
- **A failed upload.** Storage stores the whole object or nothing; the
  earlier object stays. Locally, each writer keeps today's behavior.
- **A PDF that fails the gate hosted.** Nothing is uploaded; the earlier
  PDF was deleted first, so the artifact reads as absent, as locally.
- **The process dies mid-render hosted.** The temporary directory stays
  on the machine's disk until the machine is replaced (Honest
  limitations).
- **Two requests write the same read-modify-write object** (staged
  candidates, outreach targets, an append). The later upload wins and one
  change is lost. Locally the same race exists between processes.
- **An invalid key.** `InvalidFileKey` on both stores, before any file
  or request.
- **The storage migration without Supabase's storage schema.** Refused
  with the text above, exit 1, the store at the version it had. Locally
  the Postgres tests
  install the shim first.
- **The storage migration meets an existing `harrier-files` with other
  settings.** Refused; the store stays at the version it had.
- **Hosted mode without `HARRIER_SUPABASE_PUBLISHABLE_KEY`**, or with a
  value not beginning `sb_publishable_`: start refused, `error: hosted
  mode needs HARRIER_SUPABASE_PUBLISHABLE_KEY, a key beginning
  sb_publishable_`, exit 1. The value is never printed.
- **Another policy on `storage.objects` that matches the bucket.**
  Permissive policies combine with OR, so it would widen access. The
  storage catalog test fails on any policy on `storage.objects` other than
  `harrier_files_owner_only` (planned
  test_storage_objects_carries_only_the_harrier_policy). A real project's
  other policies are checked by spec 110 (its Amendment of 2026-10-11).

## Acceptance criteria

PR 1 (refactor):

- [ ] `services/api/src/harrier/filestore.py` defines `FileStore`,
      `LocalFileStore`, `files()`, `bind_files()`, `InvalidFileKey` and
      the `FileStoreError` family, and is the only module under
      `services/api/src/harrier/` that joins a key to `data_dir()`.
- [ ] No module in the first table under Problem calls `data_dir()`,
      `write_text`, `write_bytes`, `write_json_atomic`,
      `write_bytes_atomic`, `read_text`, `is_file`, `glob` or `unlink` on
      a data path; the pull request shows the grep before and after.
- [ ] For every key family, a local write through the seam produces the
      same relative path, bytes and file mode as before the refactor
      (planned test_the_local_store_writes_what_it_wrote_before), written
      and passing on the commit before the refactor.
- [ ] Every existing test passes unchanged, including
      `services/api/tests/test_ui_apply.py::test_an_artifact_is_served_with_its_own_media_type`,
      `services/api/tests/test_resume.py::test_passing_pdf_gate_updates_tracker_and_writes_artifacts`,
      `services/api/tests/test_mail_watch.py::test_a_truncated_state_file_is_refused`
      and
      `services/api/tests/test_job_events.py::test_a_damaged_description_file_does_not_block_a_decision`.
- [ ] An invalid key is refused on both stores before any file is touched
      (planned test_a_key_outside_the_grammar_is_refused).
- [ ] `just check` passes.

PR 2 (hosted):

- [ ] Hosted, `files()` with nothing bound raises `FileStoreUnbound`, and
      no route falls back to the local disk (planned
      test_an_unbound_hosted_store_refuses).
- [ ] A hosted request binds a store for its own `sub`, and a synchronous
      route sees it (planned
      test_a_request_binds_its_owner_for_sync_routes).
- [ ] With a fake Storage server, every writer in the first table run
      hosted writes nothing under `data_dir()` and uploads only under
      `<owner>/` (planned
      test_a_hosted_store_writes_nothing_under_the_data_directory).
- [ ] Object names hash every non-listed family and keep listed families
      plain, and the downloaded file name is the key's last segment
      (planned test_object_names_never_carry_a_person).
- [ ] Each Storage answer in the error table maps to its class, and no
      message contains the token, the owner or the full key (planned
      test_storage_errors_name_the_family_only).
- [ ] Hosted unavailable Storage raises `DamagedStateError` for seen state
      and mail state, and a status change still records with an empty
      description digest (planned
      test_an_outage_never_reads_as_empty_state).
- [ ] Two owners with the same model key get their own model from the
      cache (planned test_the_model_cache_never_crosses_owners).
- [ ] A hosted PDF that fails the gate uploads nothing and leaves no
      temporary directory; one that passes is uploaded once (planned
      test_a_hosted_pdf_uploads_only_after_the_gate).
- [ ] `harrier store migrate` on a shimmed database prints
      `postgres 0 -> <target_version()>`; without the storage shim it
      exits 1 with the precheck text and the store stays at the version
      before the storage migration (planned
      test_the_storage_migration_needs_supabase_storage).
- [ ] Acting as owner A with `as_role(conn, "authenticated", owner)` from
      `services/api/tests/pg_support.py`, the shim's `storage.objects` accepts an insert,
      select, update and delete under A's prefix in `harrier-files`, and
      refuses each under B's prefix, under no prefix, and in another
      bucket; `anon` is refused everything (planned
      test_the_bucket_policy_confines_an_owner_to_their_prefix).
- [ ] The only policy on `storage.objects` is `harrier_files_owner_only`,
      permissive, `ALL`, roles `{authenticated}`, with the expressions
      above; the bucket is private with the limit and types above (planned
      test_storage_objects_carries_only_the_harrier_policy).
- [ ] A migration statement creates no `storage` schema, table or
      function; only `services/api/tests/pg_support.py` does.
- [ ] Hosted mode refuses to start without a valid
      `HARRIER_SUPABASE_PUBLISHABLE_KEY`, with the text under Failure
      modes, and never prints the value (planned
      test_hosted_mode_needs_a_publishable_key).
- [ ] `just contract` shows the 503 declaration admitting the Storage
      body on the routes that use the seam, and no other contract diff.
- [ ] ADR-013 decision 6 carries the "Amended (spec 107)" note;
      `docs/architecture.md` names the seam, the bucket and the policy's
      role.
- [ ] Spec 117 carries the line under "Written into other specs"; once
      approved, the specs named under "Changes to approved specs" carry
      those lines; and nothing else in any of them changes.
- [ ] Local behavior: every test from PR 1 still passes, and no local
      command reads `HARRIER_SUPABASE_PUBLISHABLE_KEY` or imports `httpx`
      at runtime.
- [ ] CI's `check-python` job runs the Postgres tests above, not skipped.
- [ ] `just check` passes.

## Honest limitations

- **A tenant can bypass harrier for their own files.** Tables are closed
  to the Data API (spec 105); the bucket is not closed to Storage's own
  API, because harrier's request writes and a tenant's direct writes carry
  the same token. The policy confines both to the tenant's prefix. A
  tenant who edits their own seen state or model changes only their own
  results. There is no per-owner quota beyond the per-object size limit.
- **The Storage server is not in CI.** The tests prove the policy SQL on
  plain Postgres with a hand-built `storage` schema and a fake HTTP server
  for the client. These are assumptions, each for spec 110's staging
  project to confirm or refute:
  1. Storage runs a user token's request as `authenticated`, with
     `auth.uid()` equal to the token's `sub`. Supabase's examples imply it
     (policies `to authenticated`; a custom role needs a token naming it)
     but do not state it.
  2. Storage verifies tokens signed by the project's asymmetric keys.
  3. The publishable key in `apikey` with a user token in `Authorization`
     reaches Storage as that user.
  4. The migrating role may insert into `storage.buckets` and create a
     policy on `storage.objects`. The 2025 restriction notice permits
     policies; it does not address bucket rows.
  5. The shim's `storage.foldername()` matches the deployed one.
  6. An upload that asks for no caching is read back fresh right after an
     overwrite. Supabase warns that overwrites reach its CDN with a delay,
     and does not say how private, per-user reads behave.
  7. Removing an absent object succeeds, and a download of one answers
     not found distinguishably from a denial.
  8. A listing returns each object's update time and a version.
  9. The bucket's size limit and type list refuse at the server.
- **The REST paths are not pinned here.** The seam calls Storage's
  upload, download, remove and list operations; the exact paths come from
  Storage's published API at implementation and are part of spec 110's
  check.
- **Supabase's database backups do not include Storage objects.** Until
  spec 110 adds one, a deleted or overwritten object is gone.
- **Nothing hosted produces most artifacts yet.** Resumes, letters,
  answers, evaluations, contact discovery, discovery runs and the mail
  watch are runs, and spec 104 refuses hosted runs until spec 106. After
  this spec, the hosted request paths that touch files are artifact
  reads, staged candidate reads and updates, capture, and the
  description digest in a status change.
- **Scoring models are not trained hosted.** Training needs scikit-learn,
  which is a dev dependency the image does not install
  (`services/api/pyproject.toml:41-45`). A hosted owner has no model, and
  the rules score, until a later spec says otherwise.
- **Read-modify-write is not safe under concurrency**, hosted or local:
  staged candidates, outreach targets and the two appended files can lose
  a change.
- **Hosted PDFs touch the machine's disk** for the length of a render, in
  a 0700 directory removed in a `finally`. A killed process leaves it
  until the machine is replaced.
- **The size limit is Supabase's guidance, not a measurement.** No hosted
  file exists to measure. An academic run's raw dataset is the likeliest
  to exceed it; such a run would fail naming the family.
- **Hashed names hide people, not structure.** The family, the owner's id,
  the track id and the source name stay visible in object names.
- **Vendor-side behavior is outside harrier's logs.** Harrier's own
  messages never name an object; what Supabase records about a request is
  Supabase's.
- The line numbers cited here are refreshed by the change that implements
  this spec, not before (decision D9, 2026-10-11).

## Migration

None for the local product. Every file keeps its path, bytes and mode; a
fresh clone, the demo and every local command behave as before. No local
file is uploaded, and nothing moves a local data directory to a hosted
owner; that would be an import, which no spec offers.

A hosted store reaches the new version through `harrier store migrate`,
which
creates the bucket and its policy. No hosted object exists before it.

## Changes to approved specs

Each line below is a change this spec asks of an approved spec. None is
written into that spec here; each is an amendment Akin approves with this
spec.

- **Spec 104:** hosted start-up also requires
  `HARRIER_SUPABASE_PUBLISHABLE_KEY`, beginning `sb_publishable_`
  (Behavior, "Configuration"), and its authentication middleware, once a
  token verifies, binds the request's `SupabaseFileStore` (Behavior,
  "Which store a call uses").
- **Spec 106:** a hosted run reaches Storage as its owner. A Storage
  request needs a token Supabase signed or accepts, with `sub` the run's
  owner; a database login that sets claims is not enough. The request's
  token expires (one hour by default, spec 104) and a scheduled run has
  none. Spec 106 decides between forwarding and refreshing the owner's
  session, and a harrier-held signing key that mints short-lived owner
  tokens, and binds the run's `files()` at start. Also: run inputs and the
  run journal (`harrier_api/runs.py:302-331`, `:792-812`) hold an owner's
  free text on the machine's disk. Decision D3 (2026-10-11) settles that
  the Storage identity is spec 106's; spec 106 now carries it (its Scope
  item 12 and its Open decision 8, the token). The run inputs and journal
  point is not yet carried there.
- **Spec 108:** a scheduled run binds its owner's store through spec 106's
  mechanism before any domain code runs.
- **Spec 110:** the staging project confirms each assumption listed under
  Honest limitations; the bucket `harrier-files` has a backup story, since
  Supabase's database backups do not include Storage objects; hosted
  logging decides the LLM debug log and the log file. Spec 110's refined
  text of 2026-10-10 carries the hosted log rules; the rest is written
  into spec 110's Amendment (2026-10-11) (decision D4).
- **Spec 111:** deletion covers every object under `<owner>/` in
  `harrier-files`. Spec 111's refined text carries it (its Deletion,
  steps 2 and 5).

## Options weighed

- **A policy `TO harrier_tenant`, as spec 105 polices tables.** Needs
  tokens whose `role` claim names `harrier_tenant`, which harrier would
  have to sign, and which the Data API would then honor for harrier's
  tables. Not chosen (Behavior).
- **Hosted files in a Postgres table** owned by `owner_id` under spec
  105's `harrier_tenant` policy. One isolation mechanism, one write path,
  one backup, and runs would reach files through the same database
  identity spec 106 gives them, with no Storage token at all. It
  contradicts ADR-013 decision 6, puts binary objects in the database, and
  the API would serve every byte. Not chosen; Open decision 2.
- **A secret key or S3 access keys, with the owner prefix enforced in
  code.** Both bypass row-level security, so the code would be the only
  boundary, and spec 104 refuses a secret key in the API's environment.
  Not chosen.
- **A store parameter on every domain function.** Explicit, and visible
  to pyright. It touches the signature of every function between a route
  and a writer, and a store bound to an owner is an owner parameter by
  another name, which ADR-013 decision 2 rules out. Not chosen; Open
  decision 4.
- **Supabase's Python storage client** instead of `httpx`. A larger
  dependency for four operations. Not chosen; Open decision 8.
- **Every write atomic locally.** Safer on a crash, but it changes file
  modes (`mkstemp` creates 0600) and crash behavior in what is meant to
  be a refactor. Not chosen; a separate spec can make it.
- **Plain object names.** Simpler, and listing works everywhere, but a
  candidate's and a contact's name would travel in every request path.
  Not chosen; Open decision 5.
- **Resumable uploads** for large files. Not needed by any measured file.
  Not chosen; Open decision 7.

## Decisions taken (2026-10-11)

Akin took these in chat on 2026-10-11 ("go with your recommendations").
The text above says where each changed it.

- **D1, the Storage policy.** Hosted files live in Supabase Storage, with
  the one policy on `authenticated`. A tenant can reach their own prefix
  directly, within the bucket's size and type limits. This takes Open
  decisions 2 and 3 as recommended.
- **D3, a run's Storage identity is spec 106's.** Binding a run to its
  owner includes the identity the run uses for Storage. This spec
  consumes that binding and does not define it (Behavior, "Which store a
  call uses").
- **D4, spec 110 takes this spec's asks.** The spec 110 line under
  "Changes to approved specs" is written into spec 110's Amendment
  (2026-10-11): the staging checks, the bucket's backup statement, and the
  deploy check of other policies on `storage.objects`.
- **D9, line numbers.** Refreshed by this spec's implementing change
  (Honest limitations).

## Open decisions for Akin

1. **Build order: 107 after 104, 105 and 112, before 106.** Recommended.
   This spec needs a verified request (104), the owner semantics and
   migration numbering (105) and the domain on Postgres (112). It does not
   need runs, so `depends` drops 106, and spec 106 takes the run's Storage
   identity (Changes to approved specs). Alternative: keep the stub's
   `depends: [105, 106]` and bind runs here, which waits on 106's token
   decision.
2. **Storage as ADR-013 decision 6 says** (recommended), with the policy
   on `authenticated` and the run-side token left to spec 106.
   Alternative: amend decision 6 to keep hosted files in an owned Postgres
   table. Choose the alternative if spec 106 finds neither a refreshed
   owner session nor a minting key acceptable, since without one no run or
   scheduled run can write Storage as its owner.
   Taken 2026-10-11: Storage, with the policy on `authenticated` (D1); the
   run's Storage identity is spec 106's (D3), and the token is spec 106's
   Open decision 8.
3. **Accept a tenant's direct access to their own prefix** (recommended),
   with the size and type limits and no other owner reachable.
   Alternative: give `authenticated` only `SELECT`, and write with a
   server credential confined by code, which is Options weighed's
   rejected secret key.
   Taken 2026-10-11: a tenant may reach their own prefix directly, within
   the size and type limits (D1).
4. **Bind the store per context** (recommended): middleware sets it, the
   domain calls `files()`. Alternative: pass a store through every domain
   function.
5. **Hash non-listed object names** (recommended). Alternative: plain
   names, with candidate and contact names in object paths.
6. **One spec, two pull requests** (recommended): PR 1 a refactor with no
   behavior change, PR 2 the hosted store. Alternative: two specs, the
   seam first and a new spec for the hosted store depending on it.
7. **A 6,000,000 byte limit with standard uploads** (recommended).
   Alternative: resumable uploads and a larger limit, before any file has
   been measured.
8. **`httpx` in the `hosted` group** (recommended). Alternative: Supabase's
   Python storage client.

## Proof / origin

- `docs/adr/ADR-013-hosted-multi-tenant-deployment.md`, decisions 2, 3, 5
  and 6, and decision 1 as amended by spec 105.
- `specs/102-hosted-multi-tenant-deployment.md`: the stub's headline.
- `specs/104-the-hosted-api-authenticates-every-request.md`: hosted mode,
  the middleware, accepted token algorithms and roles, the refused secret
  key, and hosted runs refused until spec 106.
- `specs/105-every-personal-table-is-owned-and-policed.md` and its
  Amendment: `harrier_tenant`, per-owner track ids, and the routed finding
  about track-id paths this spec keeps.
- `specs/112-the-tracker-domain-runs-on-either-store.md`, Honest
  limitations: the description cache under `data/` is this spec's.
- `specs/040-mail-watch-and-durable-state.md`: damaged state raises,
  never reads as empty (`services/api/src/harrier/atomicio.py`).
- `specs/047-apply-page.md` and `specs/067-failed-render-leaves-no-pdf.md`: the artifact routes, and no PDF
  at a key unless the gate passed (`services/api/src/harrier/artifacts.py`,
  `services/api/src/harrier/resume/pdf.py:87-111`).
- `specs/077-learned-fit-score.md`: the model is JSON, never pickle
  (`services/api/src/harrier/scoring/model.py:1-12`).
- Every file and line under Problem, at commit `44ac0d7`.
- Supabase, Storage access control: policies on `storage.objects`, `to
  authenticated`, `storage.foldername(name)`, upsert needing select and
  update, service keys bypassing row security, no uploads without a
  policy. https://supabase.com/docs/guides/storage/security/access-control
  (read 2026-10-10)
- Supabase, Storage ownership: `owner_id` from the token's `sub`, unset
  for service key uploads.
  https://supabase.com/docs/guides/storage/security/ownership (read
  2026-10-10)
- Supabase, Storage custom roles: a role is reached by a token naming it,
  signed with the project's JWT secret.
  https://supabase.com/docs/guides/storage/schema/custom-roles (read
  2026-10-10)
- Supabase, Storage S3 authentication: access keys bypass row security; a
  session token carries a user JWT and respects it.
  https://supabase.com/docs/guides/storage/s3/authentication (read
  2026-10-10)
- Supabase, Storage buckets: private buckets subject every operation to
  policies; size and type limits are per bucket.
  https://supabase.com/docs/guides/storage/buckets/fundamentals (read
  2026-10-10)
- Supabase, Standard uploads: 6 MB guidance, upsert, and delayed CDN
  propagation after an overwrite.
  https://supabase.com/docs/guides/storage/uploads/standard-uploads (read
  2026-10-10)
- Supabase, Storage CDN: private objects are checked per user.
  https://supabase.com/docs/guides/storage/cdn/fundamentals (read
  2026-10-10)
- Supabase, Database backups: Storage objects are not included.
  https://supabase.com/docs/guides/platform/backups (read 2026-10-10)
- Supabase, JWT signing keys: importing a private key, minted tokens
  needing a `role` claim, and the legacy secret.
  https://supabase.com/docs/guides/auth/signing-keys (read 2026-10-10)
- Supabase, API keys: `sb_publishable_` keys are safe to expose and go in
  `apikey`; `sb_secret_` keys bypass row security.
  https://supabase.com/docs/guides/api/api-keys (read 2026-10-10)
- Supabase changelog 34270: since 2025-04-21, policies and triggers on
  `storage.objects` remain permitted while other DDL in the storage schema
  is restricted.
  https://supabase.com/changelog/34270-restricting-access-on-auth-storage-and-realtime-schemas-on-april-21-2025
  (read 2026-10-10)

## Out of scope

- How a run or a scheduled run reaches Storage as its owner: spec 106,
  then spec 108.
- Backups of the bucket, the staging check of every assumption above, and
  hosted logs (the LLM debug log and the log file): spec 110.
- Deleting an owner's objects: spec 111. Exporting them: spec 117.
- Gmail hosted: spec 109. The mail state goes through the seam here, and
  hosted mail runs stay refused.
- Training scoring models hosted.
- Per-owner storage quotas and billing.
- Making every local write atomic.
- Found while drafting, each for its own spec:
  - Artifact names are built from company and role, not from the job or
    its track (`resume/tailor.py:55-76`, `apply/letters.py:201-215`,
    `artifacts.py:85-99`). Locally, two tracks' jobs at the same company
    and role share one resume, letter and answers file today.
  - `offers/batch.py:58-62` appends to `evaluations/audit.jsonl` with no
    bound and no reader in the code.

## Amendment (2026-10-13): a run's Storage identity comes here from spec 106

Akin decided in chat on 2026-10-13 to move spec 106's Open decision 8
here, with the criterion that rested on it (spec 106, Amendment
(2026-10-13), decision 3). Spec 106 is built without Storage. This spec
receives the binding of a run's `files()` and the choice of the run's
Storage token, both to be decided when this spec is built. Nothing else
in this spec changes.

Where "Which store a call uses" ("Binding a run"), decision D3 and Open
decision 2 say a run's Storage identity and its token are spec 106's,
read this amendment: they are this spec's. A hosted run is started by
spec 106's run manager, so the binding is made in that start, before any
domain code runs. This amendment does not change `depends`.

### Acceptance criteria (amendment of 2026-10-13)

Received from spec 106, unchanged:

- [ ] In a hosted run, `files()` is bound to the run's owner before any
      domain code runs, writes only under that owner's prefix, and never
      returns this spec's local store (decision D3; planned
      test_a_hosted_run_binds_its_owners_storage).

### Open decision for Akin (amendment of 2026-10-13)

**The token a run uses for Storage.** Received from spec 106's Open
decision 8, to be decided when this spec is built. A Storage request
needs a token Supabase accepts, with `sub` the run's owner; a database
login that sets claims is not enough. The two ways, as spec 106 stated
them:

- forward and refresh the owner's own session, which a scheduled run
  (spec 108) does not have;
- a signing key harrier holds, imported into the project, that mints
  short-lived tokens for the run's owner, which gives the holder of that
  key the power to act as any owner in Storage.

No recommendation is made in this text. Until this is taken, the
criterion above states the guarantee only.

### Proof / origin (amendment of 2026-10-13)

- `specs/106-tenant-credentials-and-run-isolation.md`, Open decision 8,
  Behavior ("The run's Storage identity"), and its Amendment
  (2026-10-13), decision 3.
- Akin's decision in chat on 2026-10-13, relayed by the coordinator.
