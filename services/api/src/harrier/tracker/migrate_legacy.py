"""One-shot migration from the old repo's CSVs.

Fidelity rules (spec 004): import every row verbatim, expand notes key=value
pairs into promoted columns while preserving the original notes text, never
drop or merge a row. Duplicate url or external_key in the source aborts before
anything is written.
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from harrier.tracker.schema import CONTACT_FIELDS, NOTE_KEYS, STATUSES, TRACKER_FIELDS
from harrier.tracker.store import TrackerError, expand_notes


class MigrationError(TrackerError):
    pass


@dataclass
class MigrationReport:
    jobs_read: int = 0
    jobs_imported: int = 0
    contacts_read: int = 0
    contacts_imported: int = 0
    notes_keys_expanded: dict[str, int] = field(default_factory=dict[str, int])
    missing_columns_filled: int = 0
    unknown_statuses: dict[str, int] = field(default_factory=dict[str, int])

    def summary(self) -> str:
        lines = [
            f"jobs: {self.jobs_imported}/{self.jobs_read} imported",
            f"contacts: {self.contacts_imported}/{self.contacts_read} imported",
        ]
        if self.notes_keys_expanded:
            expanded = ", ".join(f"{k}={v}" for k, v in sorted(self.notes_keys_expanded.items()))
            lines.append(f"notes keys expanded: {expanded}")
        if self.missing_columns_filled:
            lines.append(
                f"rows with missing columns backfilled empty: {self.missing_columns_filled}"
            )
        if self.unknown_statuses:
            statuses = ", ".join(f"{k}={v}" for k, v in sorted(self.unknown_statuses.items()))
            lines.append(f"unknown statuses preserved via notes marker: {statuses}")
        return "\n".join(lines)


def _read_csv(path: Path, fields: tuple[str, ...]) -> tuple[list[dict[str, str]], int]:
    rows: list[dict[str, str]] = []
    backfilled = 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row: dict[str, str] = {}
            missing = False
            for name in fields:
                value = raw.get(name)
                if value is None:
                    missing = True
                    value = ""
                row[name] = value
            if missing:
                backfilled += 1
            rows.append(row)
    return rows, backfilled


def scan_job_duplicates(rows: list[dict[str, str]]) -> list[str]:
    """Return human-readable duplicate descriptions for url and external_key."""
    findings: list[str] = []
    url_counts = Counter(row["url"].strip() for row in rows if row["url"].strip())
    for url, count in url_counts.items():
        if count > 1:
            findings.append(f"url appears {count} times: {url}")
    key_counts = Counter(
        expand_notes(row["notes"])["external_key"]
        for row in rows
        if expand_notes(row["notes"])["external_key"]
    )
    for key, count in key_counts.items():
        if count > 1:
            findings.append(f"external_key appears {count} times: {key}")
    return findings


def migrate(
    conn: sqlite3.Connection,
    jobs_csv: Path,
    contacts_csv: Path | None,
    *,
    replace: bool = False,
) -> MigrationReport:
    existing = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()
    if int(existing["c"]) > 0:
        if not replace:
            raise MigrationError(
                f"jobs table already has {existing['c']} rows; pass --replace to reimport"
            )
        with conn:
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM contacts")

    report = MigrationReport()
    job_rows, jobs_backfilled = _read_csv(jobs_csv, TRACKER_FIELDS)
    report.jobs_read = len(job_rows)
    report.missing_columns_filled += jobs_backfilled

    duplicates = scan_job_duplicates(job_rows)
    if duplicates:
        listing = "\n  ".join(duplicates)
        raise MigrationError(
            "duplicate identities in source; resolve in the CSV, nothing was imported:\n  "
            + listing
        )

    expanded_counts: Counter[str] = Counter()
    status_fallbacks: Counter[str] = Counter()
    insert_columns = [*TRACKER_FIELDS, *NOTE_KEYS]
    placeholders = ", ".join("?" for _ in insert_columns)
    with conn:
        for row in job_rows:
            promoted = expand_notes(row["notes"])
            for key, value in promoted.items():
                if value:
                    expanded_counts[key] += 1
            status = row["status"].strip()
            if status not in STATUSES:
                # Preserve the original value in notes rather than inventing data.
                status_fallbacks[status or "(blank)"] += 1
                row = dict(row)
                marker = f"legacy_status={status}"
                row["notes"] = f"{row['notes']}; {marker}" if row["notes"] else marker
                row["status"] = "prospect"
            values = [row[name] for name in TRACKER_FIELDS]
            values += [promoted[key] for key in NOTE_KEYS]
            conn.execute(
                f"INSERT INTO jobs ({', '.join(insert_columns)}) VALUES ({placeholders})",
                values,
            )
            report.jobs_imported += 1

    report.notes_keys_expanded = dict(expanded_counts)
    report.unknown_statuses = dict(status_fallbacks)

    if contacts_csv is not None:
        contact_rows, contacts_backfilled = _read_csv(contacts_csv, CONTACT_FIELDS)
        report.contacts_read = len(contact_rows)
        report.missing_columns_filled += contacts_backfilled
        contact_placeholders = ", ".join("?" for _ in CONTACT_FIELDS)
        with conn:
            for row in contact_rows:
                conn.execute(
                    f"INSERT INTO contacts ({', '.join(CONTACT_FIELDS)}) "
                    f"VALUES ({contact_placeholders})",
                    [row[name] for name in CONTACT_FIELDS],
                )
                report.contacts_imported += 1

    return report
