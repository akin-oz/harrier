# aie feedback log

Gaps found while adopting `@akinlabs/ai-engineering@0.2.0` in harrier. Dogfooding
rule: generated files are never hand-edited; what the compiler cannot express gets
wired directly (in files the compiler does not own) and logged here as candidate
issues for the package. Source references are to the compiler checkout at
`~/Documents/projects/ai-engineering-compiler`.

## Gaps hit in this repo

1. **Blueprint workspaces cannot declare hooks at all.** `blueprint.mjs` hard-codes
   `sources.hooks = []`, and blueprint/manifest are mutually exclusive, so choosing
   the `spec-driven` pack forfeits the entire hook vocabulary. Undocumented. Impact
   here: all three governance hooks are hand-wired in `.claude/settings.json` and
   `.claude/hooks/`. Suggested fix: allow a `hooks:` block in schema 2, or let packs
   contribute hooks.
2. **No turn-end/Stop hook event.** `HOOK_EVENTS` is closed at pre-edit, post-edit,
   session-start, session-end; Claude's `Stop` event (the heart of the Sorrel-style
   verification gate) is inexpressible, and the docs do not state the limitation.
   Impact: `.claude/hooks/verify-on-stop.sh` wired by hand. Suggested fix: add a
   `turn-end` normalized event mapping to Claude `Stop`, documented as unsupported
   on runtimes without an equivalent.
3. **Hook matchers are fixed per event.** `pre-edit` always compiles to matcher
   `Edit|Write|NotebookEdit`; a hook that needs `Bash` (the commit guard) cannot be
   declared even in manifest mode. Impact: commit guard hand-wired. Suggested fix:
   optional tool-matcher field on hook declarations.
4. **The pack's approval gating is prose only.** `development/spec-driven` ships
   rules and agents that say "get agreement, then implement" but no mechanism: no
   approval frontmatter convention, no commit-trailer convention, no CI resolver.
   Impact: the whole mechanical chain (approved: yes gate, `Spec: NNN` trailer,
   spec-gate CI job) is repo-local. Suggested fix: the pack could ship the trailer
   convention, a guard hook, and a reusable CI workflow or action input.
5. **No per-runtime settings surface beyond hooks.** The compiler merges only
   `hooks.*` into `.claude/settings.json`; permissions, env, teammate settings are
   hand-maintained. Acceptable here (the compiler provably preserves entries it does
   not own, which this repo relies on), but worth documenting as the intended
   division of ownership.
6. **`aie explain` marks templates "source only".** The pack's spec template is
   materialized to `.ai/generated/templates/spec.md` but compiled into no runtime;
   agents reference it by prose. Fine, but a `templates` runtime mapping (or an
   explicit "referenced by agents at this path" line in explain output) would make
   the contract legible.
7. **`blueprint.project.type` is validated then discarded.** `monorepo` is accepted
   (this repo sets it) but reaches no adapter and is not recorded in provenance,
   despite spec 010 claiming provenance recording. Minor: either use it or drop it.

## Non-gaps worth keeping

- Settings-merge behavior is exactly right for this setup: with zero declared hooks
  the compiler never touches `.claude/settings.json`, and ownership records mean a
  future `aie` version will not treat the hand-wired entries as its own.
- Local rules/agents/commands composing alongside the pack (with collision errors
  instead of silent override) worked first try; `aie sync`, `check`, and `explain`
  were green on first run over seven local rules, four agents, one command.
