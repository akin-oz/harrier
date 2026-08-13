#!/usr/bin/env bash
# Turn-end verification gate (Stop hook). Hand-wired: the aie compiler has no
# turn-end event (gap logged in docs/aie-feedback.md). Modeled on Sorrel's
# verify-on-stop.sh. Exit 2 keeps the turn open with stderr fed back to the model.
set -u

INPUT=$(cat)
STOP_ACTIVE=$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null || echo false)
if [ "$STOP_ACTIVE" = "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}" || exit 0

# Only gate when source actually changed vs HEAD.
#
# Untracked files count. `git diff --name-only HEAD` lists tracked changes only,
# so a turn that only ADDED files (a new module, a new test) reported nothing
# changed and ran no gate at all: the gate was blindest exactly when the most
# new code had arrived (spec 045).
CHANGED=$(
  {
    git diff --name-only HEAD 2>/dev/null
    git ls-files --others --exclude-standard 2>/dev/null
  } | grep -E '\.(py|ts|tsx|json|toml|yaml|yml|sh)$' | grep -vE '^\.ai/|^docs/|^specs/' || true
)
if [ -z "$CHANGED" ]; then
  exit 0
fi

# The gate is `just gate` (fast checks: pyright, tsc, unit tests). Until spec 001
# lands the toolchain, the recipe is absent and the gate passes vacuously.
if ! command -v just >/dev/null 2>&1; then
  echo "verify-on-stop: just not installed; gate skipped." >&2
  exit 0
fi
if ! just --summary 2>/dev/null | tr ' ' '\n' | grep -qx 'gate'; then
  echo "verify-on-stop: no 'gate' recipe yet; gate skipped." >&2
  exit 0
fi

LOG="${TMPDIR:-/tmp}/harrier-verify.log"
if ! just gate >"$LOG" 2>&1; then
  tail -n 60 "$LOG" >&2
  echo "" >&2
  echo "Turn-end gate failed. The turn is NOT complete until 'just gate' is clean." >&2
  exit 2
fi

echo "Verification gate passed: just gate green." >&2
exit 0
