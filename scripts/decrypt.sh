#!/usr/bin/env bash
# Decrypt private/*.enc.* into private/decrypted/ (gitignored). Requires the age
# key in the standard SOPS location. Demo mode never calls this (ADR-002).
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p private/decrypted

found=0
while IFS= read -r -d '' f; do
  found=1
  rel="${f#private/}"
  out="private/decrypted/${rel//.enc./.}"
  mkdir -p "$(dirname "$out")"
  case "$f" in
    *.enc.md) sops decrypt --input-type binary --output "$out" "$f" ;;
    *) sops decrypt --output "$out" "$f" ;;
  esac
  echo "decrypted: $f -> $out"
done < <(find private -name '*.enc.*' -not -path 'private/decrypted/*' -type f -print0)

if [ "$found" -eq 0 ]; then
  echo "no encrypted files under private/ yet"
fi
