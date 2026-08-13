#!/usr/bin/env bash
# PreToolUse guard on source-of-truth files. Pauses (ask), never blocks.
# Hand-wired: blueprint workspaces cannot declare hooks (gap logged in
# docs/aie-feedback.md). Modeled on Sorrel's guard-source-of-truth.sh.
set -u

INPUT=$(cat)
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // .tool_input.path // empty' 2>/dev/null)
[ -z "$FILE_PATH" ] && exit 0
REL_PATH="${FILE_PATH#"$PROJECT_DIR"/}"
# A leading ./ made every pattern below miss: "./config/data-classification.json"
# does not match "config/data-classification.json" (spec 045). Normalised here
# rather than doubled in each case arm, so a new arm cannot forget it.
while [ "${REL_PATH#./}" != "$REL_PATH" ]; do REL_PATH="${REL_PATH#./}"; done

ask() {
  jq -n --arg reason "$1
  -> $REL_PATH
Approve ONLY if an approved spec (specs/NNN-*.md, approved: yes) covers this change." '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: $reason
    }
  }'
  exit 0
}

case "$REL_PATH" in
  packages/contract/*)
    ask "This edits the API contract package. It is generated from FastAPI (ADR-005); hand-edits here are always wrong. Regenerate with 'just contract' instead." ;;
  services/api/src/harrier/tracker/schema*|services/api/src/harrier/tracker/migrations/*)
    ask "This edits the tracker schema or its migrations, the single source of truth for application state (ADR-003)." ;;
  .gitattributes|.gitignore|config/data-classification.json)
    ask "This edits the classification config. A wrong rule here can leak PII into a public repo (ADR-002/ADR-008)." ;;
  .github/workflows/*)
    ask "This edits CI, the authoritative enforcement layer (spec gate, contract drift, secret scan)." ;;
  CLAUDE.md|AGENTS.md|.ai/generated/*|.claude/agents/*|.claude/commands/*)
    ask "This is a generated governance file. Edit the source under .ai/ and run 'aie sync' instead. Hand-edits here will be flagged or overwritten." ;;
  .claude/settings.json|.claude/hooks/*)
    ask "This edits the hand-wired enforcement chain itself. Treat like an ADR: deliberate, human-approved." ;;
  # CI was guarded and the things CI runs were not, so the enforcement layer
  # could be changed without touching a guarded path (spec 045).
  scripts/*|justfile|lefthook.yml|lefthook.yaml|.pre-commit-config.yaml)
    ask "This edits a script or recipe the CI workflows invoke. Guarding the workflow and not what it runs leaves the gate changeable without review." ;;
  specs/*)
    ask "This edits a spec. Specs gate implementation; changing scope or approval is the human's call (.ai/rules/spec-approval.md)." ;;
esac

exit 0
