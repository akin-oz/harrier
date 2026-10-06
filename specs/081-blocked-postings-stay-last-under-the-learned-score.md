---
spec: 081
title: Blocked postings stay last under the learned score
status: proposed
approved: no
milestone: M8
depends: [077, 078]
---

# Spec 081: Blocked postings stay last under the learned score

## Problem

Spec 077 asks the learned model to discover that a blocker (US-only scope,
W-2 and the other employment terms of spec 078) ranks a posting down, and its
ship condition 2 refuses any model whose two blocker weights are not
negative.

The model learns from the candidate's decisions, and blocked postings are
rare among them. The gates keep most of them out of the tracker, and since
spec 078 the rule score sinks the rest to the bottom of the queue, where few
are ever decided. The two blocker weights therefore rest on a handful of
rows: regularization pulls them to near zero, either sign is noise, and
condition 2 refuses every model however well it ranks everything else. A
read-only trial against a copy of the local tracker showed exactly this; its
results were reported in the session, not here (ADR-008).

The condition also guards the wrong thing. What matters is that a posting the
candidate cannot take ranks below every posting they can. Spec 078 guarantees
that for the rule score by derivation, not by learning, and the learned score
should get the same guarantee the same way.

## Scope

- `services/api/src/harrier/scoring/model.py`: `fit_score_for` applies a
  derived floor to a blocked posting's model score.
- `services/api/src/harrier/scoring/features.py`: `us_scope` and
  `employment_blocker` leave `FEATURE_ORDER`.
- `services/api/src/harrier/scoring/labels.py` and `export.py`: a labelled
  posting a blocker fires on is excluded, as `blocked`.
- `services/api/src/harrier/scoring/train.py`: ship condition 2 is removed.
- `specs/077-learned-fit-score.md`: amended in the implementing change to
  point here for the ship rule and the blocker features.
- Tests under `services/api/tests/`.

Spec 078's tables, its penalty on the rule score, the gates, the tracker
schema, the API contract and `apps/web` do not change.

## Behavior

### The floor

When `rules.blockers(job)` fires for a job the model scores, its score is
lowered by the smallest number that puts it below every unblocked model
score. Model scores lie between 0 and 100 (`round(100 * p)`), so by spec
078's own derivation over the model's bounds the penalty is 100 - 0 + 1 =
101: a blocked posting scores between -101 and -1, every unblocked one
between 0 and 100. It applies once however many blockers fire, and `signals`
gains one `blocker=<class> "<phrase>"` entry per blocker after the model's
contributions, in the format spec 078 uses.

A row the rules scored (any fallback) is unchanged: spec 078's penalty
already applies there. So under either scorer, a blocked posting ranks below
every posting the candidate can take.

### Blockers leave the features

Under a floor, a blocker weight can only reorder postings that are already
below everything eligible, so it buys nothing and costs two coefficients the
data cannot support. Worse, a weight that happens to come out positive would
show in `signals` as `+us_scope(...)`, which reads as the model rewarding a
posting the candidate cannot take. `FEATURE_ORDER` loses `us_scope` and
`employment_blocker`, leaving six features. There is still one definition of
a blocker, `rules.blockers`; it now decides the floor instead of feeding a
weight.

A model file written under the eight-feature order no longer matches the
extractor and is refused as `model-invalid` (spec 077). None is active.

### Blocked postings leave the labels

A labelled job whose posting a blocker fires on is excluded from the export,
counted as `blocked`. The floor ranks those postings, not the model, and
training on them would teach the other features to explain rejections that
the blocker caused. Training and evaluation are therefore about the postings
the model actually ranks, and the model is compared with the rules on the
same postings.

### The ship rule

A model may be activated when the minimum labels are met and the lower bound
of the 95 percent interval of the average precision difference is above 0.
Condition 2 is gone. The minimum stays ten training positives per feature,
which for six features is 60, and 30 test positives.

## Failure modes

- A blocker phrased in a way the tables do not list is not floored and is
  ranked by the model like any posting: spec 078's blind spot, unchanged.
- A table phrase that fires on an eligible posting floors it under both
  scorers. Spec 078's pre-merge check against acted-on rows is the guard.
- Must not introduce: a blocked posting ranked above an unblocked one under
  any scorer; a learned weight for a blocker; a gate decision that depends
  on the model.

## Acceptance criteria

Tests named here are planned; the implementing change cites each one in
backticks.

- [ ] A blocked posting scores below an unblocked one even when the model
      gives the blocked posting a near-certain probability and the unblocked
      one a near-zero probability, and the penalty is computed from the
      model's score bounds rather than written as a constant
      (planned test_a_blocked_posting_ranks_below_every_unblocked_model_score)
- [ ] A floored model row names its scorer, its probability, its
      contributions and each blocker with the phrase that fired
      (planned test_blocked_model_rows_name_their_blockers)
- [ ] `FEATURE_ORDER` holds neither blocker, and a model file with the old
      eight-feature order is refused as `model-invalid`
      (planned test_blockers_are_not_features)
- [ ] A labelled posting a blocker fires on is excluded from the export and
      counted as `blocked` (planned test_blocked_postings_are_excluded_from_labels)
- [ ] A model that beats the rules is activated whatever sign its remaining
      weights take, and the report has no blocker condition
      (planned test_train_ships_without_a_blocker_condition)
- [ ] The training minimum is ten positives per feature of the current
      order (planned test_the_minimum_follows_the_feature_count)
- [ ] Spec 077's tests that assumed blocker features are updated, not
      deleted: the blocker-weight refusal is replaced by the floor test above,
      and the US-only W-2 ranking test holds under the floor
- [ ] Spec 077 is amended to point here
- [ ] No real posting, company or tracker statistic appears in a fixture,
      the spec or a commit message (ADR-008)
- [ ] All gates green on PR

## Honest limitations

- **The floor is a rule, not learned.** That is the point: the data cannot
  teach it, and the candidate already decided that these postings are
  impossible.
- **A floored posting is never ranked by the model.** Its relative order
  among other blocked postings is whatever the model says, which matters to
  no one.
- **The minimum drops from 80 to 60 training positives** only because the
  feature count drops. The ten-per-feature rule itself is unchanged and is
  still a judgement.

## Out of scope

- Spec 078's tables and its penalty on the rule score.
- The region gate, spec 032.
- Retraining policy: models are still trained and activated by hand.

## Proof / origin

- Spec 077, Evaluation and the ship rule, condition 2; Features, the
  `us_scope` and `employment_blocker` rows.
- Spec 078, The penalty is derived, not chosen: the derivation this spec
  applies to the model's bounds.
- `services/api/src/harrier/scoring/train.py`: `BLOCKER_FEATURES` and the
  `blockers_negative` check this spec removes.
- Akin's request of 2026-10-06 after the trial: keep spec 078's penalty on
  top of the model's score and drop the blocker ship condition.
