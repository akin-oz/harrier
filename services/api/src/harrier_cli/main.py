"""Harrier CLI. Thin shell over the domain package; no logic lives here."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from harrier.db import connect, default_db_path
from harrier.profile import export_to, import_from, list_documents
from harrier.tracker.export import export_csv
from harrier.tracker.migrate_legacy import MigrationError, migrate
from harrier.tracker.store import TrackerError


def _cmd_migrate_legacy(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        report = migrate(
            conn,
            Path(args.jobs),
            Path(args.contacts) if args.contacts else None,
            replace=args.replace,
        )
    except MigrationError as error:
        print(f"migration aborted: {error}", file=sys.stderr)
        return 1
    print(report.summary())
    print(f"database: {default_db_path()}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    conn = connect()
    jobs_path, contacts_path = export_csv(conn, Path(args.dest))
    print(f"exported: {jobs_path}")
    print(f"exported: {contacts_path}")
    return 0


def _cmd_profile_import(args: argparse.Namespace) -> int:
    old_root = Path(args.from_root)
    if not old_root.is_dir():
        print(f"not a directory: {old_root}", file=sys.stderr)
        return 1
    conn = connect()
    imported, missing = import_from(conn, old_root)
    for line in imported:
        print(f"imported: {line}")
    for path in missing:
        print(f"missing (skipped): {path}")
    print(f"{len(imported)} documents imported into {default_db_path()}")
    return 0


def _cmd_profile_export(args: argparse.Namespace) -> int:
    conn = connect()
    written = export_to(conn, Path(args.to))
    for path in written:
        print(f"wrote: {path}")
    print(f"{len(written)} documents exported")
    return 0


def _cmd_profile_list(_args: argparse.Namespace) -> int:
    conn = connect()
    documents = list_documents(conn)
    for doc in documents:
        print(f"{doc['kind']}/{doc['name']} ({doc['format']}, updated {doc['updated_at']})")
    print(f"{len(documents)} documents")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harrier", description="Harrier CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_parser = sub.add_parser(
        "migrate-legacy", help="one-shot import from the old repo's tracker CSVs"
    )
    migrate_parser.add_argument("--jobs", required=True, help="path to legacy jobs.csv")
    migrate_parser.add_argument("--contacts", help="path to legacy contacts.csv")
    migrate_parser.add_argument(
        "--replace", action="store_true", help="drop and reimport tracker tables"
    )
    migrate_parser.set_defaults(func=_cmd_migrate_legacy)

    export_parser = sub.add_parser("export", help="export tracker to legacy-shape CSVs")
    export_parser.add_argument("--dest", default="tracker", help="destination directory")
    export_parser.set_defaults(func=_cmd_export)

    profile_parser = sub.add_parser("profile", help="profile document operations")
    profile_sub = profile_parser.add_subparsers(dest="profile_command", required=True)

    profile_import = profile_sub.add_parser("import", help="import from the old repo (read-only)")
    profile_import.add_argument("--from", dest="from_root", required=True, help="old repo root")
    profile_import.set_defaults(func=_cmd_profile_import)

    profile_export = profile_sub.add_parser("export", help="export documents to a directory")
    profile_export.add_argument("--to", required=True, help="destination directory")
    profile_export.set_defaults(func=_cmd_profile_export)

    profile_list = profile_sub.add_parser("list", help="list stored documents")
    profile_list.set_defaults(func=_cmd_profile_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
    except TrackerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
