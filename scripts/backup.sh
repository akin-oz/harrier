#!/usr/bin/env bash
# Snapshot all local personal data to a timestamped archive OUTSIDE the repo
# (ADR-008: personal data has no home in git; backup is entirely local).
set -euo pipefail

cd "$(dirname "$0")/.."
DEST="${HARRIER_BACKUP_DIR:-$HOME/Backups/harrier}"
mkdir -p "$DEST"

targets=()
for d in data tracker runtime reports state secrets .env; do
  [ -e "$d" ] && targets+=("$d")
done

if [ "${#targets[@]}" -eq 0 ]; then
  echo "nothing to back up yet: no data directories exist (they arrive with spec 004)"
  exit 0
fi

archive="$DEST/harrier-data-$(date +%Y-%m-%d-%H%M).tar.gz"
tar -czf "$archive" \
  --exclude 'data/*.db-wal' --exclude 'data/*.db-shm' \
  "${targets[@]}"
echo "backed up: ${targets[*]}"
ls -lh "$archive"
