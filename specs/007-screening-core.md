---
spec: 007
title: Screening core: shared shape, gates, scoring, dedupe
status: shipped
approved: yes
milestone: M2
depends: [004]
---

# Spec 007: Screening core: shared shape, gates, scoring, dedupe

Refined from the stub before implementation; scope below is the real scope.

## Problem

The shared screening path is the heart of discovery. Every importer feeds it;
no source gets its own filtering or scoring. It must port with behavior
pinned before any importer lands (specs 008 to 011 depend on it).

## Scope

Package `harrier.screening`, a faithful port of the old repo's
`scripts/job_sources.py` lines 1 to 915 (the per-source runner glue,
run_source_import, belongs to spec 011):

- `normalized.py`: the shared job shape (make_normalized_job as a TypedDict
  producer, job_key identity via stable_key) and in-batch dedupe by
  external_id then url.
- `rules.py`: the policy constants with their load-bearing comments
  (EXCLUDED_TITLE_HINTS; REMOTE_NEGATIVE_HINTS with the documented "office"
  and "flex" false-positive exclusions; REGION_NEGATIVE_HINTS checked against
  title+location only; EU-permit phrases as positive weights, never filters),
  title_allowed, title-variant matching, remote_region_allowed with the
  linkedin_search bypass, scoring_config overrides, and score_job with the
  non-stacking domain bonus and the 120 cap (the cap was removed by spec 033;
  see the amendment below).
- `archetypes.py`: detect_archetype, the single implementation (the old
  repo's two other copies die with their hosts in specs 013 and 015).
- `http.py`: request_text with retry/backoff, request_json, strip_html.
- `descriptions.py`: URL-keyed description cache under the data directory
  and enrich_job_description_for_scoring (cache first, then ATS-host fetch,
  120-char threshold).
- `seen.py`: per-source seen-state JSON under the data directory, capped at
  the last 10,000 keys.
- `pipeline.py`: screen_jobs with the exact gate order: seen-state, hold
  list, title rules, remote/EMEA policy, tracker dedupe (url, company+title,
  external_key), enrichment, scoring with the hard cutoff at 55 (removed by
  spec 033; see the amendment below). Accepted
  jobs produce tracker-ready field dicts (the notes key=value string is
  built exactly as before; harrier.tracker.add_job promotes the keys to
  columns) and their descriptions are cached.
- `config.py`: candidate config from the profile store (kind=candidate,
  imported by spec 004) with fallback to the public
  `config/candidate.example.json`; hold list from `config/companies-hold.csv`.

Deliberate changes from the old code, stated:

- Persistence is the caller's job: screen_jobs returns rows; nothing in this
  package writes the tracker (single write path, ADR-003). The old CSV
  append and repair paths are not ported.
- State lives under HARRIER_DATA_DIR (descriptions/, discovery/), not repo
  paths. Existing caches migrate at cutover (spec 022).
- File logging is replaced by the logging module; the old log() side effect
  in screen_jobs (description caching) is kept, the log file is not.
- The real candidate config is personal and lives in the database (ADR-008);
  the committed example carries structure and default weights only.

## Acceptance criteria

- [ ] All ScreeningTests and RequestTests behavior pins from the old repo's
      tests/test_job_sources.py pass against the port
- [ ] The documented false-positive cases stay accepted: "Remote (Home
      Office)" location passes, "flex remote" passes, US offices mentioned
      only in the description do not reject an EMEA-remote role
- [ ] EU-permit phrases raise the score and appear in no rejection path
- [ ] The import-linter contract restricting sources to
      harrier.screening.normalized lands with the sources package itself
      (spec 008); until then there is nothing for it to bind to
- [x] A description cache entry is replaced whole or not at all. A save
      cut off partway, or refused by the encoder, leaves the previous entry
      byte for byte, readable, with no temporary file beside it, and a
      completed save stores the same bytes as before (the 2026-10-06
      amendment below;
      `tests/test_screening.py::test_a_description_entry_is_replaced_whole_or_not_at_all`)
- [ ] All gates green on PR

## Proof / origin

Old repo: scripts/job_sources.py (constants, screen_jobs, score_job,
remote_region_allowed); tests/test_job_sources.py; CLAUDE.md "Candidate EU
status".

## What later specs changed

Recorded here because this was the one shipped spec carrying no amendment
note, so a reader arriving at it had no way to know four of its statements
had been superseded (spec 045).

- **The cutoff is gone.** Spec 033 removed `SCORE_CUTOFF`. Anything reaching
  the scorer has already matched an include keyword and passed a remote gate
  over the same text the remote bonus rewards, so on the ATS path the floor
  was 59 against a cutoff of 55 and it could not reject. A LinkedIn result
  returns early from the region gate and never earns that bonus, so its floor
  was 51: every posting the cutoff ever rejected was a LinkedIn one, rejected
  for the mechanism that makes it valid. The gates filter; the score ranks.
- **The saturation cap is gone.** Also spec 033. A strong realistic posting
  reached the cap exactly, so it tied postings that differ in quality where
  ranking matters most.
- **Matching is token-aware.** Spec 032 replaced containment matching in every
  keyword list, and made EU-permit phrasing a scoring signal rather than a
  filter, because the candidate can contract through an EU legal entity.
- **Seen-state eviction is age-based.** Spec 031 replaced the lexicographic
  rule, which evicted the same entries forever while keeping genuinely stale
  ones. The 10,000-key cap itself carries over.

