---
spec: 077
title: The fit score is learned from what the candidate acted on, and says why
status: accepted
approved: yes
milestone: M8
depends: [031, 032, 033, 074, 078, 079]
---

# Spec 077: The fit score is learned from what the candidate acted on, and says why

## Problem

`score_job` in `services/api/src/harrier/screening/rules.py` adds up keyword
hits: a base of 30, an exact-title bonus, include keywords, `SKILL_SIGNALS`,
`PREFERRED_SIGNAL_WEIGHTS`, a remote bonus of 10, a preferred-region bonus of
8 and a domain bonus of 5. Two properties follow from that shape.

**It rewards length.** Every table is matched against the whole description
and every match adds. A long posting that names more technologies scores
higher than a short one for the same role, whether or not it is a better fit.

**It cannot see a blocker.** Nothing in the score subtracts. A synthetic
reconstruction of a real case: a US-only W-2 posting that says "open to
candidates anywhere in the US" matches `\banywhere\b` in
`PREFERRED_REGION_PATTERNS`, earns the preferred-region bonus, and scored 151,
near the top of the queue. Restrictions that make a posting impossible for
this candidate (US-only scope, W-2 or at-will employment, no visa
sponsorship, student visas, a required experience level far above the
candidate's) are invisible to the sum, and the sum is what the queue and the
digest rank by.

The tracker records what the candidate did with every row it ranked: which
rows they moved forward and which they rejected, and why. That is a label.
The score never learns from it.

Specs 031 and 033 settled the cutoff (there is none: the gates filter, the
score ranks), the single score write path (`score_fields()`), and the score
version. This spec does not reopen any of them. It changes only how the
integer in `fit_score` is produced.

Spec 078 already ranks a blocked posting below every eligible one, with a
penalty derived from the rules. That fixes the 151 case without data. This
spec goes further: it learns how much each signal matters from the
candidate's own decisions, which spec 079 records with who made them and
why.

## Scope

A learned ranking model with an explicit fallback to the rules, built in four
separate stages with typed inputs and outputs:

1. **Validate**: decide whether the model can judge this job at all.
2. **Extract**: turn title, location and description into a fixed, ordered
   feature vector. Plain Python, deterministic.
3. **Score**: logistic regression evaluated in plain Python from a JSON model
   file. No scikit-learn, no numpy, no pickle at inference.
4. **Write**: map the probability to the integer `fit_score` and the
   contributions to `signals`, through `score_fields()` only.

Training is offline and split in two commands so the trainer never opens the
database:

- `harrier scoring export`: a `database`-class command (spec 074, so it is
  delegated into the container while the container runs). Reads decision
  events (spec 079), tracker rows and cached descriptions, labels them, runs
  the same extractor inference uses, and writes a feature export. Writes
  nothing to the database.
- `harrier scoring train`: a `host-only` command (spec 074). Reads only the
  export, fits the model with scikit-learn, evaluates it against the rules on
  a time-ordered held-out set, writes a dated model file and a report, and
  activates the model only when it beats the baseline and `--activate` is
  given.

Files touched: `services/api/src/harrier/scoring/` (new package: `features.py`,
`model.py`, `labels.py`, `export.py`, `train.py`), the three `score_job`
call sites (`screening/pipeline.py`, `capture.py`, `tracker/actions.py`)
which route through one `fit_score_for(job, cfg)` seam,
`screening/policy.py` (model identity in the digest),
`harrier_cli/main.py` (two subcommands and their classes),
`services/api/pyproject.toml` (one dev dependency, one import-linter
contract), `config/candidate.example.json` (one key), tests and synthetic
fixtures under `services/api/tests/`. The gates in `rules.py`, the tracker
schema, the API contract and `apps/web` do not change.

## Behavior

### Label

Labels are read from the decision events of spec 079, not reconstructed from
the row. Per job, from its `job_events`:

| Label | Jobs |
|---|---|
| `1` acted on | at least one `decision` event with `actor = 'candidate'` that moved the job forward (to `shortlisted`, `tailored_cv_requested` or `applied`). A later Withdraw, or a company rejection, does not undo it: the label is the candidate's judgement of the posting. |
| `0` skipped | the candidate's first `decision` is a move to `rejected` with `actor = 'candidate'`, and no forward candidate decision follows it. |
| excluded | no candidate decision yet (undecided); the job was closed by a `system` decision (`vacancy_closed`, `duplicate`, `application_expired`, `ai_evaluation`) before the candidate judged it; a job whose decision event carries a `description_sha256` that does not match the cached description (the text judged is not the text the extractor would read); a job with no cached description of at least `MIN_DESCRIPTION_LENGTH_FOR_SCORING` characters. |

**Company outcomes are never a label for this model.** `outcome` events with
`actor = 'company'` (rejected, ghosted, no response, assessment failed) say
what an employer did with an application, not what the candidate thought of
the posting. Spec 079 stores them apart so that no reader has to guess; this
spec does not read them for the binary label
(`test_a_company_outcome_is_never_a_label`).

**Location-reason rejections stay `0`.** A candidate decision coded
`not_remote` or `location` is a posting that passed the automated gates and
was rejected by a person anyway, which is the blind spot this model exists to
see. The model ranks such postings low; it never rejects anything (Gates,
below).

**Backfilled events.** Jobs decided before spec 079 have events marked
`backfilled = 1`, with inferred codes and no score at decision time. They are
included by default, because they hold most of the history. `harrier scoring
train --live-only` drops them, and the report always gives the test metrics
for live events separately once that subset reaches the test minimum.

**Why binary and not the three-class ordinal.** The top class would be
company `interview_invited` outcomes on jobs the candidate acted on. Spec 079
gives that class a verb and a home, but it is close to empty today, so a
third class would be a coefficient vector fitted to almost nothing. The model
file format carries `classes` and `class_values` so the ordinal model, and the
collapse `p = 0*p0 + 0.5*p1 + 1*p2`, can be added by amendment once the top
class reaches the same minimum as the positives below, without a format
change.

**Selection bias.** Every labeled job reached the tracker through the gates,
and the queue that showed it was ranked by the rule score, so acted-on jobs
lean toward jobs the rules already liked. The spec does not pretend to
correct this; it limits and measures it:

- The old score is never a feature. `fit_score`, `score`, `signals` and
  `scoring_version`, on the row or on an event, are not read by the
  extractor, and the trainer refuses a feature order that names any of them
  (`test_the_old_score_is_never_a_feature`).
- The rule score appears in the export only as the baseline column,
  recomputed with `score_job` under the current policy (including spec 078's
  blocker penalty), so model and baseline are compared on identical inputs.
- The evaluation report gives average precision within each tercile of the
  baseline score on the test set. A model that only reproduces the rules has
  near base-rate precision inside a tercile; one that learned something else
  does not.
- For live events, spec 079 keeps the `fit_score` and `scoring_version` the
  job carried when it was decided. The report gives the acted-on rate by
  that score band, per scoring version: the first direct measure of how much
  the shown ranking drove the decisions. It is reported, never used as a
  feature or a weight.

### Split

Labeled jobs are ordered by the time of the candidate decision that set
their label: the event `at` for live events, and `added_at` for backfilled
ones (a backfilled rejection's `at` is `updated_at`, an upper bound that
rescoring can push late, so it is not trusted for ordering). Ties are broken
by job id. The earliest 70 percent are training rows and the rest are test
rows. No test row is earlier than any training row
(`test_the_split_is_time_ordered`). Regularization strength is chosen by
forward-chaining cross-validation inside the training window only.

### Features

Curated, named, and few. Each is computed from title, location and
description; each reuses an existing table in `rules.py` where one exists.
Fixed order, stored in the model file as `feature_order`.

| Name | Definition | Kind |
|---|---|---|
| `skill_signal` | sum of `SKILL_SIGNALS` weights matched in title and description | numeric, p92 |
| `preferred_signal` | sum of `PREFERRED_SIGNAL_WEIGHTS` matched (EU-permit phrases stay positive, per the product invariant) | numeric, p92 |
| `title_fit` | 1 for `is_target_title_variant`, else matched include keywords over the include cap | 0 to 1 |
| `frontend_share` | frontend term weight over frontend plus backend term weight (new `BACKEND_TERMS` table beside `SKILL_SIGNALS`); 0.5 when neither appears | 0 to 1 |
| `explicit_emea_remote` | the explicit-EMEA test spec 078 uses for its location override: a `PREFERRED_REGION_PATTERNS` match other than the ambiguous `worldwide`, `global`, `anywhere` | 0 or 1 |
| `us_scope` | `rules.blockers(job)` reports class `us_scope` (spec 078's `US_SCOPE_PATTERNS`, with its explicit-EMEA location override) | 0 or 1 |
| `employment_blocker` | `rules.blockers(job)` reports class `employment` (spec 078's `EMPLOYMENT_BLOCKER_PATTERNS`) | 0 or 1 |
| `years_gap` | required years parsed from the description minus `candidate.years_experience`, floored at 0 | numeric, p92 |

Grouped rather than one feature per phrase, because each phrase alone is
rare and its coefficient would be noise at this data size. `signals` still
names the phrase that fired.

The blocker features call spec 078's `rules.blockers(job)` and nothing else.
There is one definition of a blocker, in `rules.py`: the rule penalty and the
model feature cannot disagree about whether a posting is blocked, and a
phrase added to a table reaches both and moves the policy version once
(`test_blocker_features_reuse_the_rule_tables`). The EU-permit stripping and
word-bounding are spec 078's and are inherited, not repeated.

`years_gap` needs the candidate's years, which no configuration holds today.
A new key `candidate.years_experience` is added to the example
configuration with a placeholder value, and `candidate` gains it in
`DECIDING_PATHS`. The real value lives in the profile database like the rest
of the candidate configuration (ADR-008). Missing key: the feature is 0 and
`signals` says `years_experience unset`.

There is deliberately no source feature (the ingestion-only invariant: no
per-source scoring) and no description-length feature (length is the defect).

**Normalization.** Numeric features are divided by their 92nd percentile over
the training rows and clipped to 1. The percentiles are computed from
training rows only and stored in the model file, so test rows never inform
their own scaling (`test_p92_stats_come_from_training_rows_only`). A
percentile of 0 stores as 1, so a feature that never fired in training
cannot divide by zero.

**Categorical features.** None in the first model. The format carries an
`encoders` map (feature name to vocabulary list, unknown value to all zeros)
so a categorical such as archetype can be added by amendment once the data
supports the extra coefficients.

### Model

L2-regularized logistic regression: the smallest model that can learn signed,
explainable weights. Inference is

`p = 1 / (1 + exp(-(intercept + sum(coef[i] * x[i]))))`

in plain Python over `feature_order`. Anything larger (trees, boosting) would
need more labels than exist and would make "why this score" a guess.

The model file is JSON:

```json
{
  "format_version": 1,
  "kind": "logistic_regression",
  "created_at": "YYYY-MM-DD",
  "feature_order": ["skill_signal", "..."],
  "coefficients": [0.0],
  "intercept": 0.0,
  "classes": [0, 1],
  "class_values": [0.0, 1.0],
  "p92": {"skill_signal": 1.0},
  "encoders": {},
  "training": {"rows": 0, "positives": 0, "split_at": "YYYY-MM-DD"},
  "evaluation": {"metric": "average_precision", "model": 0.0, "baseline": 0.0, "delta_ci95": [0.0, 0.0]}
}
```

The `training` block holds counts and a date only, no row ids, companies or
titles. The values above are shape placeholders, not results.

**Model identity** is the first 12 hex characters of the SHA-256 of the file
bytes. Files are written with sorted keys and fixed float formatting so the
same fit produces the same bytes.

**Why not pickle.** Unpickling executes code chosen by whoever wrote the file.
A file under `data/` is one bind mount away from anything else on the host,
and JSON makes the model diffable, hashable and readable by the scorer
without scikit-learn. Not kept.

**Why not the post-hoc sigmoid boost.** The reference model adjusted its
probability after prediction with a hand-tuned curve on one input. That
hides what the model learned behind a second, unfitted model, and makes the
contributions in `signals` stop adding up to the score. If a feature
matters, it is a feature with a fitted coefficient. Not kept.

### Output

- `fit_score` is `round(100 * p)`, an integer from 0 to 100. The digest,
  queue and API keep reading `fit_score` unchanged. No reader compares the
  score with a fixed number (checked at spec time), so the change of range
  breaks no threshold.
- `signals` starts with `scorer=model:<model id>` and `p=<two decimals>`,
  then the five largest contributions `coef[i] * x[i]` by absolute value,
  each signed, for example `-us_scope(-1.70) "anywhere in the us"` and
  `+skill_signal(+0.62)`. A blocker entry names the phrase that fired. "Why
  this score" is always answerable from the row.
- `scoring_version` is the policy version (spec 031) of the scorer that
  produced this row: with the model id when the model scored it, without it
  when the rules did.
- All four fields are written by `score_fields()` and nothing else.
  `test_every_score_field_is_written_together` stays green unchanged.

### Gates

The gates still filter and the model only ranks. `title_allowed` and
`remote_region_allowed` run before scoring exactly as now, and a job the gates
reject is never scored. Remote-only and EMEA enforcement are not touched. A
blocker feature lowers a rank; it never rejects
(`test_the_model_never_changes_a_gate_verdict`).

### Versioning

`policy_version` gains one entry, `"model": <model id>` or `"none"`. So
activating, retraining or removing a model changes the policy version and
therefore `scoring_version` on every row scored afterwards. Rows scored
earlier keep their score and version; old rows with no version still read as
`unknown` through `stored_version`. Nothing recomputes history.

A consequence stated rather than discovered: the policy version also stamps
seen decisions (spec 031), so a model change makes stored gate rejections
eligible for `harrier reconsider`. Reconsideration re-runs the unchanged
gates and reaches the same verdicts. The cost is one re-screen, the result is
correct, and keeping one version rather than two is the simpler contract.

Open rows (prospect, shortlisted) scored before activation sit on the old
rule scale. `harrier reevaluate` rescores them through the same seam
(`test_reevaluate_uses_the_active_model`); rows already decided keep their
historic score, which is the record spec 033 protects.

### Fallback

The rules score the job, and the row records why, when:

| Condition | `signals` entry |
|---|---|
| no file at the active model path | `fallback=model-missing` |
| file unreadable, wrong `format_version`, `feature_order` not equal to the extractor's, a non-finite number, or a missing `p92` entry | `fallback=model-invalid` |
| no description of at least `MIN_DESCRIPTION_LENGTH_FOR_SCORING` characters | `fallback=description-missing` |
| extraction raises | `fallback=extraction-failed` |

A fallback row's `signals` starts with `scorer=rules` and the fallback entry,
followed by the usual rule reasons, and its `scoring_version` is the policy
version with `"model": "none"`. A model-missing or model-invalid condition is
logged once per run at warning level with the path and the check that
failed; no candidate or contact identity is logged. A run never fails
because the model did.

This replaces the reference model's fixed 0.5 "skip prediction". A constant
probability would put every unjudgeable job in the middle of the ranking
with no visible reason; the rule score is a real ranking and the row says
which scorer produced it.

### Evaluation and the ship rule

`harrier scoring train` computes, on the test rows:

- **Average precision** of the model and of the baseline (the current rule
  score, with spec 078's blocker penalty). Average precision because the queue is read from the top: it
  rewards putting acted-on rows first and is not inflated by the large
  number of easy negatives the way ROC AUC is.
- A **paired bootstrap** over test rows (2000 resamples, fixed seed) of the
  difference in average precision, reported as a 95 percent interval.
- Also reported, not gating: ROC AUC for both, precision at 10, the base rate,
  per-tercile average precision (selection bias, above), and every
  coefficient with its feature name.

The report is written to `data/scoring/reports/report-YYYYMMDD.json` and
printed.

A model may be activated only when all three hold:

1. the lower bound of the 95 percent interval of the average precision
   difference is above 0;
2. superseded by spec 081, which replaced "the `us_scope` and
   `employment_blocker` coefficients are negative": blockers are no longer
   features, and a blocked posting is floored below every eligible one by
   rule instead;
3. the minimum label counts below were met.

Otherwise `train` still writes the dated model file and the report, exits 3,
and states which condition failed. `--activate` on a failing model is
refused. Activation copies the dated file to the active path.

### Minimum data (judgement for Akin to approve)

Proposed minimum, stated with its reasoning so it can be argued with:

- **Training window: at least 10 positives per feature** (80 for the eight
  features above). Ten events per variable is the common rule of thumb for
  logistic regression below which coefficients and their signs are unstable;
  L2 regularization softens it but does not make a sign learned from five
  examples trustworthy, and the signs are what `signals` shows.
- **Test window: at least 30 positives.** Below that, the bootstrap interval
  on an average precision difference is too wide to tell a better ranker from
  a lucky one, and the ship rule would mostly refuse anyway.

Below either minimum, `train` writes no model file, writes the report with
the counts, and exits 3 with `insufficient labels`. The rules keep scoring.
These two numbers are a judgement, not a measurement, and they are Akin's to
approve or change before implementation.

**While the data is below the minimum.** The rules keep scoring, and since
spec 078 they already rank blocked postings last. A refused training run
costs nothing but the report.

### Privacy (ADR-008)

Every new path and its class:

| Path | Class | Why |
|---|---|---|
| `data/scoring/active-model.json` | never-in-git | derived from personal tracker rows |
| `data/scoring/models/job-fit-YYYYMMDD-<id>.json` | never-in-git | same |
| `data/scoring/exports/features-YYYYMMDD.jsonl` | never-in-git | per-row labels and features from the tracker |
| `data/scoring/reports/report-YYYYMMDD.json` | never-in-git | counts and metrics describing the real search |
| `services/api/src/harrier/scoring/**` | public | code |
| `services/api/tests/test_scoring_model.py`, `services/api/tests/fixtures/scoring/**` | public | synthetic fixtures only |

All four `data/scoring/` paths are already covered by the `data/**` pattern
in `config/data-classification.json`, so the guarded file needs no edit. If
review prefers an explicit entry, that edit touches a guarded file and needs
Akin's approval under this spec. `test_every_scoring_path_is_never_in_git`
asserts the coverage through the matcher `tests/test_classification_coverage.py` uses.

The export holds row id, `added_at`, label, the feature vector and the
baseline score. It holds no company, title, URL, description or rejection
reason text: the trainer does not need them and an export is a file that
travels. Fixtures are synthetic: invented company names, invented postings,
no text copied from a real posting. Spec text, commit messages and the PR
carry no counts or distributions from the real tracker.

### Dependency

`scikit-learn` is added to the `dev` dependency group only. It is needed to
fit the model and to prove, in CI, that plain-Python inference reproduces
scikit-learn's probabilities. It pulls in numpy and scipy, which is why it
is kept out of runtime: the image installs `--no-dev` (Dockerfile), and an
import-linter `forbidden` contract stops every `harrier` module except
`harrier.scoring.train` from importing `sklearn` or `numpy`. `train` is
`host-only` because the container cannot import it; run without the dev
group, it exits 2 naming the install command. It was put in `dev` rather than
a named optional group so CI installs it by default: an equality test that
skips when its dependency is absent is a guard that never runs.

## Failure modes

- No model, invalid model, missing description, extraction error: rules
  score the row and the row says so (Fallback).
- Too few labels: no model is written, the rules stay, the report says why.
- A model that does not beat the rules, or rewards a blocker: written for
  inspection, never activated.
- A model trained against an older extractor: `feature_order` mismatch,
  `model-invalid`, rules. A new feature therefore requires a retrain, never a
  silent zero.
- Must not introduce: a gate decision that depends on the model; a stored
  score recomputed under a new model; a score written outside
  `score_fields()`; scikit-learn or pickle on the inference path; personal
  data in a committed file.

## Acceptance criteria

Tests live in `services/api/tests/test_scoring_model.py` unless named
otherwise. Every fixture is synthetic.

- [x] A synthetic US-only W-2 posting ("anywhere in the US", W-2, no
      sponsorship) ranks below a synthetic EMEA-remote posting with the same
      skill keywords under a fixture model, and the US posting's `signals`
      name both blockers and the phrases that fired them. As amended by spec
      081 the blocked posting is floored by rule rather than by a learned
      weight
      (`test_us_only_w2_posting_ranks_below_emea_remote_with_same_keywords`)
- [x] Feature extraction is deterministic: the same job yields the same
      vector across repeated calls and across a fresh interpreter, and
      `EU_PERMIT_PATTERNS` phrases never set a blocker
      (`test_feature_extraction_is_deterministic`,
      `test_eu_permit_phrases_are_never_blockers`)
- [x] The model JSON round-trips and scores identically without
      scikit-learn: a subprocess with `sklearn` and `numpy` blocked from
      import scores a fixture model; and a model fitted by scikit-learn on
      synthetic data, written and read back, gives probabilities equal to
      `predict_proba` within 1e-9
      (`test_model_json_round_trips_and_scores_identically_without_sklearn`)
- [x] A missing model falls back to the rules, the score equals `score_job`'s,
      and `signals` records `scorer=rules` and `fallback=model-missing`
      (`test_missing_model_falls_back_to_rules_and_records_it`); the same
      for an invalid file, a missing description and an extraction error
      (`test_each_fallback_condition_is_recorded`)
- [x] `policy_version` changes when the model file changes, and when it is
      removed (`tests/test_seen_policy.py::test_policy_version_changes_when_the_model_file_changes`)
- [x] A fallback row carries the rules version and a model row carries the
      model version (`test_scoring_version_names_the_scorer_that_was_used`)
- [x] `signals` lists the five largest contributions, signed, after the
      scorer and probability entries (`test_signals_name_the_top_contributions_with_signs`)
- [x] Labels come from candidate decision events: acted-on, skipped, and
      each exclusion rule, over synthetic events
      (`test_labels_come_from_candidate_decisions`)
- [x] A company outcome never sets or changes a label, and a job closed by a
      system decision before the candidate judged it is excluded
      (`test_a_company_outcome_is_never_a_label`,
      `test_system_decisions_are_excluded`)
- [x] A decision whose `description_sha256` does not match the cached
      description is excluded and counted in the report
      (`test_a_decision_on_a_different_description_is_excluded`)
- [x] `--live-only` drops backfilled events, and the report separates live
      test metrics (`test_live_only_drops_backfilled_events`)
- [x] Blockers are spec 078's `rules.blockers`, so a phrase added to a table
      changes both the rule penalty and, as amended by spec 081, the learned
      score's floor (`test_blocker_features_reuse_the_rule_tables`)
- [x] The split is time-ordered and p92 stats use training rows only
      (`test_the_split_is_time_ordered`, `test_p92_stats_come_from_training_rows_only`)
- [x] The old score is never a feature (`test_the_old_score_is_never_a_feature`)
- [x] `train` refuses below the minimum and refuses to activate a model that
      does not beat the baseline, each with exit 3 and the reason
      (`test_train_refuses_below_minimum_labels`,
      `test_train_refuses_a_model_that_does_not_beat_the_rules`). The refusal
      of a non-negative blocker coefficient is superseded by spec 081
      (`test_train_ships_without_a_blocker_condition`)
- [x] The model never changes a gate verdict (`test_the_model_never_changes_a_gate_verdict`);
      `tests/test_screening.py` passes unchanged
- [x] `reevaluate` scores through the active model
      (`tests/test_tracker_cli.py::test_reevaluate_uses_the_active_model`)
- [x] `export` is `database` class and `train` is `host-only`
      (`tests/test_delegation.py`, where every subcommand already must carry a class)
- [x] Inference does not import scikit-learn or numpy (import-linter contract
      in `services/api/pyproject.toml`, run by `just check`)
- [x] Every `data/scoring/` path is never-in-git
      (`test_every_scoring_path_is_never_in_git`)
- [x] `tests/test_scoring.py::test_every_score_field_is_written_together` and
      `::test_no_reader_takes_a_field_the_writer_does_not_fill` pass unchanged
- [x] No real posting, company or tracker statistic appears in a fixture,
      the spec, or a commit message (ADR-008)
- [ ] All gates green on PR

## Honest limitations

- **The label is a proxy.** "Acted on" measures what the candidate chose to
  pursue, not what would have led to an offer. Interview outcomes are
  recorded since spec 079 but too few to learn from, so the model learns
  taste, not success.
- **Most history is backfilled.** Decisions made before spec 079 have
  inferred codes, an approximate time and no score at decision time. They
  are marked and can be dropped, but until live events accumulate the model
  learns mostly from reconstructed labels.
- **Selection bias is measured, not removed.** Rows the old ranking buried
  were less likely to be read, so some skipped rows were never really
  judged. Without knowing which rows were shown and read, no reweighting is
  honest. The per-tercile report is a check, not a correction.
- **Gate-rejected postings are never labeled.** The model knows nothing about
  postings the gates removed. That is acceptable only because the model never
  sees them either.
- **The label window is short.** Cached descriptions exist only for rows
  added since the description cache was introduced, so the labeled set
  covers a short stretch of one person's search. A held-out window that short
  can confirm a gain; it cannot show the gain survives a change in the market
  or in the candidate's preferences. Retraining is manual.
- **Pattern features have pattern blind spots.** A blocker phrased in a way
  the tables do not list is invisible to the model, exactly as it is to the
  rules today. The model can weigh a signal; it cannot discover one.
- **`years_gap` parses numbers from free text.** "5+ years" and "five years"
  are not the same string. Parse misses read as no requirement.
- **A score of 0 to 100 is not comparable with a rule score.** Rows across
  the switch are distinguished by `scoring_version`, as spec 033 intends, but
  a list that mixes both versions still sorts them together.

## Out of scope

- The region gate letting "anywhere in the US" through
  `remote_region_allowed`. That is a gate defect and belongs to spec 032.
  This spec only ranks such a posting low.
- The web UI, including any display of contributions beyond the existing
  `signals` field.
- LLM-based scoring or LLM-extracted features.
- Rule-score blocker penalties. Spec 078; this spec reuses its tables.
- Recording decisions and company outcomes. Spec 079; this spec reads them.
- The three-class ordinal model and categorical features (format-ready,
  added by amendment when the data supports them).
- Automatic or scheduled retraining.

## Open decisions for Akin

1. The minimum data thresholds: 10 positives per feature in training, 30 in
   test.
2. Whether candidate rejections coded `not_remote` or `location` count as
   negatives, as proposed, or are excluded.
3. Whether backfilled events are included by default, as proposed, or only
   with a flag.
4. Ship condition 2 (blocker coefficients must be negative) as a hard gate.
5. `candidate.years_experience` as a new deciding configuration key.

## Proof / origin

- `services/api/src/harrier/screening/rules.py` (`score_job`,
  `PREFERRED_REGION_PATTERNS` with `\banywhere\b`): the additive scorer and
  the ambiguous region pattern behind the 151 case.
- `services/api/src/harrier/tracker/score.py` (`score_fields`,
  `stored_version`) and spec 033: the single write path and versioning this
  spec writes through.
- `services/api/src/harrier/screening/policy.py` and spec 031: the policy
  version the model identity joins.
- Spec 074 and `harrier_cli/main.py` `COMMAND_CLASSES`: why export runs in the
  container and train on the host.
- Spec 078: the blocker tables and `rules.blockers`, reused as features, and
  the penalized rule score used as the baseline.
- Spec 079: the decision events the labels are read from, and the separation
  of company outcomes from candidate decisions.
- Staged validate, preprocess, score pipeline; percentile normalization with
  clipping and stored stats; an encoder stored beside the model; an explicit
  fallback and dated model filenames: adapted from a private reference
  pre-screening service the author worked on. No code or data was copied;
  pickle loading and the post-hoc probability adjustment were deliberately
  not adopted (reasons above).
- The read-only audit of the local tracker that informed the label, split
  and minimum-data decisions was reported to Akin in the session, not
  recorded here (ADR-008).

## Amendment (2026-10-06, before approval)

Amended after specs 078 and 079 were written, at Akin's request:

- **Labels read spec 079's events.** The candidate's own decisions set the
  label; company outcomes never do; system closures are excluded. This
  replaces inference from `applied_date` and free-text reasons.
- **The split orders by decision time** for live events.
- **Blocker features call spec 078's `rules.blockers`.** One definition of a
  blocker serves the penalty and the model.
- **The baseline is the penalized rule score**, so the model has to beat the
  rules as they will be, not as they were.
- **The interim alternative and its open decision are gone**: spec 078 is
  that alternative.

The approval this spec carried before the amendment was reset to
`approved: no`, so that approval covers the amended text.

## Amendment (2026-10-06, during implementation)

What implementation found, each with what proves it:

- **No model is logged at info, not as a warning.** No model is the state of
  every installation until one is trained, so a warning would fire on every
  run, and `tests/test_demo.py::test_demo_discovery_needs_no_environment_keys`
  holds a demo run to logging nothing that looks broken. A model file that
  exists and is refused still warns. The row records `fallback=model-missing`
  either way, as specified.
- **`frontend_share` counts terms; it does not weigh them.** The backend side
  has no weights to borrow, and inventing some would be the hand tuning this
  model exists to replace. `FRONTEND_TERMS` and `BACKEND_TERMS` sit beside
  `SKILL_SIGNALS` in `rules.py`, and both join the policy fingerprint: a model
  scores what the extractor gives it, so a changed table moves the version
  just as a changed model does. TypeScript, Node and full stack name both
  sides and count for neither, and Angular for neither as the skill table
  does not name it.
- **Features match whole words.** The extractor reads the rule tables through
  `contains_word`, the discipline `rules.py` applies since the Siracusa
  defect, where `score_job` still matches substrings. `title_fit` reads the
  include keywords in the title alone.
- **`years_gap` reads a stated requirement.** A number of years counts only
  when "experience" follows it within a few words; a range counts as its
  lower bound; the largest requirement in the description wins.
  `test_required_years_reads_the_stated_requirement`.
- **The export says when and how each job was decided.** Beside the id,
  label, vector and baseline, each row carries the time that orders the split,
  whether the decision was live, and the score and version it carried when
  decided (for the selection-bias report). A header line names the feature
  order, the rules version the baseline was scored under, and how many jobs
  each exclusion removed. A job whose only decision has no known actor is
  counted apart, as `actor-unknown`. Still no text: `test_the_export_carries_no_text`.
- **Training needs both outcomes on both sides of the split.** A window with
  no negatives cannot be fitted or judged, so it is refused as insufficient,
  like a window short of positives. Regularization is chosen from a log grid
  of four strengths over four forward-chaining folds; the grid is a
  judgement, and which strength wins is the data's.
- **Scoring is out of reach of the sources too.** `harrier.scoring` joins the
  modules the "sources are ingestion only" contract forbids: a learned score
  is per-source scoring all the same if a source can reach it. The new
  sklearn and numpy contract needs `include_external_packages`; a numpy
  import added to the scorer breaks it.
- **The pipeline imports the seam where it calls it.** `harrier.scoring` reads
  the screening tables and the screening pipeline scores through
  `harrier.scoring`, so a module-level import is a cycle whenever scoring
  loads first. `test_every_scoring_module_imports_first` imports each module
  first in a fresh interpreter.
- **Activation writes atomically.** The container can be scoring while the
  host activates a model, and a model is identified by its exact bytes, so
  `harrier.atomicio.write_bytes_atomic` writes the file whole, the same
  write-then-rename `write_json_atomic` uses.
- **The fixtures are built in the tests.** Model files and exports are
  generated in `tests/test_scoring_model.py` from synthetic values, so no
  `tests/fixtures/scoring/` directory was needed.
- **The data check.** Run read-only against a copy of the local tracker,
  backfilled and exported there and then deleted, training was refused as
  insufficient labels: the rules keep scoring, as specified while the data is
  below the minimum. The counts were reported in the session, not here
  (ADR-008).

## Amendment: blockers are a floor, not features (spec 081, 2026-10-06)

Spec 081 supersedes the blocker parts of this spec. Blocked postings are rare
among the decisions the model learns from, so their weights came out near
zero with either sign, and ship condition 2 refused every model. Now:

- `us_scope` and `employment_blocker` are no longer features, leaving six,
  so the minimum is 60 training positives (still ten per feature).
- A blocked posting's model score is lowered by a penalty derived from the
  model's bounds, so it ranks below every eligible posting under either
  scorer, and `signals` names each blocker after the contributions.
- A labelled posting a blocker fires on is excluded from the export, as
  `blocked`.
- Ship condition 2 is gone.

The criteria above that described the old behavior point to spec 081's tests.
