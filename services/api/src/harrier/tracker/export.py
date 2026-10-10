"""CSV export in the exact legacy shapes (ADR-003: grep and diff survive).

Rows whose source status was not a legal lifecycle value were imported as
prospect with a legacy_status marker in notes (migrate_legacy). Export
restores the original status and strips the marker, so an exported CSV
matches its source and survives a reimport round-trip.

The writers take an open text handle, so `harrier export` writes files and
the browser's downloads (spec 096) write a response from the same code, in
the same columns. Only the downloads neutralize formula cells: the command's
files are read back by the legacy import, and an apostrophe would change the
text it imports.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path
from typing import TextIO

from harrier.tracker.schema import CONTACT_FIELDS, TRACKER_FIELDS
from harrier.tracker.store import extract_note_value, list_contacts, list_jobs
from harrier.tracks import DEFAULT_TRACK_ID, Scope

# A spreadsheet reads a cell beginning with one of these as a formula.
FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")
# A cell that is a number, such as a fit score of -5, is data, not a formula,
# and keeps its sign.
_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?")


def neutralize_cell(value: str) -> str:
    """The value a spreadsheet will show as text (spec 096).

    Company names and titles arrive from job boards, so a value can begin
    with a formula character. A leading apostrophe makes a spreadsheet show
    it as text. A number is written as it is.
    """
    if value.startswith(FORMULA_LEADS) and not _NUMBER.fullmatch(value):
        return f"'{value}"
    return value


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


def _cells(row: dict[str, str], fields: tuple[str, ...], *, neutralize: bool) -> dict[str, str]:
    if not neutralize:
        return {name: row[name] for name in fields}
    return {name: neutralize_cell(row[name]) for name in fields}


def write_jobs(
    handle: TextIO, conn: sqlite3.Connection, scope: Scope, *, neutralize: bool = False
) -> None:
    """The track's jobs in the legacy 20-column shape, header first."""
    writer = csv.DictWriter(handle, fieldnames=list(TRACKER_FIELDS))
    writer.writeheader()
    for row in list_jobs(conn, scope):
        writer.writerow(_cells(_legacy_faithful(row), TRACKER_FIELDS, neutralize=neutralize))


def write_contacts(handle: TextIO, conn: sqlite3.Connection, *, neutralize: bool = False) -> None:
    """The person's contacts in the legacy 17-column shape, header first."""
    writer = csv.DictWriter(handle, fieldnames=list(CONTACT_FIELDS))
    writer.writeheader()
    for row in list_contacts(conn):
        writer.writerow(_cells(row, CONTACT_FIELDS, neutralize=neutralize))


def export_csv(conn: sqlite3.Connection, scope: Scope, dest_dir: Path) -> tuple[Path, Path | None]:
    """The legacy-shape CSVs. The default track writes `jobs.csv` and
    `contacts.csv` into `dest_dir`, as it always has. Any other track writes
    only `<dest_dir>/<slug>/jobs.csv`: contacts are the person's, not a
    track's (spec 093)."""
    if scope.track.id != DEFAULT_TRACK_ID:
        track_dir = dest_dir / scope.track.slug
        track_dir.mkdir(parents=True, exist_ok=True)
        jobs_path = track_dir / "jobs.csv"
        with jobs_path.open("w", newline="", encoding="utf-8") as handle:
            write_jobs(handle, conn, scope)
        return jobs_path, None
    dest_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = dest_dir / "jobs.csv"
    contacts_path = dest_dir / "contacts.csv"
    with jobs_path.open("w", newline="", encoding="utf-8") as handle:
        write_jobs(handle, conn, scope)
    with contacts_path.open("w", newline="", encoding="utf-8") as handle:
        write_contacts(handle, conn)
    return jobs_path, contacts_path
