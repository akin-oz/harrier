---
spec: 157
title: A hosted account trains its own scoring model from the browser
status: accepted
approved: yes
milestone: M10
depends: [077, 095, 106, 107]
---

# Spec 157: A hosted account trains its own scoring model from the browser

## Problem

Spec 077 gave harrier a learned fit score. It is trained from the owner's
own decisions, and it ranks postings better than the rules only once it beats
them on held-out rows. On the hosted deployment, which is where the
operator's real search now lives (spec 153, Problem), nothing can produce a
model:

1. **The export is refused.** `POST /ops/scoring/export` answers 501 on the
   hosted API with "scoring-export runs are not available on the hosted
   API: no hosted route serves their files (spec 107)"
   (`_no_hosted_reader` in `services/api/src/harrier_api/runs.py`). Spec 107
   refused it because the only reader of an export is training, and training
   did not run hosted.
2. **Training has no run.** `harrier scoring train` is a host-only command
   (`OnHost` in `services/api/src/harrier_api/settings_routes.py`), because
   it needs scikit-learn. scikit-learn is a dev dependency, and the image
   installs `--no-dev` (`services/api/pyproject.toml`, `Dockerfile`).

The rest of the path already works hosted. A hosted run writes and reads
`scoring/exports`, `scoring/models` and `scoring/reports` through its
binding to the owner's Storage prefix (spec 107; the listed families in
`services/api/src/harrier/filestore.py`). Scoring reads
`scoring/active-model.json` through the same seam (`load_active_model` in
`services/api/src/harrier/scoring/model.py`). So a hosted owner scores by
rules only, and the reason is only the two refusals above.

Who loses: the owner. Every decision they record on the hosted tracker is a
label the model could learn from, and none of it is used.

## Scope

1. A `training` dependency group holding scikit-learn, installed in the
   hosted API image.
2. A run kind `scoring-train`, started by `POST /ops/scoring/train`, which
   runs `harrier scoring train`, with `--activate` when asked.
3. The hosted API runs `scoring-export` and `scoring-train`.
4. The Operations page's Scoring section gets a "Train the model" button
   beside the export, and the Settings page lists `scoring train` as a
   routed command, not a host-only one.

## Behavior

### The image

- `services/api/pyproject.toml` gains a dependency group `training` that
  holds `scikit-learn`. The `dev` group includes `training`, so local
  development and CI are unchanged.
- The `Dockerfile` stages that build the hosted API image sync
  `--group training` as well.
- The import-linter contract "only the trainer imports scikit-learn or
  numpy" is unchanged: only `harrier.scoring.train` may import them. The API
  process never imports the trainer. A training run is a subprocess, as
  every run is (spec 006).

### Training as a run

`POST /ops/scoring/train` takes `{"activate": bool}`, defaulting to `false`.
It starts the run kind `scoring-train`, a `ParameterizedKind("scoring
train", switches={"--activate"}, takes_job=False)`, so the command is
`harrier scoring train` or `harrier scoring train --activate`. It answers as
`POST /ops/scoring/export` does: the run, or 409 while a run of the same kind
is going (`RUN_CONFLICT_RESPONSES`).

The run does what the command does today (spec 077):

- It reads the newest export.
- It writes a model under `scoring/models/` and a report under
  `scoring/reports/`.
- It activates the model (writes `scoring/active-model.json`) only when
  `--activate` was given and the model beat the rule score on held-out rows.
- Every message the command prints today is a line of the run's output:
  "activated model ...", "--activate refused; the rules keep scoring", and
  the rest.

The run ends with the command's own exit code (`EXIT_OK`, `EXIT_UNUSABLE`,
`EXIT_REFUSED` in `train.py`). A refusal reads as a failed run whose output
says why, and in every refusal the rules keep scoring and the active model,
if any, is unchanged. This spec does not change the codes.

On the hosted API, every file the run reads or writes is in the owner's
Storage prefix, through the run's binding (spec 107). One owner's run never
reads another owner's export or writes another owner's model (spec 106).

