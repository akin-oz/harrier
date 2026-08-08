"""CSV export in the exact legacy shapes (ADR-003: grep and diff survive).

Rows whose source status was not a legal lifecycle value were imported as
prospect with a legacy_status marker in notes (migrate_legacy). Export
restores the original status and strips the marker, so an exported CSV
matches its source and survives a reimport round-trip.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from harrier.tracker.schema import CONTACT_FIELDS, TRACKER_FIELDS
from harrier.tracker.store import extract_note_value, list_contacts, list_jobs


def _strip_note_key(notes: str, key: str) -> str:
    stripped = re.sub(rf"(^|;\s*){re.escape(key)}=[^;]*", "", notes or "")
    return stripped.strip("; ").strip()


def _legacy_faithful(row: dict[str, str]) -> dict[str, str]:
    legacy_status = extract_note_value(row["notes"], "legacy_status")
    if not legacy_status:
        return row
    faithful = dict(row)
    faithful["status"] = legacy_status
    faithful["notes"] = _strip_note_key(row["notes"], "legacy_status")
    return faithful


def export_csv(conn: sqlite3.Connection, dest_dir: Path) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = dest_dir / "jobs.csv"
    contacts_path = dest_dir / "contacts.csv"

    with jobs_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TRACKER_FIELDS))
        writer.writeheader()
        for row in list_jobs(conn):
            faithful = _legacy_faithful(row)
            writer.writerow({name: faithful[name] for name in TRACKER_FIELDS})

    with contacts_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CONTACT_FIELDS))
        writer.writeheader()
        for row in list_contacts(conn):
            writer.writerow({name: row[name] for name in CONTACT_FIELDS})

    return jobs_path, contacts_path
