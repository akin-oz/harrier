---
spec: 152
title: A local store's job descriptions import into one hosted account
status: shipped
approved: yes
milestone: M10
depends: [107, 141, 146, 149]
---

# Spec 152: A local store's job descriptions import into one hosted account

## Problem

Spec 141 imported the local store's rows into one hosted account, and spec
146's `harrier store import-artifacts` copied their resumes, cover letters
and answers into the owner's Storage. Neither copied the job descriptions.
Spec 146 put them out of scope ("Out of scope": "job descriptions"), and
its `FAMILIES` in `services/api/src/harrier/artifactimport.py` lists the
three artifact directories only.

The local store holds a description for most imported rows, in
`data/descriptions/<key>.json`, written by discovery and by capture
(`save_description_cache` in
`services/api/src/harrier/screening/descriptions.py`). The hosted account
holds none of them. Since spec 149, tailor, cover letter and answers refuse
a job with no stored description, so every imported row refuses until its
description is pasted by hand, although the text is on the operator's own
disk.

Spec 151 fetches a missing description from the posting page. That does not
cover these rows well: many imported postings have closed since they were
found, and a closed posting cannot be fetched. The local copy is the only
one left.

## Scope

`import-artifacts` copies a fourth family, `descriptions`, limited to the
entries of jobs the hosted account holds.

## Behavior

Everything not stated here is spec 146's, unchanged: the input variables,
the checks before writing, the key grammar, the size limit, `stat` before
`put`, the size check after it, the never-overwrite rule, interruption and
re-running.

### What it copies

- After `answers`, the command copies the `descriptions` directory of the
  data directory: each regular file directly in it, in name order, to the
  key `descriptions/<file name>`.
- **Only the entries of hosted jobs.** The command builds the set of
  description keys the app would read for the account's own rows: for
  every job the owner sees with a URL, the key `save_description_cache`
  writes for that URL. The key function moves out of its private name
  (`_description_cache_key`) so the import calls it rather than restating
  it. A local entry whose key is not in the set is skipped without a
  request and counted as `no hosted job`. Discovery caches a description
  for every posting it fetches, including those screening rejects, so most
  local entries belong to postings that never reached the tracker.
- A hosted entry that already exists with different bytes is left as it
  is, as spec 146 leaves every differing object. For descriptions this is
  the expected case, not a fault: the hosted one was pasted or fetched
  later (specs 149, 151). It is counted as `kept, newer on hosted`, and it
  does not make the run incomplete.

### Checks before writing

Spec 146's check 5 ("at least one of the three directories exists")
counts four directories. Check 6 ("the files fit the account") is
unchanged and reads the three artifact families only: descriptions are
matched by URL, so an account that holds the rows but none of the
artifacts is not refused for its descriptions.

### Output

One family line and one reason line join spec 146's output, in this place:

```
answers: <number> copied, <number> already there
descriptions: <number> copied, <number> already there, <number> kept, newer on hosted
not copied, no hosted job: <number>
not copied, over the size limit: <number>
```

`no hosted job` and `kept, newer on hosted` do not make the verdict
`incomplete`. A description over the size limit, of another name, or whose
size check fails does, as for any family. Nothing printed names a URL, a
company, a key or the owner. The `attached` and `unattached` lines keep
counting the three artifact families only.

## Failure modes

| Case | Result |
|---|---|
| No `descriptions` directory locally | The family line reads 0 copied; the run goes on (spec 146 refuses only when all four directories are missing) |
| A local entry for a job the account does not hold | Skipped without a request, counted as `no hosted job` |
| A hosted job with no local entry | Nothing to copy; spec 151's fetch or a paste covers it |
| The hosted entry already exists, same bytes | Counted as already there |
| The hosted entry exists, different bytes | Left; counted as `kept, newer on hosted`; not incomplete |
| A local entry that is not valid JSON or not UTF-8 | Copied as bytes, unchanged, as spec 146 copies every file. The hosted reader reads it as missing, as the local reader does (spec 079 amendment) |
| A job whose URL differs between local and hosted rows | Not possible after spec 141, which copies URLs unchanged. A row edited on hosted since then matches no local entry, and its entry is counted as `no hosted job` |
| A run stopped part way | As spec 146: each object whole or absent; running again continues |

## Acceptance criteria

- [x] A local description whose key matches a hosted job's URL is copied to
      `descriptions/<file name>`, and the hosted read route serves it
      (`services/api/tests/test_import_artifacts.py::test_a_description_of_a_hosted_job_is_imported`).
- [x] A local description for a URL no hosted job holds is skipped without
      a request and counted as `no hosted job` (`services/api/tests/test_import_artifacts.py::test_a_description_of_no_hosted_job_is_skipped`).
- [x] A hosted description with different bytes is not overwritten, is
      counted as `kept, newer on hosted`, and leaves the verdict `imported`
      (`services/api/tests/test_import_artifacts.py::test_a_newer_hosted_description_is_kept`).
- [x] The output carries the two new lines, in the place above, and names
      no URL, key or company (an added case in spec 146's output test).
- [x] An account with rows and descriptions but no artifacts is not
      refused by check 6 (`services/api/tests/test_import_artifacts.py::test_descriptions_alone_pass_the_fit_check`).
- [x] The import uses the description cache's own key function (`services/api/tests/test_import_artifacts.py::test_the_import_uses_the_cache_key_function`).

## Amendments found while implementing (2026-10-17)

- **A public name beside the private one.** The key function gains a
  public name, `description_cache_key`, that calls `_description_cache_key`
  (`services/api/src/harrier/screening/descriptions.py`), rather than the
  private one being renamed. Every existing caller keeps working, and a
  change to the rule in one place reaches the import.
- **The command now reads four directories.** Spec 146's test that the
  command reads nothing outside its directories lists `descriptions` as
  the fourth
  (`services/api/tests/test_import_artifacts.py::test_import_artifacts_leaves_other_directories_alone`).

## Proof / origin

- The gap: spec 146, "Out of scope", and `FAMILIES` in
  `services/api/src/harrier/artifactimport.py`.
- The key: `save_description_cache` in
  `services/api/src/harrier/screening/descriptions.py`.
- Why it matters now: spec 149's refusal.

## Out of scope

- Fetching a description from the posting: spec 151.
- Copying descriptions of postings the account does not hold.
- Overwriting a hosted description.
- The other caches spec 146 left out (verdicts, seen state, reports).
- Changing the key function or the description format.

## Honest limitations

- A local entry is the text as discovery or capture found it, which can be
  cut short: the Greenhouse, Lever, Ashby and RemoteOK sources keep the
  first 4,000 characters (spec 149, "Out of scope").
- A row whose URL was edited on the hosted account after the import gets
  nothing from this import.

## Changes to approved specs

- **Spec 146, "Out of scope".** Job descriptions move in scope as a fourth
  family, limited to hosted jobs, with the two counters above. Its check 5
  counts four directories. The amendment is written into spec 146 in the
  implementation change.

## Migration

Run `harrier store import-artifacts --owner <user id>` once more, with the
same variables as before. The artifacts it copied are skipped as already
there, and the descriptions are copied.
