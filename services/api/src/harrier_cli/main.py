"""Harrier CLI. Thin shell over the domain package; no logic lives here."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harrier.db import connect, default_db_path
from harrier.profile import export_to, import_from, list_documents
from harrier.tracker.export import export_csv
from harrier.tracker.migrate_legacy import MigrationError, migrate
from harrier.tracker.store import TrackerError


def load_project_env(path: Path | None = None) -> None:
    """Load .env from the working directory (spec 011; launchd wrappers rely
    on it). Existing environment variables are never overridden."""
    env_path = path if path is not None else Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _cmd_discover(args: argparse.Namespace) -> int:
    from harrier.discovery import (
        SOURCE_ORDER,
        DiscoveryOptions,
        apify_allowed_now,
        run_discovery,
    )

    only = frozenset(item.strip() for item in args.only_source if item.strip())
    unknown = sorted(only - set(SOURCE_ORDER))
    if unknown:
        print(
            f"unknown --only-source value(s): {', '.join(unknown)}; "
            f"valid: {', '.join(SOURCE_ORDER)}",
            file=sys.stderr,
        )
        return 2

    def runnable(name: str) -> bool:
        if only and name not in only:
            return False
        if name == "wellfound":
            return bool(args.wellfound_file)
        if name == "wttj":
            return bool(args.wttj_file)
        if name == "apify_linkedin" and args.scheduled:
            return apify_allowed_now()
        return True

    enabled = [name for name in SOURCE_ORDER if runnable(name)]
    total = len(enabled)
    state = {"step": 0}

    def progress(source: str, stage: str) -> None:
        if stage == "fetching":
            state["step"] += 1
        payload = {
            "event": "progress",
            "step": state["step"],
            "total": total,
            "message": f"{source}: {stage}",
        }
        print(f"::harrier::{json.dumps(payload)}", flush=True)
        print(f"{source}: {stage}", flush=True)

    conn = connect()
    aggregate = run_discovery(
        conn,
        DiscoveryOptions(
            dry_run=args.dry_run,
            notify=not args.no_notify,
            only_sources=only,
            apify_count=int(args.apify_count),
            dataset_files=list(args.dataset_file),
            wellfound_files=list(args.wellfound_file),
            wttj_files=list(args.wttj_file),
            scheduled=args.scheduled,
        ),
        progress,
    )
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))
    return 0


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


def _cmd_demo_run(args: argparse.Namespace) -> int:
    """Exercise the run machinery (spec 006): progress protocol plus log lines."""
    import json
    import signal
    import time
    from types import FrameType

    interrupted = False

    def handle_term(_signum: int, _frame: FrameType | None) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, handle_term)
    steps = int(args.steps)
    delay = float(args.delay)
    for step in range(1, steps + 1):
        if interrupted:
            print("demo run interrupted", flush=True)
            return 130
        payload = {"event": "progress", "step": step, "total": steps, "message": f"step {step}"}
        print(f"::harrier::{json.dumps(payload)}", flush=True)
        print(f"working on step {step} of {steps}", flush=True)
        time.sleep(delay)
    if interrupted:
        print("demo run interrupted", flush=True)
        return 130
    print("demo run complete", flush=True)
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

    discover = sub.add_parser("discover", help="run discovery over all sources (spec 011)")
    discover.add_argument("--dry-run", action="store_true", help="evaluate without writes")
    discover.add_argument("--no-notify", action="store_true", help="skip the Telegram summary")
    discover.add_argument(
        "--only-source", action="append", default=[], help="restrict to a source (repeatable)"
    )
    discover.add_argument("--apify-count", type=int, default=150, help="Apify job count")
    discover.add_argument(
        "--dataset-file", action="append", default=[], help="local Apify dataset JSON (repeatable)"
    )
    discover.add_argument(
        "--wellfound-file", action="append", default=[], help="Wellfound export (repeatable)"
    )
    discover.add_argument(
        "--wttj-file", action="append", default=[], help="WTTJ export (repeatable)"
    )
    discover.add_argument(
        "--scheduled",
        action="store_true",
        help="apply the scheduled policy: Apify on weekday mornings only, configured count",
    )
    discover.set_defaults(func=_cmd_discover)

    demo_run = sub.add_parser("demo-run", help="exercise the run machinery (spec 006)")
    demo_run.add_argument("--steps", default="8", help="number of progress steps")
    demo_run.add_argument("--delay", default="0.4", help="seconds between steps")
    demo_run.set_defaults(func=_cmd_demo_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_project_env()
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
