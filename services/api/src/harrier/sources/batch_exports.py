"""Wellfound and WTTJ batch export importers (spec 010 port). Ingestion only.

Manual exports (JSON or CSV) from sources without free APIs. Both share the
reading and key-picking helpers; only the key lists differ.

Supported shapes: a CSV with a header row, a JSON list of objects, or a JSON
object carrying the list under "items" or "results". Anything else raises:
a wrong-shaped export must fail loudly, never report a successful zero-row
import (deliberate change from the old code, which returned []). Proof:
tests/test_batch_and_capture.py (test_read_export_rows_csv_json_and_container,
test_read_export_rows_rejects_bad_shapes).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, cast

from harrier.screening.normalized import NormalizedJob, make_normalized_job

WELLFOUND_SOURCE = "wellfound"
WTTJ_SOURCE = "wttj"


def read_export_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            return [cast("dict[str, Any]", dict(row)) for row in csv.DictReader(handle)]
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        typed = cast("dict[str, Any]", payload)
        if "items" in typed:
            payload = cast("object", typed["items"])
        elif "results" in typed:
            payload = cast("object", typed["results"])
        else:
            raise RuntimeError(f"unsupported export payload (no items/results container): {path}")
    if not isinstance(payload, list):
        raise RuntimeError(f"unsupported export payload: {path}")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(cast("list[object]", payload)):
        if not isinstance(item, dict):
            raise RuntimeError(f"export row {index} is not an object: {path}")
        rows.append(cast("dict[str, Any]", item))
    return rows


def pick(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_wellfound_row(row: dict[str, Any]) -> NormalizedJob:
    return make_normalized_job(
        source=WELLFOUND_SOURCE,
        company=pick(row, "company", "company_name", "startup", "startup_name"),
        title=pick(row, "title", "role", "job_title", "position"),
        location=pick(row, "location", "locations", "job_location"),
        url=pick(row, "url", "job_url", "link", "apply_url"),
        description=pick(row, "description", "job_description", "summary"),
        created_at=pick(row, "created_at", "posted_at", "published_at", "date_posted"),
        external_id=pick(row, "id", "job_id", "external_id"),
        raw_compensation=pick(row, "salary", "compensation", "salary_range"),
        board_key="wellfound_export",
        metadata={"import_kind": "batch_export"},
        raw_payload=row,
    )


def normalize_wttj_row(row: dict[str, Any]) -> NormalizedJob:
    return make_normalized_job(
        source=WTTJ_SOURCE,
        company=pick(row, "company", "company_name", "organization"),
        title=pick(row, "title", "job_title", "name"),
        location=pick(row, "location", "city", "locations"),
        url=pick(row, "url", "job_url", "link", "offer_url"),
        description=pick(row, "description", "contents", "summary"),
        created_at=pick(row, "created_at", "published_at", "publication_date"),
        external_id=pick(row, "id", "job_id", "slug"),
        raw_compensation=pick(row, "salary", "compensation", "salary_range"),
        board_key="wttj_export",
        metadata={"import_kind": "batch_export"},
        raw_payload=row,
    )


def load_wellfound_exports(paths: list[str]) -> list[NormalizedJob]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        rows.extend(read_export_rows(Path(raw_path).expanduser()))
    return [normalize_wellfound_row(row) for row in rows]


def load_wttj_exports(paths: list[str]) -> list[NormalizedJob]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        rows.extend(read_export_rows(Path(raw_path).expanduser()))
    return [normalize_wttj_row(row) for row in rows]