### Hosted runs

In `HOSTED_RUN_KINDS`, `scoring-export` becomes `RUNS`, and `scoring-train`
is added as `RUNS`. Neither needs a provider key. The export's reader is the
training run, so spec 107's reason for refusing the export no longer holds.

### The browser

- **Operations page.** `ScoringSection` (`apps/web/src/pages/operations/ScoringSection.tsx`)
  keeps "Write the feature export" and adds a button "Train the model". The
  button starts `POST /ops/scoring/train` with `activate: true`, and the
  section shows its output in a run stream, as the export does. The text
  that says training runs on the host is replaced: training uses the newest
  export, and activates the model only if it beats the rules.
- **Settings page.** `scoring train` moves from `OnHost` to
  `Routed("POST /ops/scoring/train")`. The model fact the host panel shows
  (`_active_model`, `GET /settings/host`) is unchanged and reads the owner's
  model.

Locally, the route and the run exist too (spec 095). Whether the local
container image installs `training` is open decision 1.

## Failure modes

| Case | Result |
|---|---|
| No export exists | "no export found; run `harrier scoring export` first"; exit 2; the run fails; the rules keep scoring |
| Too few labels to learn from | "insufficient labels: ..."; no model written; exit 3; the run fails |
| The model does not beat the rules | Model and report written, not activated; "refused: ..." and "--activate refused; the rules keep scoring"; the run ends with the command's code |
| A second train while one runs | 409, as every run kind |
| An image without the `training` group | The run fails with the import error, named in its output; nothing is written |
| The trainer exceeds the machine's memory | The kernel kills the subprocess; the run fails; the active model is unchanged, because it is written last |
| A Storage error writing the model | The run fails naming the family; no `active-model.json` is written |
| An owner with an active model trains again | The new model replaces it only when it passes the gate; otherwise the old one stays active |

## Acceptance criteria

- [x] `POST /ops/scoring/train` with `activate: true` starts a run whose
      command is `harrier scoring train --activate`, and without it, `harrier
      scoring train`
      (`services/api/tests/test_ui_operations.py::test_scoring_train_route_starts_the_run`).
- [x] On the hosted API, `scoring-export` and `scoring-train` start for an
      owner and are no longer answered 501
      (`services/api/tests/test_hosted_runs.py::test_hosted_scoring_runs_are_allowed`).
- [x] A hosted training run, with the Storage fake and a synthetic export
      in the owner's prefix, writes its model and report under that prefix,
      and with `--activate` and a passing model writes
      `scoring/active-model.json` there. A second owner's prefix is untouched
      (`services/api/tests/test_hosted_files.py::test_a_hosted_training_run_writes_only_its_owners_model`).
- [x] The hosted image installs scikit-learn. A test reads the `Dockerfile`
      and requires `--group training` on the hosted stages' `uv sync`
      lines. A test that reads source text is a last resort; it is used
      here because the image is not built in the test suite
      (`services/api/tests/test_deploy_files.py::test_the_hosted_image_installs_the_training_group`).
- [x] The import-linter contract still passes: no module but
      `harrier.scoring.train` imports scikit-learn or numpy
      (`uv run lint-imports` in `services/api`, unchanged contract in
      `services/api/pyproject.toml`).
- [x] The Scoring section shows "Train the model". Clicking it posts
      `{activate: true}` to `/ops/scoring/train` and streams the run
      (`apps/web/src/pages/operations/ScoringSection.test.tsx::trains the model and streams the run`).
- [x] The Settings command list shows `scoring train` as routed to
      `POST /ops/scoring/train`
      (`services/api/tests/test_ui_settings.py::test_scoring_train_is_a_routed_command`).
- [x] The contract is regenerated, and `just contract` shows no diff beyond
      the new route.
- [x] The pull request states the hosted image's size before and after,
      measured from the built images.

## Proof / origin

