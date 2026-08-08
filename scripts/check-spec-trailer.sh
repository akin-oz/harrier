#!/usr/bin/env bash
# Commit-msg hook: every commit carries a Spec: NNN trailer (shape check only;
# CI resolves the number to an approved spec). Covers plain terminal commits;
# .claude/hooks/guard-commit.sh covers agent commits pre-execution.
set -u

MSG_FILE="${1:?usage: check-spec-trailer.sh <commit-msg-file>}"

# Merge and fixup commits are exempt (CI skips merges too).
FIRST_LINE=$(head -n 1 "$MSG_FILE")
case "$FIRST_LINE" in
  Merge\ *|fixup!\ *|squash!\ *) exit 0 ;;
esac

if grep -qE 'Spec:[[:space:]]*[0-9]{3}' "$MSG_FILE"; then
  exit 0
fi

cat >&2 <<'EOF'
Commit rejected: missing a 'Spec: NNN' trailer.

  git commit -m "feat(tracker): add transition tests" -m "Spec: 004"

Every commit must reference a spec in specs/. If none covers this work,
write one first (see specs/README.md).
EOF
exit 1
