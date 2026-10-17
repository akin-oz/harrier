---
spec: 154
title: The artifact import never overwrites a hosted write that lands during the copy
status: proposed
approved: no
milestone: M10
depends: [107, 146, 152]
---

# Spec 154: The artifact import never overwrites a hosted write that lands during the copy

## Problem

Spec 146's `harrier store import-artifacts` promises that a hosted object
with other bytes is never overwritten: "The command never overwrites,
because a hosted run may have produced a newer artifact under the same
key". The code keeps that promise only when nothing writes during the run.

`_copy_one` in `services/api/src/harrier/artifactimport.py` asks
`hosted.stat(key)` first. When the object is absent it sends
`hosted.put(key, data, atomic=True)`. `SupabaseFileStore.put` in
`services/api/src/harrier/filestore.py` always uploads with
`x-upsert: true` (its `_upload`), as spec 107's operation table says
("one upload with upsert"). So a hosted write to the same key that lands
between the `stat` and the `put` is replaced by the local bytes.

Who loses: the owner. Their hosted account keeps running while the
operator imports. A resume tailored, a letter or answers generated, or a
description pasted or fetched (specs 149, 151) under the key the import is
about to write is replaced by the older local copy, and the import counts
it "copied". Nothing reports the loss. The window is one request per file,
and it repeats for every file of every family: resumes, cover letters,
answers, and the descriptions spec 152 adds.

Spec 146 recorded this as an honest limitation ("No conditional write").
CodeRabbit raised it again on PR #391 (spec 152). It was confirmed there
and deferred, because it is a gap in the seam every family shares, not in
the descriptions family.

What Storage offers. An upload without upsert is create-only. Supabase's
Standard uploads guide: uploading to a path that exists returns "400 Asset
Already Exists", and of two concurrent uploads to one path the first to
complete succeeds and the others get that error. Storage's server
(`supabase/storage`, read 2026-10-17) raises `KeyAlreadyExists` for a
non-upsert upload over a live object: `statusCode` "409", `error`
"Duplicate", message "The resource already exists". Its object routes send
every such error as HTTP 400 with the real status in the body's
`statusCode` (`src/http/error-handler.ts`; only the iceberg and vector
routes send the status itself). The seam's `answered_status` already reads
that body field, so it maps this answer to 409.

`FakeStorage` in `services/api/tests/storage_support.py` models a
non-upsert upload over an existing object, but as HTTP 409. Nothing sends
one today, so the shape hosted Storage actually sends has never been
exercised.

## Scope

1. The seam gains a create-only upload on `SupabaseFileStore`.
2. `import-artifacts` uploads with it, and classifies the "already exists"
   answer the way it already classifies an object `stat` found.
3. `FakeStorage` answers a create-only upload over an existing object in
   both shapes Storage sends.

Every other writer keeps `put`, which keeps upsert.

## Behavior

### The seam

`SupabaseFileStore.create(key, data) -> bool` uploads `data` to `key` only
when no object is there.

- It sends the request `put` sends: the same key check, object name,
  content type from the extension, `Cache-Control`, the 6,000,000 byte
  limit checked before any request, with one difference. The header is
  `x-upsert: false`.
- Storage answers 200: returns `True`. The object holds `data`.
- Storage answers that the object exists: returns `False`. The object is
  unchanged. "Exists" is `answered_status(response) == 409`, so HTTP 409
  and HTTP 400 carrying `statusCode` "409" both count.
- Any other answer raises what `put` raises for it, through the same
  `storage_error`: `FileStoreDenied` for 401 and 403, `FileTooLarge` for
  413, `FileStoreUnavailable` for 5xx and transport errors, `FileStoreError`
  otherwise. The message names the operation (`create`) and the family,
  never a key or an object name.
- An extension outside the bucket's five raises `FileStoreError` before a
  request, as `put` does.

`create` is not part of the `FileStore` protocol and `LocalFileStore` does
not gain it (Open decision 1). `put`, `append`, and every caller of them
are unchanged.

### The import

`_copy_one` keeps its order. Only the absent branch changes.

1. `stat(key)`. Present: `read(key)` and compare, as today.
2. Absent: `create(key, data)`, under the same 429 waits as today
   (`_patiently`, `RETRY_WAITS`).
   - `True`: `stat(key)` must report the local size, as today. Counted
     "copied".
   - `False`: something wrote the key since the `stat`. `read(key)` and
     compare, exactly as step 1 does for an object found present:
     - same bytes: counted "already there";
     - other bytes, in `resumes`, `cover-letters` or `answers`: left as it
       is, counted "differs from the hosted object", and the verdict is
       `incomplete`;
     - other bytes, in `descriptions`: left as it is, counted `kept, newer
       on hosted`, and the verdict is not affected (spec 152);
     - the read finds no object: the run stops (Failure modes).
   - The attached and unattached counts treat the file as step 1 would.

