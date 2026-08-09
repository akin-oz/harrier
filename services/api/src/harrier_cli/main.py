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


def _cmd_tailor(args: argparse.Namespace) -> int:
    from harrier.resume.content import ResumeBundleError
    from harrier.resume.tailor import run_tailor

    jd_text: str | None = None
    if args.jd_text:
        jd_text = args.jd_text
    elif args.jd_file:
        try:
            jd_text = Path(args.jd_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"tailor failed: cannot read --jd-file: {error}", file=sys.stderr)
            return 1

    conn = connect()
    try:
        result = run_tailor(conn, args.job_id, jd_text=jd_text, no_ai=args.no_ai)
    except (ResumeBundleError, ValueError, RuntimeError) as error:
        print(f"tailor failed: {error}", file=sys.stderr)
        return 1
    print(f"tailored_pdf={result.pdf_path}")
    print(f"metadata={result.metadata_path}")
    if result.evaluation_path is not None:
        print(f"evaluation_report={result.evaluation_path}")
    print(f"ai_tailored={'yes' if result.ai_tailored else 'no'}")
    return 0


def _read_jd_file(path_value: str | None) -> tuple[str | None, int | None]:
    if not path_value:
        return None, None
    try:
        return Path(path_value).read_text(encoding="utf-8"), None
    except (OSError, UnicodeError) as error:
        print(f"cannot read --jd-file: {error}", file=sys.stderr)
        return None, 1


def _cmd_cover_letter(args: argparse.Namespace) -> int:
    from harrier.apply import generate_cover_letter, write_cover_letter_artifacts
    from harrier.apply.profile import ApplicationProfileError
    from harrier.screening.descriptions import load_cached_description
    from harrier.tracker import get_job

    jd_text, error_code = _read_jd_file(args.jd_file)
    if error_code is not None:
        return error_code
    conn = connect()
    try:
        row = get_job(conn, args.job_id)
        if not jd_text:
            jd_text = load_cached_description(row.get("url", "")) or None
        letter = generate_cover_letter(
            conn,
            row.get("company", ""),
            row.get("title", ""),
            job_url=row.get("url", ""),
            tracker_row=row,
            jd_text=jd_text,
            extra_notes=args.notes,
        )
        artifacts = write_cover_letter_artifacts(
            conn,
            row.get("company", ""),
            row.get("title", ""),
            row.get("url", ""),
            letter["short_version"],
            letter["full_version"],
        )
    except (ApplicationProfileError, TrackerError, ValueError, RuntimeError) as error:
        print(f"cover letter failed: {error}", file=sys.stderr)
        return 1
    for kind, path in artifacts.items():
        print(f"{kind}={path}")
    return 0


def _cmd_answers(args: argparse.Namespace) -> int:
    from harrier.apply import generate_answer_set, parse_questions, render_markdown, write_output
    from harrier.apply.profile import ApplicationProfileError
    from harrier.screening.descriptions import load_cached_description
    from harrier.tracker import get_job

    jd_text, error_code = _read_jd_file(args.jd_file)
    if error_code is not None:
        return error_code
    conn = connect()
    try:
        row = get_job(conn, args.job_id)
        if not jd_text:
            jd_text = load_cached_description(row.get("url", "")) or None
        questions = parse_questions(args.question, args.questions_file)
        drafts = generate_answer_set(
            conn,
            row.get("company", ""),
            row.get("title", ""),
            questions,
            job_url=row.get("url", ""),
            tracker_row=row,
            jd_text=jd_text,
        )
        content = render_markdown(
            row.get("company", ""), row.get("title", ""), row.get("url", ""), row, drafts
        )
        output_path = write_output(row.get("company", ""), row.get("title", ""), content)
    except (ApplicationProfileError, TrackerError, OSError, ValueError, RuntimeError) as error:
        print(f"answers failed: {error}", file=sys.stderr)
        return 1
    print(f"answers={output_path}")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from harrier.offers import EvaluationError, evaluate_offer
    from harrier.screening.descriptions import load_cached_description
    from harrier.tracker import get_job

    jd_text, error_code = _read_jd_file(args.jd_file)
    if error_code is not None:
        return error_code
    if args.jd_text:
        jd_text = args.jd_text
    conn = connect()
    try:
        row = get_job(conn, args.job_id)
        if not jd_text:
            jd_text = load_cached_description(row.get("url", ""))
        result = evaluate_offer(
            conn,
            row.get("company", ""),
            row.get("title", ""),
            row.get("url", ""),
            jd_text or "",
        )
    except (EvaluationError, TrackerError, ValueError) as error:
        print(f"evaluate failed: {error}", file=sys.stderr)
        return 1
    print(f"evaluation_report={result.report_path}")
    print(f"verdict={result.verdict.verdict}")
    print(f"confidence={result.verdict.confidence}")
    print(f"reason={result.verdict.reason}")
    return 0


