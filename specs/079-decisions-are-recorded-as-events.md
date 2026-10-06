---
spec: 079
title: Every decision on a job is recorded, with who made it and why
status: proposed
approved: no
milestone: M8
depends: [031, 033, 036, 072]
---

# Spec 079: Every decision on a job is recorded, with who made it and why

## Problem

The tracker stores the current state of a job and forgets how it got there.
`set_status` in `services/api/src/harrier/tracker/store.py` overwrites
`status`, stamps `updated_at`, and keeps no history. That was enough for a
queue. It is not enough for anything that learns from the candidate's
behavior (spec 077), and it loses facts that cannot be recovered later:

- **Who decided is not recorded.** `rejected` holds the candidate's own skips
  ("missing stack", "hybrid"), the company's verdicts ("rejected by
  company", "ghosted"), and system actions ("vacancy is closed", the
  AI-evaluation auto-reject in `offers/batch.py`). They share one status and
  one free-text column. A company rejecting an application the candidate
  chose to send is the opposite of the candidate rejecting a posting, yet a
  reader of the row sees the same word. Spec 077 has to guess the difference
  from `applied_date` and string matching.
- **When it was decided is not recorded.** `updated_at` moves on any field
  write, including rescoring, so the decision time is gone. A time-ordered
  split has to use `added_at`, which is when the posting arrived, not when
  the candidate judged it.
- **What the candidate saw is not recorded.** The score and scoring version
  the row carried when it was decided are overwritten by the next rescore.
  Without them, the selection bias spec 077 describes (the queue was ranked
  by the old score) can be named but never measured.
- **The reason is free text.** The same cause appears under several
  spellings, with typos and prefixes, so any count of reasons is a regex
  over history.
- **The text that was judged is not pinned.** The description cache is keyed
  by URL and can be rewritten; nothing ties a decision to the description it
  was made on.

## Scope

- An append-only `job_events` table in the tracker schema, added by migration
  5 in `services/api/src/harrier/tracker/schema.py`. This is a guarded path
  (tracker schema and migrations) and needs approval under this spec.
- `services/api/src/harrier/tracker/reasons.py` (new): the reason code table,
  each code bound to exactly one actor, and the mapping from free text to a
  code.
- `set_status` and `add_job` in `tracker/store.py`: each appends its event in
  the same transaction as the row write. They remain the only writers.
- The three `set_status` callers (`tracker/actions.py`, `offers/batch.py`,
  `resume/tailor.py`) pass the actor and code.
- CLI (`services/api/src/harrier_cli/main.py`): `reject` gains `--code`; a new
  verb `company-outcome` records a company's verdict; a new `events`
  subcommand group with `backfill` and `show`.
- Tests under `services/api/tests/`.

The API contract and `apps/web` do not change here. A web rejection keeps
sending free text and its code is inferred (below). Entering a code from the
browser is spec 080, which carries the contract change. The status
lifecycle does not change: no status is added, renamed or removed.

## Behavior

### Two kinds of event, three actors

| Kind | Actor | Meaning | Written by |
|---|---|---|---|
| `created` | `system` or `candidate` | the row entered the tracker (discovery, or manual add/capture) | `add_job` |
| `decision` | `candidate` | the candidate moved the job: shortlisted, tailored, applied, rejected it, reopened it | `set_status` from a candidate verb |
| `decision` | `system` | an automated move: AI-evaluation auto-reject, vacancy closed, duplicate | `set_status` from `offers/batch.py` and system reasons |
| `outcome` | `company` | the employer's response to an application: rejected, ghosted, no response, assessment failed, interview invited | `set_status` from `company-outcome` |

**A company's verdict is never a candidate decision.** This is the rule this
spec exists to enforce, and it holds at entry, in storage and on read:

- **At entry.** `harrier reject` records a `candidate` decision and accepts
  only candidate or system codes; given a company code it refuses with exit 2
  and names `company-outcome`. `harrier company-outcome <selector> <code>`
  records a `company` outcome, accepts only company codes, and refuses a row
  with no `applied_date` (a company cannot reject an application that was
  never sent). The status it sets follows from the code: `interview_invited`
  moves the job to `interviewing`, every other company code to `rejected`,
  both through the existing transition rules.
