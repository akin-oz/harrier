"""The daily Telegram digest (spec 019 port of send_daily_digest.py).

Five sections over the database and the mail watch event log; one
message a day, and dry runs never send.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import cast

from harrier.mail.watch import events_path
from harrier.notify import send_telegram_message
from harrier.tracker import list_jobs

DIGEST_ACTIONABLE_KINDS = {
    "interview_invite",
    "scheduling_request",
    "assessment",
    "request_info",
    "recruiter_reply",
}
TOP_PROSPECT_STATUSES = {"prospect", "shortlisted", "tailored_cv_requested"}
WAIT_ACTIONS = {"wait for reply", "wait until outreach window"}
GHOSTED_DAYS = 21

SendFn = Callable[[str], int]


def parse_target_date(raw: str | None) -> date:
    if raw:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    return datetime.now(UTC).date()


def extract_auto_added_date(notes: str) -> str | None:
    match = re.search(
        r"(?:^|;\s*)(?:auto_added|tier_a_seed)=(\d{4}-\d{2}-\d{2})(?:;|$)", notes or ""
    )
    return match.group(1) if match else None


def tracker_added_at(row: dict[str, str]) -> str:
    added = (row.get("added_at") or "").strip()
    if added:
        return added[:10]
    return extract_auto_added_date(row.get("notes", "")) or ""


def parse_score(value: str) -> int:
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def find_new_prospects(rows: list[dict[str, str]], target_date: date) -> list[dict[str, str]]:
    wanted = target_date.isoformat()
    return [row for row in rows if tracker_added_at(row) == wanted]


def top_prospects(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    active = [
        row for row in rows if (row.get("status") or "").strip().lower() in TOP_PROSPECT_STATUSES
    ]
    return sorted(active, key=lambda row: parse_score(row.get("fit_score", "0")), reverse=True)[:3]


def outreach_actions_due(rows: list[dict[str, str]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if (row.get("status") or "").strip().lower() != "applied":
            continue
        action = (row.get("next_outreach_action") or "").strip().lower()
        if not action or action in WAIT_ACTIONS:
            continue
        company = (row.get("company") or "Unknown").strip() or "Unknown"
        groups[action].append(company)
    return dict(groups)


def ghosted_applications(rows: list[dict[str, str]], target_date: date) -> list[str]:
    cutoff = target_date - timedelta(days=GHOSTED_DAYS)
    companies: list[str] = []
    for row in rows:
        if (row.get("status") or "").strip().lower() != "applied":
            continue
        raw_date = (row.get("applied_date") or "").strip()
        if not raw_date:
            continue
        try:
            applied = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if applied <= cutoff:
            companies.append((row.get("company") or "Unknown").strip() or "Unknown")
    return companies


def parse_event_timestamp(raw: str) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def extract_json_payload(line: str) -> dict[str, object] | None:
    """Tolerant event-line parsing: the old HANDLER_OUTPUT prefix is
    accepted so a migrated legacy log still reads."""
    text = line.strip()
    if not text:
        return None
    if "HANDLER_OUTPUT:" in text:
        text = text.split("HANDLER_OUTPUT:", 1)[1].strip()
    if not text.startswith("{"):
        return None
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def actionable_updates(target_date: date) -> list[dict[str, object]]:
    path = events_path()
    if not path.exists():
        return []
    updates: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        payload = extract_json_payload(line)
        if not payload:
            continue
        kind = payload.get("kind") or payload.get("action")
        if kind not in DIGEST_ACTIONABLE_KINDS:
            continue
        timestamp = parse_event_timestamp(str(payload.get("timestamp") or ""))
        if not timestamp or timestamp.date() != target_date:
            continue
        company = str(payload.get("company") or "Unknown company").strip() or "Unknown company"
        role = str(payload.get("role") or "Unknown role").strip() or "Unknown role"
        tracker_row = payload.get("tracker_row")
        key = (str(kind), company, role, str(tracker_row), timestamp.isoformat())
        if key in seen:
            continue
        seen.add(key)
        updates.append(
            {
                "kind": kind,
                "company": company,
                "role": role,
                "tracker_row": tracker_row,
                "timestamp": timestamp,
                "next_action": str(payload.get("next_action") or "").strip(),
            }
        )
    return sorted(updates, key=lambda item: cast("datetime", item["timestamp"]), reverse=True)


def render_digest(
    target_date: date,
    new_rows: list[dict[str, str]],
    top_rows: list[dict[str, str]],
    outreach_groups: dict[str, list[str]],
    ghosted: list[str],
    updates: list[dict[str, object]],
) -> str:
    lines = [f"Daily job digest — {target_date.isoformat()}", ""]

    lines.append(f"New prospects today: {len(new_rows)}")
    if new_rows:
        for row in new_rows[:5]:
            lines.append(
                f"• {row.get('company', 'Unknown')} — {row.get('title', 'Unknown')} "
                f"({row.get('fit_score', '0')})"
            )
    else:
        lines.append("• None")

    lines.extend(["", "Top 3 prospects"])
    if top_rows:
        for index, row in enumerate(top_rows, start=1):
            status = (row.get("status") or "").strip() or "prospect"
            lines.append(
                f"{index}. {row.get('company', 'Unknown')} — {row.get('title', 'Unknown')} "
                f"({row.get('fit_score', '0')}, {status})"
            )
    else:
        lines.append("• None")

    lines.extend(["", "Outreach actions due"])
    if outreach_groups:
        for action, companies in sorted(outreach_groups.items()):
            lines.append(f"  {action} ({len(companies)})")
            for company in companies[:5]:
                lines.append(f"  - {company}")
            if len(companies) > 5:
                lines.append(f"  - ... and {len(companies) - 5} more")
    else:
        lines.append("  All caught up.")

    if ghosted:
        lines.extend(["", f"⚠️ {len(ghosted)} applications ghosted (>21d no response)"])
        for company in ghosted[:10]:
            lines.append(f"  - {company}")
        if len(ghosted) > 10:
            lines.append(f"  - ... and {len(ghosted) - 10} more")

    lines.extend(["", "Updates needing action"])
    if updates:
        for item in updates[:5]:
            tracker = item["tracker_row"]
            tracker_text = f", tracker {tracker}" if tracker not in (None, "", "null") else ""
            next_action = str(item["next_action"]) or "Review and respond."
            kind_text = str(item["kind"]).replace("_", " ")
            lines.append(
                f"• {kind_text}: {item['company']} — {item['role']}{tracker_text}. {next_action}"
            )
    else:
        lines.append("• None")

    return "\n".join(lines)


def build_digest(conn: sqlite3.Connection, target_date: date) -> str:
    rows = list_jobs(conn)
    return render_digest(
        target_date,
        find_new_prospects(rows, target_date),
        top_prospects(rows),
        outreach_actions_due(rows),
        ghosted_applications(rows, target_date),
        actionable_updates(target_date),
    )


def run_digest(
    conn: sqlite3.Connection,
    target_date: date,
    *,
    dry_run: bool = False,
    send: SendFn = send_telegram_message,
) -> tuple[str, int]:
    """Render the digest; send unless dry-run. Returns (digest, send rc)."""
    digest = build_digest(conn, target_date)
    if dry_run:
        return digest, 0
    return digest, send(digest)
