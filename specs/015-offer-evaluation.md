---
spec: 015
title: Offer evaluation and batch prospect evaluation
status: shipped
approved: yes
milestone: M3
depends: [012, 004]
---

# Spec 015: Offer evaluation and batch prospect evaluation

## Problem

The six-block evaluation with its machine verdict contract, and the
batch driver with auditable auto-reject. The old scripts carry the M3
persona blocker a third time: the STAR seed stories and the prompt's
candidate header (name, stack, targets, compensation, the skip
threshold) are hardcoded personal content, and the batch driver shells
out to other scripts through subprocess.

## Scope

- Package harrier.offers:
  - evaluate: payload (JD, truth sources, achievements, seed stories,
    archetype taxonomy), prompt assembled from data (name, identity, and
    experience label from the resume bundle; stack from the bundle's
    skills; targets and compensation from the candidate document; the
    skip guidance compensation floor derived as salary_min minus 10000),
    the LLM call through harrier.llm, fenced-JSON response parsing, and
    verdict-contract validation (verdict in strong_apply, apply,
    borderline, skip; confidence coerced to float with invalid values
    treated as 0.0 so they can never clear an auto-reject threshold)
  - report: the six-block markdown report (F verdict badge first, then
    A role classification, B CV match with gap markers, C fit
    assessment, D application strategy, E STAR+R stories), written to
    data/reports/{slug}.md
  - stories: seed stories loaded from the profile store (kind
    story_seeds, json); captured stories deduplicated by story_id into a
    bounded story bank document (kind story_bank, json, newest wins,
    capped at 200 entries)
  - batch: in-process driver over tracker prospects with report-exists
    gating (--refresh to re-evaluate), --limit, --threshold (default
    0.8), --include-borderline, and auto-reject only with the explicit
    --apply flag; every committed rejection goes through set_status with
    an ai-evaluation reason and appends an audit entry to
    data/evaluations/audit.jsonl (timestamp, job id, url, verdict,
    confidence, threshold, reason)
- CLI: harrier evaluate --job-id N [--jd-text | --jd-file] and
  harrier evaluate-prospects [--apply] [--threshold F] [--limit N]
  [--refresh] [--include-borderline]

## Stated changes from the old code

- Seed stories move from code constants into the story_seeds profile
  document; the repo commits a synthetic example
  (config/story-seeds.example.json) and the real seeds are a local data
  operation into the operator's database.
- The prompt header is assembled from the resume bundle and candidate
  document instead of hardcoded personal lines.
- Story capture goes to a deduplicated, bounded json document in the
  profile store instead of an append-only markdown file that grew
  without bound and could repeat stories.
- The batch driver calls the evaluation in process against the database
  instead of shelling out to evaluate_offer.py and jobs.py through
  subprocess.
- Reports and the audit log land under the data directory
  (never-in-git), not reports/.

## Acceptance criteria

- [ ] A skip verdict at or above the threshold rejects only with
      --apply set and writes an audit entry; without the flag the row
      is untouched and no audit entry is written
- [ ] An existing report gates re-evaluation unless --refresh is given
- [ ] An invalid confidence value can never clear the auto-reject
      threshold
- [ ] Story capture deduplicates by story_id and respects the bound
- [ ] The assembled prompt contains no hardcoded personal content
      (proven against the synthetic fixtures)
- [x] All gates green on PR (PR #12)

## Proof / origin

Old repo scripts/evaluate_offer.py and scripts/evaluate_prospects.py
(no old tests existed; the pins here are new). Proving file:
services/api/tests/test_offers.py.

## Out of scope

Contacts and outreach (specs 016 and 017), GUI surfaces, and moving the
verdict thresholds into DB-backed config (spec 023).