- **The `interviewing` verb is a company outcome.** `harrier interviewing`
  and the API verb `interviewing` already exist and move an applied job to
  `interviewing`. They record a `company` outcome with code
  `interview_invited`, exactly as `company-outcome <selector>
  interview_invited` does. An interview is something the company did; left
  as a candidate decision, the old verb would be a back door that files a
  company action under the candidate (found while writing spec 080).
- **In storage.** Every code in `reasons.py` belongs to exactly one actor, and
  the event row stores both, so the pair cannot disagree: a CHECK constraint
  ties `kind = 'outcome'` to `actor = 'company'`.
- **On read.** Anything that derives a label (spec 077's export) reads the
  candidate's decision from `decision` events with `actor = 'candidate'`, and
  the company's response from `outcome` events. "Applied, then rejected by
  the company" is a positive candidate decision followed by a negative
  company outcome, never a negative.

The row's `status` still becomes `rejected` for a company rejection, as it
does today, so the queue, digest and transitions (spec 036) behave exactly as
before. `rejection_reason` still receives the free text. The separation lives
in the event, which is where the history is.

### The event row

Migration 5:

```sql
CREATE TABLE job_events (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    kind TEXT NOT NULL CHECK (kind IN ('created', 'decision', 'outcome')),
    actor TEXT NOT NULL CHECK (actor IN ('candidate', 'company', 'system', 'unknown')),
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '',
    reason_text TEXT NOT NULL DEFAULT '',
    fit_score TEXT NOT NULL DEFAULT '',
    scoring_version TEXT NOT NULL DEFAULT '',
    description_sha256 TEXT NOT NULL DEFAULT '',
    backfilled INTEGER NOT NULL DEFAULT 0 CHECK (backfilled IN (0, 1)),
    CHECK ((kind = 'outcome') = (actor = 'company'))
);
CREATE INDEX idx_job_events_job ON job_events(job_id, at);
CREATE TRIGGER job_events_append_only_update BEFORE UPDATE ON job_events
    BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;
CREATE TRIGGER job_events_append_only_delete BEFORE DELETE ON job_events
    BEGIN SELECT RAISE(ABORT, 'job_events is append-only'); END;
```

- `fit_score` and `scoring_version` are copied from the row at the moment of
  the event, before the write. They record what the candidate was looking at
  when they decided. Later rescoring does not touch them.
- `description_sha256` is the SHA-256 of the cached description at event
  time, or empty when none is cached. It pins the text the decision was made
  on without copying it.
- `reason_text` is the free text as entered. `reason_code` is the code: given
  explicitly, or inferred by `reasons.infer_code(text)`, or `unclassified`
  when inference finds nothing. Non-rejection decisions carry an empty code.
- Append-only is enforced by the database, not by convention. A correction
  is a new event, never an edit.

### Reason codes

`reasons.py` holds the table. Each code has one actor. Shape, not the final
list (the table in code is the authority, and adding a code is a code change
reviewed against this spec):

| Actor | Codes |
|---|---|
| `candidate` | `not_remote`, `location`, `stack`, `role_too_senior`, `role_too_junior`, `contract_type`, `language`, `timezone`, `company`, `compensation`, `other` |
| `company` | `company_rejected`, `ghosted`, `no_response`, `assessment_failed`, `interview_invited` |
| `system` | `vacancy_closed`, `duplicate`, `application_expired`, `ai_evaluation` |
| `unknown` | `unclassified` |

`infer_code` maps free text through an ordered pattern table: "rejected by
company" and "rejected without an offer" to `company_rejected`, "ghosted" to
`ghosted`, "no response" to `no_response`, "vacancy closed" and its variants
to `vacancy_closed`, "ai-evaluation:" to `ai_evaluation`, and so on. The
patterns are generic phrases, not tracker contents.

**What inference may decide.** A text that infers a company code on a
candidate verb is the separation rule meeting old habits: `harrier reject 12
"rejected by company"` is how company rejections were recorded until now.
The CLI refuses it and names `company-outcome`. The API, which cannot be
changed here, records it as a `company` outcome when the row has an
`applied_date`, and as `unknown` / `unclassified` when it does not. It never
records a company verdict as a candidate decision.

### Backfill

`harrier events backfill` (a `database`-class command, spec 074) writes
events for rows that have none, marked `backfilled = 1`, from what the row
still holds:

- a `created` event at `created_at`;
- an `applied` candidate decision at `applied_date`, when set;
- for a `rejected` row, one event at `updated_at` with actor and code from
  `infer_code(rejection_reason)`; if the code is a company code and
  `applied_date` is set, it is an `outcome`; if the inferred actor contradicts
  the row (a company code with no `applied_date`), it is `unknown` /
  `unclassified`.

`fit_score` and `scoring_version` on backfilled events are empty: the score
at decision time was never kept, and copying today's value would claim
something the data cannot show. `at` on a backfilled rejection is
`updated_at`, an upper bound and not the decision time; `backfilled = 1` is
how a reader knows not to treat it as one.

Idempotent: a job with any event is skipped, so a second run writes nothing.
`--dry-run` prints counts per kind, actor and code and writes nothing. The
counts are printed locally and never committed (ADR-008).

### Reading

`harrier events show <selector>` prints a job's events in order. The queue,
digest and API keep reading the `jobs` row; nothing that exists today reads
`job_events`. Its first reader is spec 077's export.

## Failure modes

- A status write whose event insert fails is rolled back with it: one
  transaction, both or neither (planned test_a_status_change_and_its_event_commit_together).
- A company code on `reject`, or a candidate code on `company-outcome`: exit
  2, nothing written, the message names the right verb.
- `company-outcome` on a row with no `applied_date`: exit 2, nothing written.
- An UPDATE or DELETE on `job_events`: aborted by the trigger.
- Free text that matches no pattern: `unclassified`, never a guess.
- Must not introduce: a second writer of `job_events`; a change to the status
  lifecycle or the legal transitions (spec 036); a company verdict stored as a
  candidate decision; reason text in logs; a rewrite of backfilled events.

## Acceptance criteria

Tests marked planned do not exist yet. They are named here so the
implementation has a target; the implementing change cites each one in
backticks, where `tests/test_spec_structure.py` checks it exists.

Tests in `services/api/tests/test_job_events.py` unless named otherwise. All
rows are synthetic.

- [ ] Every `set_status` call appends exactly one event, with the row's
      `fit_score` and `scoring_version` from before the write, and `add_job`
      appends one `created` event
      (planned test_every_status_change_appends_one_event,
      planned test_an_event_records_the_score_the_candidate_saw)
- [ ] A status change and its event commit together or not at all
      (planned test_a_status_change_and_its_event_commit_together)
- [ ] `job_events` refuses UPDATE and DELETE
      (planned test_job_events_is_append_only)
- [ ] A company rejection is recorded as a `company` outcome and never as a
      candidate decision: `company-outcome` writes `kind='outcome'`,
      `actor='company'`; `reject` with a company code refuses and names
      `company-outcome`; the CHECK constraint refuses a hand-built row that
      pairs `outcome` with any other actor
      (planned test_a_company_verdict_is_never_a_candidate_decision)
- [ ] The `interviewing` verb, from the CLI and from the API, records a
      `company` outcome with code `interview_invited`
      (planned test_the_interviewing_verb_is_a_company_outcome)
- [ ] `company-outcome` refuses a row with no `applied_date`
      (planned test_a_company_cannot_reject_an_application_never_sent)
- [ ] Every code belongs to exactly one actor, and `infer_code` maps each
      documented phrase to its code and an unknown phrase to `unclassified`
      (planned test_every_reason_code_has_one_actor, planned test_infer_code)
- [ ] An API rejection with company text on an applied row is recorded as a
      company outcome, and on an unapplied row as `unknown`
      (planned tests/test_ui_tracker.py::test_api_rejection_text_never_becomes_a_candidate_decision)
- [ ] The AI-evaluation auto-reject is a `system` decision with code
      `ai_evaluation` (planned tests/test_offers.py::test_auto_reject_is_a_system_decision)
- [ ] Backfill writes the documented events with `backfilled=1` and empty
      scores, separates company outcomes from candidate decisions by the same
      rules, and is idempotent; `--dry-run` writes nothing
      (planned test_backfill_reconstructs_what_the_row_still_holds,
      planned test_backfill_is_idempotent)
- [ ] A migrated database matches a fresh one
      (`tests/test_scoring.py::test_a_migrated_database_matches_a_fresh_one`,
      extended to cover migration 5)
- [ ] Reopening a rejected job (spec 072) appends a candidate decision and
      leaves the earlier rejection event in place
      (planned test_reopening_keeps_the_history)
- [ ] `tests/test_tracker_invariants.py` and the transition tests pass
      unchanged
- [ ] `events backfill` and `events show` are `database` class
      (`tests/test_delegation.py`)
- [ ] No reason text, company or title appears in a log line written by this
      code (planned test_event_writes_log_no_reason_text)
- [ ] No real tracker row appears in a fixture (ADR-008)
- [ ] All gates green on PR

## What this gives spec 077

Stated so the value is checkable rather than assumed:

- **Clean labels.** Candidate decisions and company outcomes are separate
  columns of truth. Skips, applications, company rejections and system
  closures no longer share a word.
- **An ordinal top class that can fill.** `interview_invited` is a company
  outcome with a verb to record it. The three-class model spec 077 defers
  needs exactly this.
- **Decision-time splits.** The time-ordered split can use the decision time
  for live events instead of `added_at`.
- **Measurable selection bias.** The score and version shown at decision time
  are kept, so acted-on rate by score band becomes a query.
- **Reproducible features.** `description_sha256` says whether the cached
  description is the one that was judged.

Spec 077 is amended on its own branch to read labels from events once this
lands.

## Honest limitations

- **History before this change is reconstructed, not recorded.** Backfilled
  rejections carry `updated_at` as their time, no score, and an inferred code.
  They are marked, and 077 can include or exclude them, but they are not
  better than today's data.
- **The code is only as good as its entry.** Until spec 080 gives the browser
  a code selector, browser rejections rely on inference, and new free text
  that fits no pattern is `unclassified`.
- **Outcomes depend on being recorded.** The tracker has no rows in
  `interviewing` today. Nothing here detects an interview invitation; the
  candidate has to record it. Gmail-watch could propose outcomes from
  replies, which is a separate spec.
- **What was shown is still not recorded.** The event says what the row
  scored when decided, not where it sat in the queue or whether it was ever
  displayed. Nearly every row gets an explicit decision, so this is a smaller
  gap than it would be in a feed, but it is a gap.

## Out of scope

- A reason-code selector and a company-outcome action in the web UI, and the
  API contract fields they need: spec 080.
- Gmail-watch proposing company outcomes from replies.
- Recording queue position or impressions.
- Guaranteeing a cached description for every new row, including manual
  adds. Some recent decided rows have no cached description; that is a
  capture defect with its own spec.
- Any change to statuses, transitions, the queue, the digest or scoring.
- Spec 077's label definition (amended separately after this lands).

## Proof / origin

- `services/api/src/harrier/tracker/store.py::set_status`: the single status
  writer, which overwrites and keeps no history.
- `services/api/src/harrier/offers/batch.py`: the system auto-reject that
  writes `rejected` with a prefixed free-text reason.
- `services/api/src/harrier_cli/main.py`: `reject` takes free-text reason
  words, which is how company verdicts and candidate skips came to share a
  column.
- Spec 077, Label and Selection bias: the facts this spec makes recordable.
- Spec 036: the transitions this spec leaves untouched. Spec 072: reopening,
  which now leaves history instead of erasing it.
- ADR-003: one tracker write path, which the event writes join rather than
  bypass. ADR-008: events live in the database, never in git.

## Amendment (2026-10-06, before approval)

The existing `interviewing` verb records a company outcome
(`interview_invited`), not a candidate decision. Spec 080, which moves the
browser's Interviewing button into a Company replied control, found that the
verb would otherwise remain a way to record a company action as the
candidate's. Stated under Behavior and proved by
planned test_the_interviewing_verb_is_a_company_outcome.
