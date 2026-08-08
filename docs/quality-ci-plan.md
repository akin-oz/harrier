# Quality and CI plan

The enforcement philosophy, borrowed from Sorrel: do not prevent the model from being
wrong; make wrong un-mergeable. Local gates give fast feedback; CI is authoritative.
Spec 002 implements the workflows; `just` recipes are shared by both so they cannot
drift (ADR-007).

## Gate list

| Gate | What | Local | PR | main |
|---|---|---|---|---|
| Python type-check | `uv run pyright` (strict) | turn-end + `just check` | yes | yes |
| TS type-check | `tsc --noEmit` (strict) | turn-end + `just check` | yes | yes |
| Python lint/format | `ruff check` + `ruff format --check` | pre-commit (staged) + `just check` | yes | yes |
| TS lint/format | ESLint (incl. FSD boundaries) + Prettier | pre-commit (staged) + `just check` | yes | yes |
| Python unit tests | pytest (incl. ported behavior pins, encryption coverage test) | turn-end + `just check` | yes | yes |
| TS unit tests | vitest | turn-end + `just check` | yes | yes |
| Import direction | import-linter (`harrier` never imports `harrier_api`; sources never import screening/tracker) | `just check` | yes | yes |
| Contract codegen drift | regenerate `packages/contract`, `git diff --exit-code` | `just contract` | yes | yes |
| Governance drift | `aie check` via `akin-oz/ai-engineering@v0` | `aie check` | yes | yes |
| Spec trailer gate | every commit's `Spec: NNN` resolves to a spec file with `approved: yes` | commit guard (shape only) + lefthook commit-msg | yes (authoritative) | merge-protected |
| Secret scan | gitleaks with project rules | pre-commit (staged) | yes (full diff) | yes (full history, scheduled weekly) |

## PR versus main

- **PR**: everything above. The spec-trailer job iterates `BASE..HEAD` commits,
  extracts the first `Spec: NNN` from each body, globs `specs/NNN-*.md`, and greps
  line-anchored `approved: yes` (mechanics: Sorrel's `spec-gate.yml`). Merge commits
  exempt. Failure annotates the offending commit.
- **main**: the same suite re-runs on push (protection against direct pushes and
  merge skew), plus the weekly full-history gitleaks scan.
- Branch protection: PRs only, required checks all of the above, no force pushes.

## Local layers (fast to slow)

1. **Pre-commit (lefthook)**: staged-file lint/format, gitleaks, trailer shape.
   Sub-second to seconds; covers plain terminal commits.
2. **Turn-end (`.claude/hooks/verify-on-stop.sh` running `just gate`)**: both
   type-checks plus unit tests, only when source changed vs HEAD; exit 2 holds the
   agent's turn open until green.
3. **`just check`**: the full CI-equivalent suite on demand.

## Test strategy notes

- Ported behavior pins are the parity backbone: the old repo's 22 test files map to
  pytest suites per spec (each spec stub names its pins).
- Known coverage gaps in the old repo become new tests, not ported gaps:
  `evaluate_offer`, the auto-reject path, `send_daily_digest`, the RemoteOK importer.
- The walking skeleton (specs 004 to 006) must cross every gate once before feature
  work: the first PR that exercises contract drift, the SSE protocol, and the
  migration assertions proves the harness end to end.

## Review layer (agents, on demand)

`/spec-review` runs the four read-only reviewers over the branch diff:
contract-guardian, data-integrity-reviewer, privacy-reviewer (never skipped),
fsd-reviewer. Cut from the candidate list: a separate pipeline reviewer (merged into
data-integrity-reviewer: sources, screening, and tracker are one shared path and the
overlap would produce duplicate findings) and a standalone QA engineer (test presence
is enforced mechanically by the gates; test quality review happens in spec review
against each spec's acceptance criteria rather than as a standing agent).
