"""Deterministic OpenAPI export: the contract artifact (ADR-005).

Usage: python -m harrier_api.export_openapi <output-path>
Writes sorted-key JSON with a trailing newline so the file is byte-stable
across runs and the CI drift job can diff it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from harrier_api.app import create_app


def export(output: Path) -> None:
    schema = create_app().openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m harrier_api.export_openapi <output-path>", file=sys.stderr)
        return 2
    export(Path(argv[0]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