def _cmd_evaluate_prospects(args: argparse.Namespace) -> int:
    from harrier.offers import BatchOptions, evaluate_prospects

    conn = connect()
    summary = evaluate_prospects(
        conn,
        BatchOptions(
            apply=args.apply,
            threshold=args.threshold,
            limit=args.limit,
            refresh=args.refresh,
            include_borderline=args.include_borderline,
        ),
    )
    for line in summary.lines:
        print(line)
    print(f"processed={summary.processed}")
    print(f"skipped_existing={summary.skipped_existing}")
    print(f"errors={summary.errors}")
    print(f"verdict_counts={json.dumps(summary.verdict_counts)}")
    label = "auto_rejected" if args.apply else "would_reject"
    print(f"{label}={summary.auto_rejected if args.apply else summary.would_reject}")
    if not args.apply and summary.would_reject:
        print("re-run with --apply to commit the rejections")
    return 0


def _cmd_find_contacts(args: argparse.Namespace) -> int:
    from harrier.outreach import find_best_contacts_for_job, find_contacts_for_job
    from harrier.tracker import get_job

    conn = connect()
    try:
        row = get_job(conn, args.job_id)
        finder = find_best_contacts_for_job if args.best_only else find_contacts_for_job
        summary = finder(
            company=row.get("company", ""),
            role=row.get("title", ""),
            job_url=row.get("url", ""),
            max_items=args.max_items,
        )
    except (TrackerError, RuntimeError) as error:
        print(f"find-contacts failed: {error}", file=sys.stderr)
        return 1
    from typing import cast

    print(json.dumps({k: v for k, v in summary.items() if k != "candidates"}, indent=2))
    candidates_raw = summary.get("candidates")
    candidates = cast("list[object]", candidates_raw) if isinstance(candidates_raw, list) else []
    for index, item in enumerate(candidates[:8], start=1):
        row_data = cast("dict[str, str]", item) if isinstance(item, dict) else {}
        print(
            f"{index}. {row_data.get('person_name', '')} | {row_data.get('person_title', '')} | "
            f"{row_data.get('relevance', '')} | fit={row_data.get('fit_score', '')} | "
            f"{row_data.get('linkedin_url', '')}"
        )
    return 0


