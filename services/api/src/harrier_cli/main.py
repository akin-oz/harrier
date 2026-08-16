"""Harrier CLI. Thin shell over the domain package; no logic lives here."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from harrier.db import connect, default_db_path
from harrier.logsetup import configure_logging
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
    from harrier.runoutcome import classify_run

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
        if name == "apify_linkedin":
            if args.shadow:
                return False
            if args.scheduled:
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
            shadow=args.shadow,
            scheduled=args.scheduled,
        ),
        progress,
    )
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))

    # The exit status is the whole point of spec 029. Partial failure stays
    # zero: a day where one board is down and four are fine is a normal day,
    # and a status that fails a run the operator would call fine gets ignored
    # within a week. Total failure, and a run that attempted nothing, are the
    # only shapes that are never normal.
    outcome = classify_run(aggregate)
    if outcome.total_failure:
        print(f"discovery failed: {outcome.describe()}", file=sys.stderr)
        return outcome.exit_code
    if outcome.failed:
        print(f"discovery: {outcome.describe()}", file=sys.stderr)
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


def _cmd_check(args: argparse.Namespace) -> int:
    """Report rows that break an invariant. Changes nothing.

    Rows written before spec 036 may already be in states it forbids, and a
    migration that silently edited somebody's job search to satisfy a rule
    invented afterwards would destroy the record of what they actually did.
    So this reports and exits non-zero, and the operator decides.

    `--link-contacts` is the one thing it writes, and only when asked. The
    summary line says so when it wrote, rather than claiming nothing changed
    (review finding on PR #44).
    """
    from harrier.outreach.joblink import backfill_job_ids, unresolved_links
    from harrier.tracker.invariants import check_rows
    from harrier.tracker.store import list_jobs

    wrote = 0
    conn = connect()
    try:
        if args.link_contacts:
            # The one thing this command changes, and only when asked. It
            # gives existing contact links the job id they were written
            # without; it never drops a link it cannot match (spec 036).
            resolved, unmatched = backfill_job_ids(conn)
            wrote = resolved
            print(f"linked {resolved} contact link(s) to jobs; {unmatched} left unmatched")
        problems = check_rows(list_jobs(conn))
        link_problems = unresolved_links(conn)
    finally:
        conn.close()

    if not problems and not link_problems:
        print("no tracker rows break a status invariant, and every contact link resolves")
        return 0
    for job_id, breach in problems:
        print(f"job {job_id}: {breach}", file=sys.stderr)
    for who, breach in link_problems:
        print(f"contact {who}: {breach}", file=sys.stderr)
    total = len(problems) + len(link_problems)
    changed = f"{wrote} contact link(s) were written" if wrote else "nothing was changed"
    print(f"{total} item(s) need attention; {changed}", file=sys.stderr)
    return 1


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
    notes = args.notes
    if getattr(args, "notes_file", None):
        try:
            notes = Path(args.notes_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            print(f"cover letter failed: cannot read --notes-file: {error}", file=sys.stderr)
            return 1
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
            extra_notes=notes,
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


def _cmd_outreach_draft(args: argparse.Namespace) -> int:
    from typing import cast

    from harrier.outreach import find_contact, generate_outreach, write_outreach_draft
    from harrier.tracker import get_job

    jd_text, error_code = _read_jd_file(args.jd_file)
    if error_code is not None:
        return error_code
    supplied: dict[str, object] = {}
    if getattr(args, "input_file", None):
        try:
            parsed: object = json.loads(Path(args.input_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            print(f"outreach-draft failed: cannot read --input-file: {error}", file=sys.stderr)
            return 1
        if not isinstance(parsed, dict):
            print("outreach-draft failed: --input-file must hold a JSON object", file=sys.stderr)
            return 1
        supplied = cast("dict[str, object]", parsed)

    def supplied_text(key: str, fallback: str) -> str:
        value = supplied.get(key)
        return str(value) if isinstance(value, str) and value else fallback

    conn = connect()
    try:
        row = get_job(conn, args.job_id)
        contact_name = supplied_text("contact_name", args.contact_name or "")
        contact_role = supplied_text("contact_role", args.contact_role or "")
        contact_linkedin = supplied_text("contact_linkedin", args.contact_linkedin or "")
        jd_text = supplied_text("jd_text", jd_text or "") or None
        if contact_linkedin:
            # A supplied identifier always resolves; explicit manual fields
            # take precedence over the stored values (review finding: an
            # unknown identifier must not silently continue).
            contact = find_contact(conn, contact_linkedin)
            if contact is None:
                print(
                    f"no stored contact matches {contact_linkedin!r}; "
                    "add it via contacts approve or pass --contact-name",
                    file=sys.stderr,
                )
                return 1
            contact_name = contact_name or contact.get("person_name", "")
            contact_role = contact_role or contact.get("person_title", "")
        drafts = generate_outreach(
            conn,
            company=row.get("company", ""),
            role=row.get("title", ""),
            job_url=row.get("url", ""),
            contact_name=contact_name,
            contact_role=contact_role,
            contact_linkedin=contact_linkedin,
            jd_text=jd_text or "",
            audience=supplied_text("audience", args.audience or ""),
            tone=supplied_text("tone", args.tone),
            ai=args.ai,
        )
        paths = write_outreach_draft(row.get("company", ""), row.get("title", ""), drafts)
    except (TrackerError, ValueError, RuntimeError, OSError) as error:
        print(f"outreach-draft failed: {error}", file=sys.stderr)
        return 1
    for kind, path in paths.items():
        print(f"{kind}={path}")
    return 0


def _cmd_gmail_watch(args: argparse.Namespace) -> int:
    from harrier.mail import run_watch

    conn = connect()
    try:
        summary = run_watch(conn, dry_run=args.dry_run)
    except RuntimeError as error:
        print(f"gmail watch failed: {error}", file=sys.stderr)
        return 1
    for line in summary.lines:
        print(line)
    if summary.send_failure:
        # Clamp: POSIX exit statuses are modulo 256, so a raw helper value
        # like 256 would read as success (review finding).
        return min(max(1, summary.send_failure), 255)
    if not args.dry_run and summary.actionable_count == 0:
        print("gmail_watch=no_new_actionable_messages")
    return 0


def _cmd_gmail_oauth(args: argparse.Namespace) -> int:
    from harrier.mail import GMAIL_SCOPES, env_config

    config = env_config()
    client_secret_raw = args.client_secret_file or str(config.get("client_secret_file") or "")
    token_raw = args.token_file or str(config.get("token_file") or "")
    if not client_secret_raw.strip():
        print("missing GMAIL_OAUTH_CLIENT_SECRET_FILE", file=sys.stderr)
        return 2
    if not token_raw.strip():
        print("missing GMAIL_OAUTH_TOKEN_FILE", file=sys.stderr)
        return 2
    client_secret_file = Path(client_secret_raw).expanduser().resolve()
    token_file = Path(token_raw).expanduser().resolve()
    print(f"resolved_client_secret={client_secret_file}")
    print(f"resolved_token_file={token_file}")
    if client_secret_file.is_dir() or not client_secret_file.exists():
        print(f"client secret file not found: {client_secret_file}", file=sys.stderr)
        return 2
    if token_file.exists() and token_file.is_dir():
        print(f"token path is a directory: {token_file}", file=sys.stderr)
        return 2
    try:
        from google_auth_oauthlib.flow import (  # pyright: ignore[reportMissingImports]
            InstalledAppFlow,  # pyright: ignore[reportUnknownVariableType]
        )
    except ImportError:
        print(
            "missing Gmail OAuth dependencies. Install with:\n"
            "uv sync --project services/api --group gmail",
            file=sys.stderr,
        )
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_file), GMAIL_SCOPES)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    credentials = flow.run_local_server(port=0, open_browser=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    token_file.chmod(0o600)
    print(f"gmail_oauth_token={token_file}")
    return 0


def _cmd_gmail_migrate_state(args: argparse.Namespace) -> int:
    from harrier.mail import migrate_seen_state

    try:
        target = migrate_seen_state(Path(args.from_root))
    except FileNotFoundError as error:
        print(f"gmail state migration failed: {error}", file=sys.stderr)
        return 1
    print(f"migrated_state={target}")
    return 0


def _cmd_digest(args: argparse.Namespace) -> int:
    from harrier.digest import parse_target_date, run_digest

    try:
        target_date = parse_target_date(args.date)
    except ValueError as error:
        print(f"digest failed: invalid --date: {error}", file=sys.stderr)
        return 2
    conn = connect()
    digest, rc = run_digest(conn, target_date, dry_run=args.dry_run)
    print(digest)
    return rc


def cutover_installer() -> list[str]:
    """Install the new schedule during cutover, raising if it does not.

    A failed install raises rather than returning, because `run_cutover`
    reads a return as success. Proved by
    `tests/test_cutover.py::test_the_cli_wrapper_raises_when_the_installer_reports_failure`,
    which calls this directly, and the consequence for a whole run is proved
    separately by `::test_a_failed_schedule_install_fails_the_cutover`.

    Module level rather than a closure inside the cutover command: the defect
    lived in the wrapper, and a closure cannot be reached without running an
    entire cutover, which is why no test caught it.
    """
    from harrier.schedule import install_schedule

    outcome = install_schedule()
    if not outcome.ok:
        raise RuntimeError("; ".join(outcome.failures) or "the schedule did not install")
    return [*outcome.lines, "schedule install ok=True"]


def _cmd_cutover(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from harrier.cutover import CutoverError, preflight, run_cutover, utc_stamp

    old_root = Path(args.old_root).expanduser()
    if not old_root.is_dir():
        print(f"error: no old repo at {old_root}", file=sys.stderr)
        return 1

    conn = connect()
    try:
        if args.cutover_command == "preflight":
            checks = preflight(conn, old_root=old_root)
            print(checks.report())
            if not checks.ready:
                print(
                    f"\n{len(checks.blocked)} blocking check(s); cutover will refuse to run",
                    file=sys.stderr,
                )
                return 1
            print("\nevery mechanical check passes; the attestations above are yours to make")
            return 0

        install = cutover_installer

        try:
            result = run_cutover(
                conn,
                old_root=old_root,
                stamp=utc_stamp(datetime.now(UTC)),
                execute=args.execute,
                attested=args.attested,
                install=install,
            )
        except CutoverError as error:
            print(f"refused: {error}", file=sys.stderr)
            return 1
        for line in result.lines:
            print(line)
        for failure in result.failures:
            print(f"failure: {failure}", file=sys.stderr)
        for blocker in result.blocked:
            print(f"blocked: {blocker}", file=sys.stderr)
        if not result.executed:
            if result.blocked:
                print(
                    "\ndry run: nothing was changed, and the real run would be REFUSED "
                    "for the blocked check(s) above.",
                    file=sys.stderr,
                )
            else:
                print("\ndry run: nothing was changed. Add --execute --attested to do it for real.")
        return 0 if result.ok else 1
    finally:
        conn.close()


def _iso_date(value: str) -> str:
    """argparse validator: a bad date used to reach date.fromisoformat and
    escape as an uncaught ValueError traceback (review finding on PR #27)."""
    from datetime import date

    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from error


def _positive_int(value: str) -> int:
    """argparse validator: a negative limit silently became a slice like
    ranked[:-1], which is not a row limit (review finding on PR #27)."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a whole number: {value}") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {parsed}")
    return parsed


def _print_job(job: dict[str, str]) -> None:
    from harrier.tracker import describe

    print(describe(job))
    if job["next_action"]:
        print(f"   next: {job['next_action']}")


def _cmd_tracker_verb(args: argparse.Namespace) -> int:
    """The status transitions, add, and the two read verbs (spec 027)."""
    from harrier.capture import add_captured_job
    from harrier.tracker import (
        UNDECIDED_STATUSES,
        SelectorError,
        describe,
        list_jobs,
        rank_active,
        status_counts,
    )

    # The shared operations. The API routes call these same functions, so a
    # difference between the command line and the browser is a bug in one of
    # them rather than a design decision nobody wrote down (spec 042).
    from harrier.tracker.actions import TrackerActionError, change_status, rescore

    conn = connect()
    try:
        if args.command in {"next", "review"}:
            jobs = list_jobs(conn)
            if args.command == "review":
                # Counts cover everything; the queue below is only what still
                # needs a decision from you, which is what review is for.
                counts = status_counts(jobs)
                active = sum(count for name, count in counts.items() if name != "rejected")
                print(f"total {len(jobs)}, active {active}")
                print(", ".join(f"{name} {count}" for name, count in counts.items() if count))
                print()
                ranked = rank_active(jobs, args.limit, statuses=UNDECIDED_STATUSES)
                if not ranked:
                    print("nothing awaiting a decision")
                    return 0
            else:
                ranked = rank_active(jobs, args.limit)
                if not ranked:
                    print("nothing active")
                    return 0
            for job in ranked:
                _print_job(job)
            return 0

        if args.command == "add":
            result = add_captured_job(
                conn,
                company=args.company,
                title=args.title,
                location=args.location,
                url=args.url,
                source=args.source,
                description=args.description,
            )
            print(result.message)
            # CaptureResult carries no row, so the added or clashing job is
            # looked up by what identifies it, which is also the check the
            # duplicate path just ran.
            if args.url:
                for candidate in list_jobs(conn):
                    if candidate["url"] == args.url.strip():
                        print(describe(candidate))
                        break
            # duplicate and invalid are refusals, not crashes: the caller
            # asked to add something already tracked, or gave too little.
            return 0 if result.status == "added" else 1

        if args.command == "reevaluate":
            # The same function the API route calls (spec 042). Neither side
            # reimplements the other, and a test drives both through it.
            #
            # Spec 033's behaviour lives inside that function now rather than
            # here: rescoring uses the description stored at import, and a job
            # with none is refused instead of scored against less input than
            # the first pass had. Moving it rather than keeping a copy is the
            # whole point of the shared action.
            try:
                outcome = rescore(conn, args.selector)
            except TrackerActionError as error:
                # Named by what the operator typed. The row itself is not
                # resolved on this path any more, because the shared action
                # resolves it.
                print(f"skipped {args.selector}: {error}", file=sys.stderr)
                return 2
            print(f"rescored {outcome.previous} -> {outcome.current}")
            _print_job(outcome.job)
            return 0

        reason = " ".join(getattr(args, "reason", []) or []).strip() or None
        # The same function the API route calls (spec 042).
        updated = change_status(
            conn,
            args.selector,
            args.command,
            reason=reason,
            applied_date=getattr(args, "applied_date", None),
        )
        _print_job(updated)
        return 0
    except SelectorError as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        conn.close()


def _cmd_config(args: argparse.Namespace) -> int:
    from harrier.sources.feeds import FEEDS_PATH, read_line_config
    from harrier.userconfig import (
        COMPANY_HOLDS,
        DISCOVERY,
        DISCOVERY_PATH,
        FEEDS,
        HOLDS_PATH,
        KINDS,
        LINKEDIN_SEARCHES,
        SEARCH_URLS_PATH,
        ConfigError,
        delete_config,
        get_config,
        list_config,
        read_hold_file_raw,
        set_config,
    )

    conn = connect()
    try:
        if args.config_command == "list":
            rows = list_config(conn)
            if not rows:
                print("no configuration stored; the file fallbacks are in use")
            for row in rows:
                print(f"{row['kind']:20s} {row['updated_at']}  {row['value'][:80]}")
        elif args.config_command == "get":
            value = get_config(conn, args.kind)
            if value is None:
                print(f"no stored {args.kind}; the file fallback is in use", file=sys.stderr)
                return 1
            print(json.dumps(value, indent=2, ensure_ascii=False))
        elif args.config_command == "set":
            raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
            set_config(conn, args.kind, json.loads(raw))
            print(f"{args.kind} stored")
        elif args.config_command == "unset":
            removed = delete_config(conn, args.kind)
            print(f"{args.kind} {'removed' if removed else 'was not stored'}")
            return 0 if removed else 1
        else:
            # import: read each committed or local file once into the store.
            sources: dict[str, list[str]] = {
                FEEDS: read_line_config(FEEDS_PATH),
                LINKEDIN_SEARCHES: read_line_config(SEARCH_URLS_PATH),
                COMPANY_HOLDS: read_hold_file_raw(HOLDS_PATH),
            }
            imported = 0
            for kind, values in sources.items():
                if not values:
                    print(f"{kind}: no file to import, skipped")
                    continue
                set_config(conn, kind, values)
                print(f"{kind}: {len(values)} entries imported")
                imported += 1
            settings = _settings_from_file(DISCOVERY_PATH)
            if settings:
                set_config(conn, DISCOVERY, settings)
                print(f"{DISCOVERY}: {len(settings)} settings imported")
                imported += 1
            if not imported:
                print("nothing to import; no configuration files found", file=sys.stderr)
                return 1
            print(f"imported {imported} of {len(KINDS)} kinds")
    except (ConfigError, json.JSONDecodeError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _cmd_check_feeds(args: argparse.Namespace) -> int:
    from harrier.feedhealth import (
        DEAD,
        LIVE,
        UNREACHABLE,
        check_feeds,
        load_feeds_for_check,
        prune_dead,
    )

    conn = connect()
    try:
        feeds = load_feeds_for_check(conn)
        report = check_feeds(feeds)
        if not report.results:
            print("no boards configured", file=sys.stderr)
            return 1
        width = max(len(item.url) for item in report.results)
        for item in report.results:
            print(f"{item.url:{width}s}  {item.source:10s}  {item.verdict:11s}  {item.status}")
        counts = report.counts()
        print(f"\n{counts[LIVE]} live, {counts[DEAD]} dead, {counts[UNREACHABLE]} unreachable")
        if not args.prune:
            if counts[DEAD]:
                print("re-run with --prune to remove the dead entries")
            return 0
        removed = prune_dead(conn, report)
        if not removed:
            # Deliberately not an error: a watchlist with nothing dead in it
            # is the outcome --prune exists to produce.
            print("nothing pruned; no board answered as dead")
            return 0
        for item in removed:
            print(f"removed {item.url} ({item.status})")
        print(f"{len(removed)} removed; the remaining boards are now stored configuration")
    finally:
        conn.close()
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    from harrier.backup import BackupError, create_backup

    try:
        result = create_backup(Path(args.dest) if args.dest else None, keep=int(args.keep))
    except BackupError as error:
        print(f"backup failed: {error}", file=sys.stderr)
        return 1
    size_mb = result.bytes_written / (1024 * 1024)
    print(f"{result.archive} ({size_mb:.1f} MiB, {result.jobs} tracker rows, verified)")
    for path in result.pruned:
        print(f"pruned {path.name}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    from harrier.backup import BackupError, restore_backup

    try:
        restored = restore_backup(
            Path(args.archive), Path(args.into) if args.into else None, force=args.force
        )
    except BackupError as error:
        print(f"restore failed: {error}", file=sys.stderr)
        return 1
    print(f"restored {restored} tracker rows")
    return 0


def _cmd_verify_backup(args: argparse.Namespace) -> int:
    from harrier.backup import BackupError, verify_archive

    try:
        rows = verify_archive(Path(args.archive))
    except BackupError as error:
        print(f"archive is not usable: {error}", file=sys.stderr)
        return 1
    print(f"{args.archive} opens and holds {rows} tracker rows")
    return 0


def _cmd_reconsider(args: argparse.Namespace) -> int:
    from harrier.discovery import SOURCE_ORDER
    from harrier.screening.config import load_candidate_config
    from harrier.screening.reconsider import reconsider_source

    conn = connect()
    try:
        candidate_cfg = load_candidate_config(conn)
        sources = [args.source] if args.source else list(SOURCE_ORDER)
        total_cleared = 0
        for source in sources:
            report = reconsider_source(conn, source, candidate_cfg, dry_run=not args.apply)
            if report.examined:
                print(report.describe())
            total_cleared += report.changed
        if not total_cleared:
            # Not "everything used the current rules": zero is also what a
            # watchlist of protected manual rejections produces, and saying
            # the wrong one of those is a claim about the operator's own
            # decisions (review finding on PR #33).
            print("nothing is eligible to clear")
            return 0
        if args.apply:
            print(f"{total_cleared} cleared; the next discovery run will judge them again")
        else:
            print(f"{total_cleared} would be cleared; re-run with --apply")
    finally:
        conn.close()
    return 0


def _cmd_review_followup(args: argparse.Namespace) -> int:
    import subprocess
    import time

    from harrier.reviewfollowup import (
        DEFAULT_DAILY_LIMIT,
        REQUEST,
        RESPOND,
        WAIT,
        FollowUpError,
        decide,
        gather,
        load_counts,
        record_request,
        report,
        request_review,
    )

    def run_gh(argv: list[str]) -> str:
        result = subprocess.run(["gh", *argv], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"gh exited {result.returncode}")
        return result.stdout

    from harrier.reviewfollowup import PullRequestState

    numbers = [int(value) for value in args.pull_requests]
    counts = load_counts()
    states: list[PullRequestState] = []
    exit_code = 0
    for number in numbers:
        try:
            state = gather(number, run_gh, owner=args.owner, repo=args.repo)
        except FollowUpError as error:
            print(f"error: {error}", file=sys.stderr)
            exit_code = 1
            continue
        states.append(state)
        decision = decide(
            state,
            requests_today=counts.get(str(number), 0),
            daily_limit=int(args.daily_limit or DEFAULT_DAILY_LIMIT),
        )
        print(decision.describe(number))
        if decision.action == RESPOND:
            # Never asks for a new review here, and never marks anything read.
            # Reading a finding and answering it is the judgement half of this
            # spec, and it belongs to whoever is at the keyboard. All this can
            # do honestly is refuse to move on while the reviewer is waiting
            # on an answer.
            for thread in state.awaiting:
                print(f"  thread {thread.identifier} (resolved={thread.resolved})")
            for review in state.unread_reviews:
                where = (
                    " INCLUDING FINDINGS OUTSIDE THE DIFF"
                    if review.has_findings_outside_the_diff
                    else ""
                )
                print(f"  review {review.identifier}: {review.actionable_count} actionable{where}")
            continue
        if decision.action == WAIT and args.wait:
            time.sleep(decision.wait_minutes * 60)
            request_review(number, run_gh, owner=args.owner, repo=args.repo)
            counts[str(number)] = record_request(number)
            print(f"PR #{number}: review requested")
        elif decision.action == REQUEST and not args.dry_run:
            request_review(number, run_gh, owner=args.owner, repo=args.repo)
            counts[str(number)] = record_request(number)
            print(f"PR #{number}: review requested")

    print()
    for line in report(states):
        print(line)
    # A pull request nothing has reviewed is not a reviewed pull request,
    # which is the distinction the service's own check does not draw.
    if any(not state.reviewed for state in states):
        exit_code = exit_code or 2
    # And a reviewed pull request with an unanswered finding is not a settled
    # one. Exiting zero here is what let a Major finding sit unread: the loop
    # counted unresolved threads, and the finding was in a review body that
    # creates none (review finding on PR #37).
    if any(state.outstanding for state in states):
        exit_code = exit_code or 3
    return exit_code


def _settings_from_file(path: Path) -> dict[str, object]:
    from typing import cast

    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    # The committed example carries a _comment key for the reader; it is not
    # a setting and must not become one.
    return {
        key: value
        for key, value in cast("dict[str, object]", parsed).items()
        if not key.startswith("_")
    }


def _cmd_parity(args: argparse.Namespace) -> int:
    from harrier.parity import (
        CHECKLIST_PATH,
        MatrixError,
        RunSummaryError,
        checklist_status,
        diff_runs,
        load_run_summary,
        parse_matrix,
        render_diff,
        stated_counts,
        verdict_counts,
        waiver_problems,
        write_checklist,
    )
    from harrier.parity.checks import CHECKS

    try:
        if args.parity_command == "checklist":
            rows = parse_matrix()
            target = write_checklist(Path(args.out) if args.out else None)
            counts = verdict_counts(rows)
            print(f"{len(rows)} items written to {target}")
            print(f"keep={counts['keep']} change={counts['change']} drop={counts['drop']}")
            stated = stated_counts()
            if stated is not None and stated != counts:
                # The matrix states its own totals; a mismatch means the
                # document and its table disagree about what exists.
                print(
                    f"warning: the matrix states {stated} but its table holds {counts}",
                    file=sys.stderr,
                )
                return 1
        elif args.parity_command == "status":
            rows = parse_matrix()
            path = Path(args.checklist) if args.checklist else CHECKLIST_PATH
            if not path.is_file():
                print(
                    f"no checklist at {path}; run `harrier parity checklist` first", file=sys.stderr
                )
                return 1
            text = path.read_text(encoding="utf-8")
            status = checklist_status(text, rows)
            # Three populations, kept apart. One incomplete number over
            # ninety-seven items reads the same whether the work has moved or
            # not, which is why the old count carried no information
            # (spec 039).
            print(
                f"{status.total} items: {len(status.verified)} verified by a check, "
                f"{len(status.waived_items)} waived, {len(status.manual)} manual, "
                f"{len(status.failing)} failing"
            )
            for slug in status.verified:
                print(f"verified: {slug} ({CHECKS[slug].name})")
            for slug, why in status.failing:
                print(f"FAILING: {slug} ({why})", file=sys.stderr)
            for slug in status.orphaned:
                print(f"retired item still recorded: {slug}")

            # A tick with no reason looks like a decision and records none.
            unreasoned = waiver_problems(text)
            for slug in unreasoned:
                print(f"ticked with no reason: {slug}", file=sys.stderr)

            # Non-zero only for something that can actually be wrong: a check
            # that failed, a waiver with no reason, or a decision recorded
            # against an item the matrix no longer carries. A manual item is
            # a note, and exiting non-zero for the rest of time because
            # ninety-two notes are unread is the noise this replaces.
            if status.failing or unreasoned or status.orphaned:
                return 1
            return 0
        else:
            report = diff_runs(load_run_summary(Path(args.old)), load_run_summary(Path(args.new)))
            print(render_diff(report), end="")
            return 0 if report.clean else 1
    except (MatrixError, RunSummaryError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    import platform

    from harrier.schedule import (
        ScheduleConfigError,
        install_schedule,
        schedule_status,
        uninstall_schedule,
    )

    if platform.system() != "Darwin":
        print(
            "launchd is unavailable on this platform; run the CLI manually or via cron (ADR-006)",
            file=sys.stderr,
        )
        return 1
    try:
        if args.schedule_command == "install":
            result = install_schedule(dry_run=args.dry_run)
            for line in result.lines:
                print(line)
            print(f"written={len(result.written)} loaded={len(result.loaded)}")
            if not result.ok:
                print(f"{len(result.failures)} job(s) failed to load", file=sys.stderr)
                return 1
        elif args.schedule_command == "status":
            statuses = schedule_status()
            for status in statuses:
                print(status.line())
            # The exit code has to be able to say no (spec 040). A status
            # command that always succeeds is a status command nothing can be
            # scripted against, and the failure it is meant to surface is a
            # job that quietly stopped running.
            problems = [f"{status.name}: {status.problem}" for status in statuses if status.problem]
            if problems:
                print("", file=sys.stderr)
                for problem in problems:
                    print(f"problem: {problem}", file=sys.stderr)
                return 1
        else:
            uninstalled = uninstall_schedule()
            for line in uninstalled.lines:
                print(line)
            if not uninstalled.ok:
                print(f"{len(uninstalled.failures)} job(s) failed to unload", file=sys.stderr)
                return 1
    except ScheduleConfigError as error:
        print(f"schedule failed: {error}", file=sys.stderr)
        return 1
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

    check_parser = sub.add_parser(
        "check", help="report tracker rows that break a status invariant (spec 036)"
    )
    check_parser.add_argument(
        "--link-contacts",
        action="store_true",
        help="give existing contact links their job id, dropping nothing (spec 036)",
    )
    check_parser.set_defaults(func=_cmd_check)

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
    discover.add_argument(
        "--dry-run",
        action="store_true",
        help="evaluate without writes (still fetches every source, including paid Apify)",
    )
    discover.add_argument(
        "--shadow",
        action="store_true",
        help="dual-run mode (spec 022): dry-run and no paid source, so it is free to repeat",
    )
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
    notes_group = cover.add_mutually_exclusive_group()
    notes_group.add_argument("--notes", help="extra guidance passed to the generator")
    # The API passes notes as a file rather than on argv: argv is readable
    # from the process table and notes are the operator's own words (spec 047).
    notes_group.add_argument("--notes-file", help="path to a file holding that guidance")
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

    outreach_draft = sub.add_parser(
        "outreach-draft", help="generate outreach message drafts (spec 017; nothing sends)"
    )
    outreach_draft.add_argument("--job-id", type=int, required=True)
    outreach_draft.add_argument("--contact-linkedin", default="")
    outreach_draft.add_argument("--contact-name", default="")
    outreach_draft.add_argument("--contact-role", default="")
    outreach_draft.add_argument(
        "--audience", choices=["recruiter", "hiring_manager", "peer"], default=""
    )
    outreach_draft.add_argument(
        "--tone", choices=["direct", "warm", "concise", "confident"], default="direct"
    )
    outreach_draft.add_argument("--jd-file", default=None)
    outreach_draft.add_argument("--ai", action="store_true", help="AI drafts instead of templates")
    # The API passes the contact and the tone as a JSON file rather than on
    # argv: a contact's name and LinkedIn URL are a real person's details, and
    # argv is readable from the process table (spec 048).
    outreach_draft.add_argument(
        "--input-file", default=None, help="JSON holding the contact, audience, tone and jd_text"
    )
    outreach_draft.set_defaults(func=_cmd_outreach_draft)

    backfill = sub.add_parser(
        "backfill-posters", help="backfill LinkedIn poster contacts via guest endpoint (spec 016)"
    )
    backfill.add_argument("--limit", type=int, default=0)
    backfill.add_argument("--dry-run", action="store_true")
    backfill.set_defaults(func=_cmd_backfill_posters)

    gmail_watch = sub.add_parser(
        "gmail-watch", help="poll Gmail (readonly) and classify job emails (spec 018)"
    )
    gmail_watch.add_argument(
        "--dry-run", action="store_true", help="print classifications; send nothing"
    )
    gmail_watch.set_defaults(func=_cmd_gmail_watch)

    gmail_oauth = sub.add_parser(
        "gmail-oauth", help="bootstrap the local Gmail OAuth token (spec 018)"
    )
    gmail_oauth.add_argument("--client-secret-file", default=None)
    gmail_oauth.add_argument("--token-file", default=None)
    gmail_oauth.set_defaults(func=_cmd_gmail_oauth)

    gmail_migrate = sub.add_parser(
        "gmail-migrate-state", help="copy the old repo's seen state (spec 018)"
    )
    gmail_migrate.add_argument("--from-root", required=True)
    gmail_migrate.set_defaults(func=_cmd_gmail_migrate_state)

    digest = sub.add_parser("digest", help="send the daily Telegram digest (spec 019)")
    digest.add_argument("--date", default=None, help="UTC date YYYY-MM-DD (default today)")
    digest.add_argument("--dry-run", action="store_true", help="print the digest without sending")
    digest.set_defaults(func=_cmd_digest)

    schedule = sub.add_parser("schedule", help="launchd schedule lifecycle (spec 020)")
    schedule_sub = schedule.add_subparsers(dest="schedule_command", required=True)
    schedule_install = schedule_sub.add_parser("install", help="render, write, and load plists")
    schedule_install.add_argument(
        "--dry-run", action="store_true", help="render without writing or loading"
    )
    schedule_sub.add_parser("status", help="installed, loaded, drift, and next run")
    schedule_sub.add_parser("uninstall", help="unload and remove the plists")
    schedule.set_defaults(func=_cmd_schedule)

    cutover = sub.add_parser("cutover", help="the cutover from the old system (spec 024)")
    cutover.add_argument("--old-root", default="~/job-hunt-local", help="the old repo (read-only)")
    cutover_sub = cutover.add_subparsers(dest="cutover_command", required=True)
    cutover_sub.add_parser("preflight", help="check every precondition and refuse if unmet")
    cutover_run = cutover_sub.add_parser("run", help="quiesce, snapshot, verify, go live")
    cutover_run.add_argument(
        "--execute", action="store_true", help="do it for real (default is a dry run)"
    )
    cutover_run.add_argument(
        "--attested",
        action="store_true",
        help="confirm the checks no machine can make (see `cutover preflight`)",
    )
    cutover.set_defaults(func=_cmd_cutover)

    # Tracker verbs (spec 027): the daily driver, ported from the old
    # scripts/jobs.py. Every mutating verb takes a selector.
    for name, help_text in (
        ("shortlist", "mark a job shortlisted"),
        ("track", "mark a job as having a tailored CV requested"),
        ("interviewing", "mark a job as interviewing"),
        ("reevaluate", "rescore a job against the current candidate config"),
    ):
        verb = sub.add_parser(name, help=f"{help_text} (spec 027)")
        verb.add_argument("selector", help="job id, or a unique substring")
        verb.set_defaults(func=_cmd_tracker_verb)

    applied_cmd = sub.add_parser("applied", help="mark a job applied (spec 027)")
    applied_cmd.add_argument("selector", help="job id, or a unique substring")
    applied_cmd.add_argument(
        "--applied-date", type=_iso_date, default=None, help="YYYY-MM-DD (default today)"
    )
    applied_cmd.set_defaults(func=_cmd_tracker_verb)

    reject_cmd = sub.add_parser("reject", help="reject a job with a reason (spec 027)")
    reject_cmd.add_argument("selector", help="job id, or a unique substring")
    reject_cmd.add_argument("reason", nargs="*", help="why (recorded on the row)")
    reject_cmd.set_defaults(func=_cmd_tracker_verb)

    add_cmd = sub.add_parser("add", help="add a job by hand, scored and deduped (spec 027)")
    add_cmd.add_argument("--company", required=True)
    add_cmd.add_argument("--title", required=True)
    add_cmd.add_argument("--url", default="")
    add_cmd.add_argument("--location", default="")
    add_cmd.add_argument("--source", default="manual")
    add_cmd.add_argument("--description", default="")
    add_cmd.set_defaults(func=_cmd_tracker_verb)

    for name, help_text in (
        ("next", "what to work on now"),
        ("review", "tracker counts plus the top of the queue"),
    ):
        verb = sub.add_parser(name, help=f"{help_text} (spec 027)")
        verb.add_argument("--limit", type=_positive_int, default=10, help="rows to show")
        verb.set_defaults(func=_cmd_tracker_verb)

    config = sub.add_parser("config", help="user configuration in the database (spec 023)")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("list", help="what is stored")
    config_get = config_sub.add_parser("get", help="print one stored value as JSON")
    config_get.add_argument("kind")
    config_set = config_sub.add_parser("set", help="store one value from JSON")
    config_set.add_argument("kind")
    config_set.add_argument("--file", default=None, help="JSON file (default: stdin)")
    config_unset = config_sub.add_parser("unset", help="remove one value, restoring the fallback")
    config_unset.add_argument("kind")
    config_sub.add_parser("import", help="import the config/ files into the store, once")
    check_feeds_cmd = config_sub.add_parser(
        "check-feeds", help="probe every configured board and report live/dead (spec 025)"
    )
    check_feeds_cmd.add_argument(
        "--prune",
        action="store_true",
        help="remove the boards this run probed as dead; never removes unreachable ones",
    )
    check_feeds_cmd.set_defaults(func=_cmd_check_feeds)
    config.set_defaults(func=_cmd_config)

    backup = sub.add_parser("backup", help="verified snapshot of the data directory (spec 030)")
    backup.add_argument("--dest", default=None, help="destination directory")
    # Imported here rather than at module scope to match how every other
    # command in this file reaches the domain: the CLI stays importable
    # without pulling the whole domain in.
    from harrier.backup import DEFAULT_KEEP

    backup.add_argument(
        "--keep",
        default=str(DEFAULT_KEEP),
        type=_positive_int,
        help="archives to retain, on top of the newest of each week",
    )
    backup.set_defaults(func=_cmd_backup)

    restore = sub.add_parser("restore", help="restore a verified archive (spec 030)")
    restore.add_argument("archive")
    restore.add_argument("--into", default=None, help="data directory to restore into")
    restore.add_argument(
        "--force", action="store_true", help="overwrite a non-empty data directory"
    )
    restore.set_defaults(func=_cmd_restore)

    verify_backup = sub.add_parser("verify-backup", help="open an archive and query it")
    verify_backup.add_argument("archive")
    verify_backup.set_defaults(func=_cmd_verify_backup)

    reconsider = sub.add_parser(
        "reconsider",
        help="re-open rejections made under screening rules that have since changed (spec 031)",
    )
    reconsider.add_argument("--source", default=None, help="one source (default: all)")
    reconsider.add_argument(
        "--apply", action="store_true", help="clear them; without this it only reports"
    )
    reconsider.set_defaults(func=_cmd_reconsider)

    followup = sub.add_parser(
        "review-followup",
        help="wait out a rate-limited review and ask again (spec 043)",
    )
    followup.add_argument("pull_requests", nargs="+", help="pull request numbers")
    followup.add_argument("--owner", default="akin-oz")
    followup.add_argument("--repo", default="harrier")
    followup.add_argument("--daily-limit", default=None, type=_positive_int)
    followup.add_argument(
        "--wait", action="store_true", help="sleep out the rate limit rather than reporting it"
    )
    followup.add_argument(
        "--dry-run", action="store_true", help="report what it would do and comment nothing"
    )
    followup.set_defaults(func=_cmd_review_followup)

    parity = sub.add_parser("parity", help="parity verification against the old system (spec 022)")
    parity_sub = parity.add_subparsers(dest="parity_command", required=True)
    parity_checklist = parity_sub.add_parser(
        "checklist", help="generate the cutover checklist from docs/parity-matrix.md"
    )
    parity_checklist.add_argument("--out", default=None, help="output path")
    parity_status = parity_sub.add_parser("status", help="how much of the checklist is done")
    parity_status.add_argument("--checklist", default=None, help="checklist path")
    parity_diff = parity_sub.add_parser(
        "diff", help="compare an old-system run summary with a harrier one"
    )
    parity_diff.add_argument("--old", required=True, help="old system run summary JSON")
    parity_diff.add_argument("--new", required=True, help="harrier run summary JSON")
    parity.set_defaults(func=_cmd_parity)

    demo_run = sub.add_parser("demo-run", help="exercise the run machinery (spec 006)")
    demo_run.add_argument("--steps", default="8", help="number of progress steps")
    demo_run.add_argument("--delay", default="0.4", help="seconds between steps")
    demo_run.set_defaults(func=_cmd_demo_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    configure_logging()
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
