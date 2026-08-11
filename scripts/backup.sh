#!/usr/bin/env bash
# Verified snapshot of all local personal data, outside the repo
# (ADR-008: personal data has no home in git; backup is entirely local).
#
# This is a thin wrapper. The work is in `harrier backup` (spec 030), because
# the previous version of this script was the defect: it ran tar over a live
# WAL-mode database with the write-ahead log excluded, so every transaction
# since the last checkpoint was missing from an archive that still exited 0.
# It also resolved the data directory by its own rule while the application
# honours HARRIER_DATA_DIR, so with the override set it archived nothing and
# reported success.
set -euo pipefail

cd "$(dirname "$0")/.."
exec uv run --project services/api harrier backup "$@"