def _cmd_contacts(args: argparse.Namespace) -> int:
    from harrier.outreach import (
        approve_candidate,
        set_best_contact_for_job,
        sync_tracker_outreach,
        update_candidate_review_status,
    )
    from harrier.tracker import get_job, list_contacts

    conn = connect()
    if args.contacts_command == "list":
        for contact in list_contacts(conn):
            print(
                f"{contact['id']}. {contact.get('person_name', '')} | "
                f"{contact.get('person_title', '')} | {contact.get('relevance', '')} | "
                f"{contact.get('company', '')} | {contact.get('contact_status', '')}"
            )
        return 0
    try:
        row = get_job(conn, args.job_id)
    except TrackerError as error:
        print(f"contacts failed: {error}", file=sys.stderr)
        return 1
    company = row.get("company", "")
    role = row.get("title", "")
    if args.contacts_command == "set-best":
        updated_row = set_best_contact_for_job(conn, args.job_id, args.linkedin_url)
        if updated_row is None:
            print("contact is not linked to this job", file=sys.stderr)
            return 1
        print(f"best_contact={updated_row.get('best_contact_name', '')}")
        return 0
    if args.contacts_command == "approve":
        added = approve_candidate(conn, company, role, row.get("url", ""), args.linkedin_url)
        if added is None:
            print("candidate not found in the staged artifact", file=sys.stderr)
            return 1
        sync_tracker_outreach(conn)
        print(f"approved: {added.get('person_name', '')} ({added.get('linkedin_url', '')})")
        return 0
    updated = update_candidate_review_status(company, role, args.linkedin_url, "rejected")
    if updated is None:
        print("candidate not found in the staged artifact", file=sys.stderr)
        return 1
    print(f"rejected: {updated.get('person_name', '')}")
    return 0


def _cmd_outreach(args: argparse.Namespace) -> int:
    from harrier.outreach import (
        mark_job_outreach_replied,
        mark_job_outreach_sent,
        outreach_due_rows,
        snooze_job_outreach,
        sync_tracker_outreach,
    )

    conn = connect()
    try:
        if args.outreach_command == "sync":
            rows = sync_tracker_outreach(conn)
            print(f"synced {len(rows)} rows")
        elif args.outreach_command == "due":
            for row in outreach_due_rows(conn):
                print(
                    f"{row['id']}. {row.get('company', '')} | {row.get('title', '')} | "
                    f"{row.get('next_outreach_action', '')} | "
                    f"best={row.get('best_contact_name', '')}"
                )
        elif args.outreach_command == "mark-sent":
            row = mark_job_outreach_sent(conn, args.job_id, sent_at=args.date)
            print(f"outreach_status={row['outreach_status']}")
        elif args.outreach_command == "mark-replied":
            row = mark_job_outreach_replied(conn, args.job_id, replied_at=args.date)
            print(f"outreach_status={row['outreach_status']}")
        else:
            row = snooze_job_outreach(conn, args.job_id, args.until)
            print(f"next_outreach_action={row['next_outreach_action']}")
    except (TrackerError, ValueError) as error:
        print(f"outreach failed: {error}", file=sys.stderr)
        return 1
    return 0


