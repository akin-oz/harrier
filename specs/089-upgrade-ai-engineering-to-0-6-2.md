---
spec: 089
title: Upgrade @akinlabs/ai-engineering from 0.2.0 to 0.6.2 without changing what it enforces
status: accepted
approved: yes
milestone: M8
depends: [001, 002, 045]
---

# Spec 089: Upgrade @akinlabs/ai-engineering from 0.2.0 to 0.6.2 without changing what it enforces

## Problem

Harrier compiles its governance layer with `@akinlabs/ai-engineering@0.2.0`
(`package.json`, pinned in CI as `akin-oz/ai-engineering@v0.2.0`). The
package is at 0.6.2. Between the two, the `spec-driven` pack went from
version 1 to version 3 and started contributing three things this repo
already has, in its own form:

| Pack contribution (0.3.0 and 0.4.0) | What harrier already does |
| --- | --- |
| `hook.spec-trailer`: a PreToolUse hook on Bash refusing a commit without a `Spec:` trailer | `.claude/hooks/guard-commit.sh` (spec 045), on the same event and tool |
| `rule.spec-trailer`: a rule saying `Spec: none` (followed by a reason) is a valid trailer | `.github/workflows/spec-gate.yml` resolves every trailer to an approved spec, so `Spec: none` fails CI. The rule text also carries an em dash, which the writing-style rule forbids in `CLAUDE.md` |
| `permission.protect-guardrails`: `Edit` deny rules on `.claude/settings.json`, `.claude/settings.local.json`, `.claude/hooks/**`, `.ai/generated/**` | `.claude/hooks/guard-source-of-truth.sh` pauses for human review on the same paths. A deny turns "ask" into "never through the edit tools" |

A plain `npx aie sync` on 0.6.2 therefore installs a second commit guard beside
spec 045's, writes a rule into `CLAUDE.md` that contradicts the spec gate, and
rewrites `.claude/settings.json`. Verified with the 0.6.2 checkout at
`~/Documents/projects/ai-engineering-compiler`: `aie sync --dry-run` lists
`.claude/hooks/spec-trailer.sh (would create)` and
`.claude/settings.json (would update)`.

Separately, `docs/aie-feedback.md` still describes gaps 1, 2, 3 and 8 as open
(no hooks in blueprints, no turn-end event, fixed hook matchers, no floating
`v0` tag). The 0.3.0 changelog closes all four.

## Scope

After this change:

- `package.json` declares `"@akinlabs/ai-engineering": "^0.6.2"` and
  `pnpm-lock.yaml` resolves it to 0.6.2.
- `.ai/blueprint.yaml` carries
  `workflow.disable: [hook.spec-trailer, rule.spec-trailer, permission.protect-guardrails]`,
  each with a one-line comment naming the harrier mechanism that replaces it.
  `aie sync` then leaves `.claude/settings.json` and `.claude/hooks/` untouched
  and adds no rule to `CLAUDE.md` or `AGENTS.md`. The only generated change is
  the pack banner, `development/spec-driven@1` becoming `@3`, in
  `CLAUDE.md`, `AGENTS.md`, `.ai/generated/**`, and `.claude/agents/spec-author.md`,
  `.claude/agents/implementer.md`, `.claude/commands/spec.md`. Verified with
  `aie sync --dry-run` on 0.6.2 against that blueprint: no `missing` entries,
  no settings drift, no warnings.
- `.github/workflows/ci.yml` pins `akin-oz/ai-engineering@v0.6.2`. The exact
  pin stays deliberate (the comment says so instead of claiming no `v0` tag
  exists). The action's new `audit` input keeps its default, `warn`: findings
  annotate the job and do not fail it.
- `docs/aie-feedback.md` gains a short status line under each of gaps 1, 2, 3
  and 8 naming the release that closed it, and its heading sentence names
  0.6.2 as the version in use. Nothing else in the log changes.
- `npx aie check` is clean after `npx aie sync`, and `just check` is green.

Failure modes:

- A `disable` entry the pack does not contribute is a compile error naming
  the available ids, so a typo cannot pass as a disabled contribution.
- `aie sync` run with the pack contributions enabled would modify
  `.claude/settings.json`; the `disable` list is what prevents it, and
  `aie check` in CI reports drift if the committed output and the blueprint
  disagree.

## Acceptance criteria

- [ ] `package.json` and `pnpm-lock.yaml` resolve `@akinlabs/ai-engineering` to 0.6.2
- [ ] `.ai/blueprint.yaml` disables `hook.spec-trailer`, `rule.spec-trailer`, `permission.protect-guardrails`, with a comment per entry
- [ ] `git diff` of the synced output touches only generated banners; `.claude/settings.json` and `.claude/hooks/` are unchanged
- [ ] `CLAUDE.md` and `AGENTS.md` contain no `## Rule: spec-trailer` section
- [ ] `.github/workflows/ci.yml` uses `akin-oz/ai-engineering@v0.6.2` and the stale comment about the `v0` tag is gone
- [ ] `docs/aie-feedback.md` marks gaps 1, 2, 3 and 8 as closed, naming 0.3.0
- [ ] `npx aie check` exits 0 and `just check` is green

## Proof / origin

- `~/Documents/projects/ai-engineering-compiler/CHANGELOG.md` entries 0.3.0,
  0.3.1, 0.4.0 and `docs/upgrading.md` in the same checkout: what each
  release contributes and how `workflow.disable` turns one contribution off.
- `~/Documents/projects/ai-engineering-compiler/packs/development/spec-driven/pack.yaml`:
  the three contributions by id.
- Spec 045: the commit guard harrier keeps.
- Spec 002: the `aie check` CI job and the spec gate that rejects a trailer
  not resolving to an approved spec.
- `.ai/rules/source-of-truth.md`: the "pause for review" contract the
  `protect-guardrails` deny would replace.

## Out of scope

- Adopting `security: hardened`, `permission.protect-guardrails`, or any fix
  `aie audit` proposes (`deny-empty`, `sandbox-disabled`,
  `no-verify-unblocked`, `secret-readable`). Each changes what the agent may
  do and gets its own spec. Note for that spec: `aie audit` flags
  `services/api/.venv/lib/python3.12/site-packages/certifi/cacert.pem` as a
  secret, which is a CA bundle; a `*.pem` deny would cover it anyway.
- Replacing `guard-commit.sh` with the pack's hook, or the reverse.
- Changing the CI action's `audit` input to `fail`.
- Updating the stale sentence in `.claude/hooks/verify-on-stop.sh` that says
  the compiler has no turn-end event. It is a comment in a guarded hook file.
- Declaring harrier's three hand-wired hooks in the blueprint now that
  blueprints accept `hooks:`. Possible later, as its own spec, since it moves
  ownership of `.claude/settings.json` entries to the compiler.