No request the import sends carries `x-upsert: true`. The output lines,
their order and their wording are spec 146's and spec 152's, unchanged. A
lost race shows only as a count on a line that exists already.

### The fake

`FakeStorage` answers an upload without `x-upsert: true` to a name it holds
with HTTP 400 and the body `{"statusCode": "409", "error": "Duplicate",
"message": "The resource already exists"}`, the shape hosted Storage sends.
A flag makes it answer HTTP 409 with the same body instead, as the
`legacy_not_found` flag does for absent objects. The object is unchanged
either way. Its docstring names the source and the date read.

## Failure modes

| Case | Result |
|---|---|
| A hosted write with other bytes lands between the `stat` and the upload, artifact family | Not overwritten; counted "differs from the hosted object"; verdict `incomplete`, exit 1 |
| The same, `descriptions` | Not overwritten; counted `kept, newer on hosted`; the verdict is not affected |
| A hosted write with the same bytes lands in that window | Counted "already there" |
| A hosted write lands after the create succeeds | The hosted writer's `put` replaces the import's bytes. That is the newer write winning, as spec 146 intends |
| A hosted write of another size lands between the create and the size check | The run stops as spec 146's "size read back differs" row says; the object is left as Storage holds it; a rerun counts it as differing |
| The object exists at the create and is gone at the read that follows | The run stops, exit 1, with one line on stderr: `upload <family>: the hosted object changed during the run; run the command again`. Nothing is uploaded for that file. A rerun finds it absent and creates it (Open decision 2) |
| Storage answers 429 to the create | Waits and asks again, as for every call today |
| Storage answers anything else that is not success or "exists" | The run stops as spec 146's Storage rows say, naming the step `upload` and the family |
| The create's answer is lost (transport error) after Storage stored the object | The run stops as unavailable; a rerun finds the object with the same bytes and counts it "already there". Unchanged from today |
| Two copies of the command at once | One creates the object. The other's create answers "exists", its read finds the same bytes, and it counts the file "already there". Replaces spec 146's row, which said the second `put` changes nothing |
| A Storage version that answers the conflict as HTTP 409 | Same result as the 400 form |
| A Storage version that ignores `x-upsert: false` and replaces the object | Not detectable by the command. Honest limitations |

## Acceptance criteria

All run against `FakeStorage` and the CI Postgres service, with synthetic
names and bytes only, like spec 146's.

- [ ] `create` on an absent key stores the bytes with the content type
      `put` would send, returns `True`, and its request carries
      `x-upsert: false` (planned test_create_stores_a_new_object_without_upsert).
- [ ] `create` on a key the fake holds returns `False` and leaves the
      object's bytes, in both the HTTP 400 and the HTTP 409 shape (planned
      test_create_leaves_an_existing_object_in_both_answer_shapes).
- [ ] `create` raises what `put` raises for 403, 413, 503, another 400, an
      oversized body and an extension outside the five, naming `create` and
      the family and never the key (planned test_create_fails_as_put_fails).
- [ ] With the fake's upload hook placing other bytes under the key just
      before the import's upload, an artifact keeps the hosted bytes, is
      counted "differs from the hosted object", and the verdict is
      `incomplete` (planned test_a_hosted_write_during_the_copy_is_never_overwritten).
- [ ] The same hook placing the local bytes counts the file "already
      there" and the verdict is `imported` (planned
      test_a_hosted_write_of_the_same_bytes_during_the_copy_is_already_there).
- [ ] The same hook on a description keeps the hosted bytes and counts it
      `kept, newer on hosted`, and the verdict is `imported` (planned
      test_a_hosted_description_written_during_the_copy_is_kept).
- [ ] An object that answers "exists" to the create and is absent at the
      read stops the run with exit 1 and the line in Failure modes, naming
      no key (planned test_an_object_gone_after_a_conflict_stops_the_run).
- [ ] No request a full import sends carries `x-upsert: true` (planned
      test_import_artifacts_never_upserts).
- [ ] A 429 to the create waits and asks again, and the file is then copied
      (an added case in
      `services/api/tests/test_import_artifacts.py::test_import_artifacts_waits_out_a_rate_limit`).
- [ ] `put` still sends `x-upsert: true` and replaces an existing object
      (planned test_put_still_replaces_an_existing_object).
- [ ] Spec 146's existing import tests pass unchanged, among them
      `services/api/tests/test_import_artifacts.py::test_import_artifacts_never_overwrites_a_different_object`
      and `services/api/tests/test_import_artifacts.py::test_import_artifacts_is_idempotent`.

