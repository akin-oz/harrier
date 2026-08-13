#!/usr/bin/env bash
# PreToolUse guard on Bash: commit contract. Hard denies (exit 2).
# Modeled on Sorrel's guard-commit.sh; resolution to an approved spec is CI's job.
set -u

INPUT=$(cat)
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null)
[ -z "$CMD" ] && exit 0

deny() { printf '%s\n' "$1" >&2; exit 2; }

# Never stage or commit env files (except templates).
if printf '%s' "$CMD" | grep -qE '(^|[^[:alnum:]_])git[[:space:]]+(add|commit)'; then
  if printf '%s' "$CMD" | grep -oE '\.env[A-Za-z0-9_.-]*' \
    | grep -vE '^\.env\.(example|sample|template)$' | grep -q .; then
    deny "BLOCKED: refusing to stage/commit .env* files. Credentials never enter git (ADR-002)."
  fi
fi

# Checked BEFORE the "is this a commit" gate below, because that gate requires
# `git` and `commit` to be adjacent and these bypasses sit between them:
# `git -c core.hooksPath=... commit` reads as neither a commit nor a bypass.
# Matched as command tokens rather than anywhere in the string. The first
# version matched a bare substring, so it blocked any command whose text merely
# mentioned these, including the commit message describing this very guard
# (review of PR #50).
if printf '%s' "$CMD" | grep -qE '(^|[[:space:]])(-c[[:space:]]+core\.hooksPath|--git-dir|GIT_DIR)=?'; then
  deny "BLOCKED: redirecting hooksPath or the git dir disables the hook chain. The verification hooks ARE the definition of done."
fi

# Only inspect git commit commands from here on.
if ! printf '%s' "$CMD" | grep -qE '(^|[^[:alnum:]_])git[[:space:]]+commit'; then
  exit 0
fi

# No bypassing the hooks.
#
# The short form has to match a CLUSTER, not a lone -n. git accepts -nm "msg",
# and the previous pattern required whitespace or end-of-string straight after
# the n, so `git commit -nm "..."` skipped every hook and passed this guard
# (spec 045). The cluster pattern below requires a single leading dash, so the
# long options that legitimately contain an n (--no-edit, --amend) are not hit.
if printf '%s' "$CMD" | grep -qE -- '--no-verify|(^|[[:space:]])-[A-Za-z]*n[A-Za-z]*([[:space:]]|=|$)'; then
  deny "BLOCKED: 'git commit --no-verify' (or -n, including bundled forms like -nm) is not allowed. The verification hooks ARE the definition of done."
fi

# Amend without editing reuses an already-trailered message.
if printf '%s' "$CMD" | grep -qE -- '--amend[[:space:]]+--no-edit|--no-edit[[:space:]]+--amend|[[:space:]]-C[[:space:]]'; then
  exit 0
fi

# Every commit carries a Spec trailer. Shape only; CI resolves it.
if ! printf '%s' "$CMD" | grep -qE 'Spec:[[:space:]]*[0-9]{3}'; then
  deny "BLOCKED: commit is missing a 'Spec: NNN' trailer.

  git commit -m \"feat(tracker): add status transition tests\" -m \"Spec: 004\"

If no approved spec covers this work, write one first (use /spec) and wait for
approval. See .ai/rules/spec-approval.md."
fi

exit 0
