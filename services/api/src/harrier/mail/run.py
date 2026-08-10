"""The Gmail watch run (spec 018 port of gmail_watch.py).

A library function returning a summary plus printable lines (stated
change: the counters are testable without patching print). Dry runs
send nothing; actionable events notify through harrier.notify.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from harrier.mail.watch import (
    ACTIONABLE_KINDS,
    GmailMessage,
    append_event,
    classify_message,
    fetch_recent_messages,
    format_telegram_message,
    load_state,
    now_iso,
    save_state,
    state_path,
    tracker_rows,
)
from harrier.notify import send_telegram_message

SEEN_STATE_LIMIT = 5000

FetchFn = Callable[[], list[GmailMessage]]
SendFn = Callable[[str], int]


@dataclass
class WatchSummary:
    fetched_count: int = 0
    unseen_count: int = 0
    actionable_count: int = 0
    ignored_count: int = 0
    send_failure: int = 0
    lines: list[str] = field(default_factory=list[str])


def _debug_line(
    message: GmailMessage, *, is_seen: bool, classified_kind: str, actionable: bool
) -> str:
    safe_id = message.message_id if message.message_id.strip() else "<missing>"
    safe_sender = message.sender or "Unknown sender"
    safe_subject = message.subject or "No subject"
    return (
        f"message_id={safe_id} | sender={safe_sender} | subject={safe_subject} | "
        f"is_seen={'true' if is_seen else 'false'} | classified_kind={classified_kind} | "
        f"actionable={'true' if actionable else 'false'}"
    )


def run_watch(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    fetch: FetchFn = fetch_recent_messages,
    send: SendFn = send_telegram_message,
) -> WatchSummary:
    summary = WatchSummary()
    state = load_state()
    seen_raw = state.get("seen_message_ids")
    # A dict as an ordered set: the cap must drop the OLDEST ids, and a
    # plain set's hash order would discard an arbitrary subset and cause
    # duplicate alerts on reclassification (review finding).
    seen_ids: dict[str, None] = (
        dict.fromkeys(str(item) for item in cast("list[object]", seen_raw))
        if isinstance(seen_raw, list)
        else {}
    )
    rows = tracker_rows(conn)
    messages = fetch()
    summary.fetched_count = len(messages)

    for message in messages:
        message_id = (message.message_id or "").strip()
        if not message_id:
            if dry_run:
                summary.lines.append(
                    _debug_line(
                        message,
                        is_seen=False,
                        classified_kind="invalid_message_id",
                        actionable=False,
                    )
                )
            continue
        if message_id in seen_ids:
            if dry_run:
                summary.lines.append(
                    _debug_line(
                        message, is_seen=True, classified_kind="skipped_seen", actionable=False
                    )
                    + " | skip_reason=already_seen"
                )
            continue

        summary.unseen_count += 1
        event = classify_message(message, rows)
        append_event(event)
        if dry_run:
            line = _debug_line(
                message,
                is_seen=False,
                classified_kind=str(event["kind"]),
                actionable=bool(event["kind"] in ACTIONABLE_KINDS),
            )
            if event["kind"] == "ignored":
                line += f" | ignore_reason={event.get('ignore_reason', 'unknown')}"
            summary.lines.append(line)
        if event["kind"] in ACTIONABLE_KINDS:
            summary.actionable_count += 1
            telegram_message = format_telegram_message(event)
            if dry_run:
                summary.lines.append(telegram_message)
                summary.lines.append("")
            else:
                rc = send(telegram_message)
                if rc != 0:
                    # The event is already logged; the run stops with the
                    # send failure (old behavior).
                    summary.send_failure = rc
                    break
        else:
            summary.ignored_count += 1
        seen_ids[message_id] = None

    state["seen_message_ids"] = list(seen_ids)[-SEEN_STATE_LIMIT:]
    state["updated_at"] = now_iso()
    save_state(state)

    summary.lines.append(f"fetched_count={summary.fetched_count}")
    summary.lines.append(f"unseen_count={summary.unseen_count}")
    summary.lines.append(f"actionable_count={summary.actionable_count}")
    summary.lines.append(f"ignored_count={summary.ignored_count}")
    return summary


def migrate_seen_state(old_root: Path) -> Path:
    """Copy the old repo's seen state into the data directory (read-only
    on the source, spec 018)."""
    source = old_root / "state" / "gmail-watch" / "seen_messages.json"
    if not source.is_file():
        raise FileNotFoundError(f"no seen state at {source}")
    target = state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target