def _cmd_backfill_posters(args: argparse.Namespace) -> int:
    from harrier.outreach import backfill_posters

    conn = connect()
    summary = backfill_posters(conn, limit=args.limit, dry_run=args.dry_run)
    for line in summary.lines:
        print(line)
    print(
        json.dumps(
            {
                "checked": summary.checked,
                "staged": summary.staged,
                "skipped_existing": summary.skipped_existing,
                "no_poster": summary.no_poster,
                "errors": summary.errors,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
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

    tailor = sub.add_parser(
        "tailor", help="generate a tailored resume PDF for a tracker job (spec 013)"
    )
    tailor.add_argument("--job-id", type=int, required=True)
    jd_group = tailor.add_mutually_exclusive_group()
    jd_group.add_argument("--jd-text", help="inline job description text")
    jd_group.add_argument("--jd-file", help="path to a job description text file")
    tailor.add_argument(
        "--no-ai",
        action="store_true",
        help="use the deterministic evidence plan only (reproducible validation)",
    )
    tailor.set_defaults(func=_cmd_tailor)

    cover = sub.add_parser(
        "cover-letter", help="generate a cover letter with the PDF gate (spec 014)"
    )
    cover.add_argument("--job-id", type=int, required=True)
    cover.add_argument("--jd-file", help="path to a job description text file")
    cover.add_argument("--notes", help="extra guidance passed to the generator")
    cover.set_defaults(func=_cmd_cover_letter)

    answers = sub.add_parser(
        "answers", help="draft application answers for a tracker job (spec 014)"
    )
    answers.add_argument("--job-id", type=int, required=True)
    answers_group = answers.add_mutually_exclusive_group()
    answers_group.add_argument("--question", help="a single question")
    answers_group.add_argument("--questions-file", help="file with one question per line")
    answers.add_argument("--jd-file", help="path to a job description text file")
    answers.set_defaults(func=_cmd_answers)

    evaluate = sub.add_parser(
        "evaluate", help="six-block offer evaluation for a tracker job (spec 015)"
    )
    evaluate.add_argument("--job-id", type=int, required=True)
    evaluate_group = evaluate.add_mutually_exclusive_group()
    evaluate_group.add_argument("--jd-text", help="inline job description text")
    evaluate_group.add_argument("--jd-file", help="path to a job description text file")
    evaluate.set_defaults(func=_cmd_evaluate)

    prospects = sub.add_parser(
        "evaluate-prospects",
        help="batch-evaluate prospects with opt-in auto-reject (spec 015)",
    )
    prospects.add_argument(
        "--apply", action="store_true", help="commit auto-rejects (default: dry run)"
    )
    prospects.add_argument(
        "--threshold", type=float, default=0.8, help="min confidence to auto-reject"
    )
    prospects.add_argument("--limit", type=int, default=0, help="only the first N prospects")
    prospects.add_argument(
        "--refresh", action="store_true", help="re-evaluate even if a report exists"
    )
    prospects.add_argument(
        "--include-borderline",
        action="store_true",
        help="also auto-reject borderline verdicts",
    )
    prospects.set_defaults(func=_cmd_evaluate_prospects)

    find_contacts = sub.add_parser(
        "find-contacts", help="stage outreach candidates via Apify profile search (spec 016)"
    )
    find_contacts.add_argument("--job-id", type=int, required=True)
    find_contacts.add_argument("--max-items", type=int, default=10)
    find_contacts.add_argument(
        "--best-only", action="store_true", help="stop early on a strong match"
    )
    find_contacts.set_defaults(func=_cmd_find_contacts)

    contacts = sub.add_parser("contacts", help="contact operations (spec 016)")
    contacts_sub = contacts.add_subparsers(dest="contacts_command", required=True)
    contacts_sub.add_parser("list", help="list stored contacts")
    for name, help_text in (
        ("approve", "copy a staged candidate into contacts"),
        ("reject", "mark a staged candidate rejected"),
        ("set-best", "pin a linked contact as the job's best contact"),
    ):
        stage_cmd = contacts_sub.add_parser(name, help=help_text)
        stage_cmd.add_argument("--job-id", type=int, required=True)
        stage_cmd.add_argument("--linkedin-url", required=True)
    contacts.set_defaults(func=_cmd_contacts)

    outreach = sub.add_parser("outreach", help="outreach queue actions (spec 016)")
    outreach_sub = outreach.add_subparsers(dest="outreach_command", required=True)
    outreach_sub.add_parser("sync", help="re-derive outreach fields for every row")
    outreach_sub.add_parser("due", help="list due outreach actions")
    for name in ("mark-sent", "mark-replied"):
        mark_cmd = outreach_sub.add_parser(name)
        mark_cmd.add_argument("--job-id", type=int, required=True)
        mark_cmd.add_argument("--date", default=None)
    snooze_cmd = outreach_sub.add_parser("snooze")
    snooze_cmd.add_argument("--job-id", type=int, required=True)
    snooze_cmd.add_argument("--until", required=True)
    outreach.set_defaults(func=_cmd_outreach)

    backfill = sub.add_parser(
        "backfill-posters", help="backfill LinkedIn poster contacts via guest endpoint (spec 016)"
    )
    backfill.add_argument("--limit", type=int, default=0)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.set_defaults(func=_cmd_backfill_posters)

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
