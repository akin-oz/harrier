"""Migration fidelity and export round-trip (spec 004). Fixtures are synthetic."""

import csv
from pathlib import Path

import pytest

from harrier.db import connect
from harrier.tracker import CONTACT_FIELDS, TRACKER_FIELDS, list_contacts, list_jobs
from harrier.tracker.export import export_csv
from harrier.tracker.migrate_legacy import MigrationError, migrate

SYNTHETIC_JOBS: list[dict[str, str]] = [
    {
        "company": "Acme",
        "title": "Senior Frontend Engineer",
        "location": "Remote, Europe",
        "url": "https://boards.example.com/acme/1",
        "source": "greenhouse",
        "added_at": "2026-05-01",
        "fit_score": "82",
        "status": "applied",
        "applied_date": "2026-05-03",
        "next_action": "follow up if no reply by 2026-05-10",
        "outreach_status": "sent",
        "notes": "score=82; archetype=product_engineer; external_key=gh:acme:1; signals=remote|vue",
    },
    {
        "company": "Beta GmbH",
        "title": "Staff Engineer, Design Systems",
        "location": "Remote (EMEA)",
        "url": "https://jobs.example.eu/beta/7",
        "source": "ashby",
        "added_at": "2026-06-12",
        "status": "prospect",
        # Multiline quoted field: the legacy CSVs contain these.
        "notes": 'score=61; source_label=Ashby Boards; line two says "quoted"\nand continues here',
    },
    {
        "company": "Gamma",
        "title": "Product Engineer",
        "location": "Anywhere (CET)",
        "url": "",
        "source": "manual",
        "added_at": "2026-07-30",
        "status": "weird_legacy_status",
        "notes": "manual_added=2026-07-30",
    },
]

SYNTHETIC_CONTACTS: list[dict[str, str]] = [
    {
        "company": "Acme",
        "applied_job_title": "Senior Frontend Engineer",
        "job_url": "https://boards.example.com/acme/1",
        "person_name": "Sam Sample",
        "person_title": "Talent Partner",
        "relevance": "recruiter",
        "fit_score": "74",
        "contact_status": "new",
    },
]


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


@pytest.fixture()
def csv_pair(tmp_path: Path) -> tuple[Path, Path]:
    jobs_csv = tmp_path / "jobs.csv"
    contacts_csv = tmp_path / "contacts.csv"
    _write_csv(jobs_csv, TRACKER_FIELDS, SYNTHETIC_JOBS)
    _write_csv(contacts_csv, CONTACT_FIELDS, SYNTHETIC_CONTACTS)
    return jobs_csv, contacts_csv


def test_migration_counts_and_field_fidelity(tmp_path: Path, csv_pair: tuple[Path, Path]) -> None:
    jobs_csv, contacts_csv = csv_pair
    conn = connect(tmp_path / "t.db")
    report = migrate(conn, jobs_csv, contacts_csv)

    assert report.jobs_read == len(SYNTHETIC_JOBS)
    assert report.jobs_imported == len(SYNTHETIC_JOBS)
    assert report.contacts_imported == len(SYNTHETIC_CONTACTS)

    jobs = list_jobs(conn)
    acme = jobs[0]
    for name in TRACKER_FIELDS:
        expected = SYNTHETIC_JOBS[0].get(name, "")
        assert acme[name] == expected, f"field {name} lost fidelity"
    assert acme["score"] == "82"
    assert acme["external_key"] == "gh:acme:1"
    assert acme["signals"] == "remote|vue"

    beta = jobs[1]
    assert "and continues here" in beta["notes"]  # multiline survived
    assert beta["source_label"] == "Ashby Boards"

    contact = list_contacts(conn)[0]
    assert contact["person_name"] == "Sam Sample"


def test_unknown_status_preserved_not_invented(tmp_path: Path, csv_pair: tuple[Path, Path]) -> None:
    jobs_csv, contacts_csv = csv_pair
    conn = connect(tmp_path / "t.db")
    report = migrate(conn, jobs_csv, contacts_csv)
    gamma = list_jobs(conn)[2]
    assert gamma["status"] == "prospect"
    assert "legacy_status=weird_legacy_status" in gamma["notes"]
    assert report.unknown_statuses == {"weird_legacy_status": 1}


def test_duplicate_url_aborts_importing_nothing(tmp_path: Path) -> None:
    rows = [dict(SYNTHETIC_JOBS[0]), dict(SYNTHETIC_JOBS[0])]
    rows[1]["company"] = "Acme Clone"
    jobs_csv = tmp_path / "jobs.csv"
    _write_csv(jobs_csv, TRACKER_FIELDS, rows)
    conn = connect(tmp_path / "t.db")
    with pytest.raises(MigrationError, match="url appears 2 times"):
        migrate(conn, jobs_csv, None)
    assert list_jobs(conn) == []


def test_second_migration_requires_replace(tmp_path: Path, csv_pair: tuple[Path, Path]) -> None:
    jobs_csv, contacts_csv = csv_pair
    conn = connect(tmp_path / "t.db")
    migrate(conn, jobs_csv, contacts_csv)
    with pytest.raises(MigrationError, match="--replace"):
        migrate(conn, jobs_csv, contacts_csv)
    report = migrate(conn, jobs_csv, contacts_csv, replace=True)
    assert report.jobs_imported == len(SYNTHETIC_JOBS)
    assert len(list_jobs(conn)) == len(SYNTHETIC_JOBS)


def test_export_reimport_round_trip(tmp_path: Path, csv_pair: tuple[Path, Path]) -> None:
    jobs_csv, contacts_csv = csv_pair
    conn = connect(tmp_path / "a.db")
    migrate(conn, jobs_csv, contacts_csv)
    first = list_jobs(conn)

    out_dir = tmp_path / "export"
    exported_jobs, exported_contacts = export_csv(conn, out_dir)

    conn2 = connect(tmp_path / "b.db")
    migrate(conn2, exported_jobs, exported_contacts)
    second = list_jobs(conn2)

    for row_a, row_b in zip(first, second, strict=True):
        for name in TRACKER_FIELDS:
            assert row_a[name] == row_b[name]
    assert [c["person_name"] for c in list_contacts(conn2)] == ["Sam Sample"]