## Amendment (2026-10-06): a cache entry is replaced whole or not at all

`save_description_cache` in `services/api/src/harrier/screening/descriptions.py`
writes an entry with `Path.write_text`, which truncates the file and then
writes it. A crash, a kill or a full disk between the two leaves part of an
entry, possibly cut mid-character so it is not UTF-8. A description that no
encoding can write empties the entry with no crash at all: a lone surrogate,
which valid JSON can carry (spec 079), makes `write_text` raise after it has
truncated the file. Since spec 079's amendments the reader treats such a file
as missing, so it no longer blocks a status change. The entry is still lost:

- The description is gone. Fetching it again is the cost the cache exists to
  avoid (spec 009).
- A decision on that job records an empty `description_sha256` (spec 079), so
  it pins no text.
- Spec 077's export leaves the job out while its entry reads as missing
  (`DESCRIPTION_MISSING` in `harrier.scoring.labels`).

How an entry is written was never specified: not here, not in spec 009, which
added `cache_job_descriptions`, and not in spec 040, which made two other
state files atomic. Spec 079 covered only the read. A data-integrity review of
the merged 077 to 081 range found it and recorded it as out of scope there.
Both cases were reproduced before this amendment was written: a save stopped
halfway left half of the new entry on disk, a save of a lone surrogate left the
entry empty, and both times the previous description read as missing.

### Behavior

- `save_description_cache(url, description)` writes through
  `harrier.atomicio.write_bytes_atomic`: a temporary file in the cache
  directory, flushed and fsynced, then renamed over the entry. The scoring
  export and the model files already write this way (spec 077).
- An entry is replaced whole or not at all. A reader sees the previous entry or
  the new one, never part of either.
- A save that fails still raises, whether the write is cut off partway or the
  encoder refuses the description before anything is written. The previous
  entry is left byte for byte and no temporary file remains. With no previous
  entry, none is left: the URL stays uncached instead of holding an empty or
  partial file.
- The format does not change. The path is still
  `descriptions/<first 24 hex digits of the URL's SHA-256>.json` under the
  data directory. The bytes are still
  `json.dumps({"url": url, "description": description}, ensure_ascii=False)`
  encoded as UTF-8, so entries written before this change read as before.
  `write_json_atomic` is not used: it indents, which changes the bytes.
- An empty URL or description still writes nothing.
- One side effect: a new entry is created with mode 0600, where `write_text`
  gave 0644 under a 022 umask. `tempfile.mkstemp` creates every file
  `harrier.atomicio` writes that way. The container runs as the host user's
  uid (spec 051), so no reader loses access.

### Failure modes

- A kill between creating the temporary file and the rename leaves the
  previous entry and a stray `.<entry>.json.<random>.tmp` beside it. The reader
  opens only the entry's own path, so the stray file is never read. Nothing
  removes it, as with spec 040's state files.
- An entry damaged before this change stays damaged. It reads as missing until
  something caches that URL again, which is why spec 079's tolerance of a
  damaged file stays.
- Must not introduce: a change to the path, the bytes or the read, or a failed
  save that no longer raises.

### Proof

`test_a_description_entry_is_replaced_whole_or_not_at_all` in
`services/api/tests/test_screening.py`. It saves an entry, then cuts the next
save off halfway: the file write stops after half its bytes and raises, as a
full disk does. It asserts that the save raises, that the entry holds its
previous bytes and reads back its previous description, and that no temporary
file is left. A save of a description holding a lone surrogate must raise and
leave the entry the same way. A completed save then replaces the entry with
exactly the bytes documented above. The cut-off sits below both the old and
the new writer, because both open their file through `io.open`, which the test
wraps. The same test fails on the in-place write, where it leaves half of the
new entry. It fails the same way on an in-place write that encodes first
(`Path.write_bytes`), which the surrogate case alone would let through.

### Honest limitations

- A real kill and a power loss are not reproduced by any test. They rest on the
  rename and the fsync before it, as spec 040's state files do.
- The rename is atomic within one filesystem. The temporary file is created in
  the cache directory itself, so the two are always on the same one.

### Not changed here

Other files under the data directory are written in place the same way. They
belong to other specs and are left for their own changes:

- The outreach target store (spec 017, `save_target_store` in
  `harrier.outreach.messages`). A cut-off write leaves a file that reads as an
  empty list, and the next `save_target` replaces it with the new target alone.
  Only the tests call `save_target` today.
- The LinkedIn page verdict cache (spec 055, `_save_cached_verdict` in
  `harrier.screening.linkedin`). A damaged verdict reads as unknown, and the
  page is fetched again.
- The staged contact candidates (spec 016, `write_candidates_artifact` in
  `harrier.outreach.discovery`), the mail events rotation (spec 040,
  `_rotate_events` in `harrier.mail.watch`) and the Gmail token (spec 018).

What a caller does when a save raises does not change either. A description
that no encoding can write still raises `UnicodeEncodeError`. The discovery
run and the screening pipeline let it propagate, and capture catches only
`OSError`. Whether such a description should be cleaned or skipped is a
separate change.

## Out of scope

run_source_import and summaries (spec 011), importers (008 to 010), Telegram
(011), the rejected-debug CSV (011 decides its fate), migration of existing
seen-state and description caches (spec 022).