## Proof / origin

- The finding: CodeRabbit on PR #391 (spec 152), confirmed and deferred
  there as a seam level gap.
- The rule it breaks: spec 146, "The copy" step 3 and Open decision 7.
- The limitation it closes: spec 146, Honest limitations, "No conditional
  write".
- The code: `_copy_one` in `services/api/src/harrier/artifactimport.py`;
  `SupabaseFileStore.put` and `_upload` in
  `services/api/src/harrier/filestore.py`.
- Storage's answer: Supabase docs, Storage, "Standard Uploads", sections on
  overwriting and concurrency; `supabase/storage` source,
  `src/internal/errors/codes.ts` (`KeyAlreadyExists`),
  `src/storage/uploader.ts` (the non-upsert check),
  `src/internal/errors/storage-error.ts` and `src/http/error-handler.ts`
  (HTTP 400 with the status in the body), read 2026-10-17.
- The fake as it stands: `FakeStorage.handler` in
  `services/api/tests/storage_support.py`.

### Who else calls `put`

Each of these writes a key it owns and means to replace what is there.
They keep upsert, and none changes:

- the resume PDF render, `services/api/src/harrier/resume/pdf.py`;
- `save_text` and `save_json` in `services/api/src/harrier/filestore.py`,
  through which letters, answers and other state are written;
- the description cache, `save_description_cache` in
  `services/api/src/harrier/screening/descriptions.py`, which a paste
  replaces (spec 149);
- the LinkedIn verdict cache, `services/api/src/harrier/screening/linkedin.py`;
- the scoring export and the trained and active models,
  `services/api/src/harrier/scoring/export.py` and `train.py`;
- the mail watch event log, `services/api/src/harrier/mail/watch.py`;
- `SupabaseFileStore.append`, which reads, adds and uploads the whole.

The import is the only writer that must not replace.

## Open decisions for Akin

1. **A separate method or an option on `put`.** Recommended: `create` on
   `SupabaseFileStore` only. The protocol's `put` says "Replace the file";
   an option that turns that off on one store and means nothing on
   `LocalFileStore` would be a difference no caller can see. The import
   already holds a `SupabaseFileStore`, not the protocol. Alternative:
   `put(..., replace: bool = True)` on both stores, with `LocalFileStore`
   using an exclusive create. That widens the change to every store for
   one caller.
2. **An object gone after "exists".** Recommended: stop the run, as the
   size check does, so a rerun decides with a fresh `stat`. Alternative:
   count it as differing and go on. That reports a file as differing from
   an object that is not there, and still ends `incomplete`.

## Out of scope

- Changing `put`, `append` or any writer other than the import.
- A create-only write on `LocalFileStore`.
- An overwrite or refresh flag for the import (spec 146, Open decision 7).
- Conditional writes on a version or an ETag. Storage's standard upload
  offers create-only, not compare-and-swap, and create-only closes this
  gap.
- Resumable (TUS) uploads.
- The race between two hosted writers that both upsert (spec 107, Honest
  limitations, for `append`).
- Changing the output lines or the verdict rules of specs 146 and 152.

## Honest limitations

- **The guarantee is Storage's.** That a non-upsert upload never replaces a
  live object, even against a concurrent upsert, rests on Supabase's docs
  and on reading its server code. The fake shows the import's handling of
  the answer, not Storage's atomicity. Staging can show the answer's shape
  with one upload; no test here proves the concurrency.
- **The answer's shape is read from source, not observed.** The deployed
  Storage version may differ from the source read. Both shapes are
  accepted, and any other answer stops the run rather than being taken as
  success.
- **A conflict costs a download.** A lost race reads the hosted object to
  compare, as a present object already does. Races are rare, so this is not
  measured.
- **The size check after a create can still see a later hosted write.** It
  then stops the run with spec 146's size message, which names a mismatch
  rather than a concurrent write. Nothing is overwritten, and a rerun
  counts it correctly.

## Changes to approved specs

None is written into those specs here. Each is an amendment Akin approves
with this spec, written into the spec in the implementation change.

- **Spec 107, "The seam", operation table and "Uploads":** `SupabaseFileStore`
  gains `create`, a create-only upload outside the protocol. Uploads set
  upsert except `create`'s, which sets `x-upsert: false`. Its fake's
  description gains the conflict answer.
- **Spec 146, "The copy" step 3:** the absent branch uploads with `create`;
  an "exists" answer is read and compared as a present object is. Its
  "Two copies of the command at once" failure row is replaced by this
  spec's. Its honest limitation "No conditional write" is removed.

## Migration

None. The command's inputs and output are unchanged. An operator who runs
it again after this lands sees the same counts for objects already copied.