- Akin, 2026-10-17: scoring export "is an important feature of harrier",
  and training should run hosted, from the web app (chosen over training on
  the Mac).
- The refusal: spec 107, the run table ("`scoring-export` | 501") and Honest
  limitations ("Scoring models are not trained hosted").
- The trainer, its gate and its messages: spec 077,
  `services/api/src/harrier/scoring/train.py`.
- The hosted seam that already carries the scoring families: spec 107,
  `services/api/src/harrier/filestore.py`.

## Open decisions for Akin

1. **The local container image.** Recommended: install `training` there
   too, so "Train the model" works the same on both. The local image grows
   by the same amount. Alternative: hosted image only. Locally the button
   then fails with the import error, and `harrier scoring train` stays the
   host command.
2. **What the button activates.** Recommended: one button that trains with
   `--activate`, because the gate already refuses a model that does not beat
   the rules. Alternative: train without activating, and a second button
   that activates the newest passing model.
3. **One button or two.** Recommended: keep "Write the feature export" and
   "Train the model" separate, since the run table has one kind per command.
   Alternative: "Train the model" writes a fresh export first, in the same
   run.

## Decisions taken (2026-10-17)

Akin approved the spec with all three open decisions as recommended:

1. The local container image installs `training` too, so "Train the model"
   works locally and hosted alike, and `harrier scoring train` is no longer
   a host-only command.
2. One button trains with `--activate`; the gate decides activation.
3. "Write the feature export" and "Train the model" stay two buttons.

## Amendment (2026-10-17, implementation)

The build found five places the Behavior did not name. Each follows from
it; none changes what the owner sees beyond this spec.

1. **The CLI's run class.** A hosted run refuses a command outside
   `RUN_CLASS` (`services/api/src/harrier_cli/main.py`), and
   `services/api/tests/test_hosted_runs.py::test_every_run_kind_declares_its_hosted_answer`
   holds it equal to the verbs of the kinds that run hosted. So `scoring
   export` and `scoring train` join it.
2. **The export opens the run's store.** `harrier scoring export` opened
   the local file with `connect()`. A hosted run must read its owner's rows,
   so it opens `_open_store()`, as spec 107's third pull request made the
   other run verbs do. Locally that is the same file.
3. **The host panel's model row.** The panel lists, beside each fact, the
   host command that changes it (spec 096). Training is no longer a host
   command, so the `model` row lists none. The fact itself (`_active_model`,
   `GET /settings/host`) is read as before.
4. **The trainer's import error.** An environment without the `training`
   group printed "Train on the host, where the dev group is installed". It
   now names the group to install
   (`cd services/api && uv sync --group training`). The exit code is
   unchanged.
5. **A renamed test.** The Operations page test that said training stays
   on the host is now
   `apps/web/src/pages/operations/OperationsPage.test.tsx::the feature export runs here beside training`,
   and spec 095's citation of it follows the new name.

The local image's stages are `runtime` in the `Dockerfile`; the hosted
image's are `hosted`. The shared `base` stage's sync installs only `pdf`
for Chromium and is unchanged, because each image's own sync replaces its
groups.

## Out of scope

- Changing the model, its features, its gate or its export format (spec 077).
- Training on a schedule, or after every decision.
- A separate machine or worker for training: it runs as a subprocess on the
  API machine, as every hosted run does.
- Training across owners, or from another owner's labels.
- Showing the training report's metrics in the browser beyond the run
  output.

## Honest limitations

- Training runs on the API machine, beside the API. That machine is small
  (`deploy/fly.staging.toml`), and nothing here measures the trainer's
  memory on it. A run that exceeds it fails, and the rules keep scoring.
- The image grows by scikit-learn and its dependencies. The pull request
  measures how much; this spec does not guess.
- The trainer reads the default track's newest export
  (`latest_export_key`), as it does locally. An owner's other tracks are not
  trained separately.

## Migration

None for the owner. After the deploy, they write an export, then press
"Train the model". Until a model passes the gate, the rules keep scoring, as
they do today.
