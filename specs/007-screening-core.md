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

## Out of scope

run_source_import and summaries (spec 011), importers (008 to 010), Telegram
(011), the rejected-debug CSV (011 decides its fate), migration of existing
seen-state and description caches (spec 022).
