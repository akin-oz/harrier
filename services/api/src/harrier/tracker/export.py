"""CSV export in the exact legacy shapes (ADR-003: grep and diff survive)."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from harrier.tracker.schema import CONTACT_FIELDS, TRACKER_FIELDS
from harrier.tracker.store import list_contacts, list_jobs


def export_csv(conn: sqlite3.Connection, dest_dir: Path) -> tuple[Path, Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = dest_dir / "jobs.csv"
    contacts_path = dest_dir / "contacts.csv"

    with jobs_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(TRACKER_FIELDS))
        writer.writeheader()
        for row in list_jobs(conn):
            writer.writerow({name: row[name] for name in TRACKER_FIELDS})

    with contacts_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CONTACT_FIELDS))
        writer.writeheader()
        for row in list_contacts(conn):
            writer.writerow({name: row[name] for name in CONTACT_FIELDS})

    return jobs_path, contacts_path
