"""Gmail watch: normalization, the classification cascade, state, and
the event log (spec 018 port of gmail_watch_lib.py).

Readonly only: the single scope is gmail.readonly and nothing here
sends, replies, labels, or modifies mail. The event log under the data
directory is the digest's input (spec 019).
"""

from __future__ import annotations

# The google client libraries are an optional dependency group (gmail),
# lazily imported below; their stubs are absent in the base environment.
# pyright: reportMissingImports=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false
import base64
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.tracker import list_jobs

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SEEN_STATE_LIMIT = 5000

ACTIONABLE_KINDS = {
    "interview_invite",
    "scheduling_request",
    "assessment",
    "request_info",
    "recruiter_reply",
    "rejection",
    "application_confirmation",
}

PRIORITY_BY_KIND = {
    "interview_invite": "high",
    "scheduling_request": "high",
    "assessment": "high",
    "request_info": "medium",
    "recruiter_reply": "medium",
    "rejection": "medium",
    "application_confirmation": "low",
    "ignored": "low",
}

IGNORED_TOKENS = [
    "unsubscribe",
    "manage preferences",
    "newsletter",
    "job alert",
    "daily jobs",
    "new jobs for you",
    "google security alert",
    "security alert",
    "new sign-in",
    "critical security alert",
]
INTERVIEW_PATTERNS = [
    r"\binterview invitation\b",
    r"\binvitation to interview\b",
    r"\binvite you to interview\b",
    r"\binterview invite\b",
    r"\bphone interview\b",
    r"\bvideo interview\b",
    r"\bonsite interview\b",
    r"\bpanel interview\b",
]
SCHEDULING_TOKENS = [
    "what times work for you",
    "please let me know your availability",
    "book a time",
    "schedule",
    "calendar invite",
    "propose time",
    "availability",
]
ASSESSMENT_TOKENS = [
    "take-home",
    "take home",
    "coding assessment",
    "assessment",
    "technical test",
    "home assignment",
]
REQUEST_INFO_TOKENS = [
    "please send",
    "can you provide",
    "could you share",
    "please provide",
    "work authorization",
    "resume",
    "portfolio",
    "references",
]
APPLICATION_CONFIRMATION_TOKENS = [
    "thanks for applying",
    "application received",
    "we received your application",
    "your application has been submitted",
    "thanks for your interest",
]
RECRUITER_TOKENS = [
    "following up",
    "follow-up on your application",
    "next steps",
    "we'd like to move forward",
    "we would like to move forward",
    "reaching out",
    "recruiter",
    "wanted to follow up",
    "we're interested in",
]
REJECTION_TOKENS = [
    "we won't be moving forward",
    "not moving forward",
    "we regret to inform you",
    "unable to proceed",
    "will not be moving forward",
    "unfortunately",
]
SCHEDULING_LINKS = [
    "calendly.com",
    "doodle.com",
    "meet.google.com",
    "zoom.us",
    "teams.microsoft.com",
]


@dataclass
class GmailMessage:
    message_id: str
    subject: str
    sender: str
    sender_email: str
    timestamp: str
    body_plain: str
    snippet: str


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def watch_dir() -> Path:
    return data_dir() / "gmail-watch"


def state_path() -> Path:
    return watch_dir() / "seen_messages.json"


def events_path() -> Path:
    return watch_dir() / "events.jsonl"


# ---------------------------------------------------------------------------
# Message normalization
# ---------------------------------------------------------------------------


def decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def parse_message_timestamp(raw: str | None) -> datetime:
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except (TypeError, ValueError):
            pass
    return datetime.now(UTC)


def decode_gmail_body_data(data: str | None) -> str:
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
        return raw.decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _body_data(payload: dict[str, Any]) -> str | None:
    body = payload.get("body")
    if isinstance(body, dict):
        data = cast("dict[str, Any]", body).get("data")
        return data if isinstance(data, str) else None
    return None


def extract_plain_text_from_payload(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    mime_type = str(payload.get("mimeType") or "").lower()
    if mime_type == "text/plain":
        return decode_gmail_body_data(_body_data(payload))
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in cast("list[object]", parts):
            if isinstance(part, dict):
                text = extract_plain_text_from_payload(cast("dict[str, Any]", part))
                if text.strip():
                    return text
    return decode_gmail_body_data(_body_data(payload))


def header_map(headers: object) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(headers, list):
        return result
    for item in cast("list[object]", headers):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, Any]", item)
        name = str(entry.get("name", "")).lower()
        value = entry.get("value")
        result[name] = decode_header_value(value if isinstance(value, str) else None)
    return result


def body_snippet(text: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    return value[: limit - 1] + "…" if len(value) > limit else value


def normalize_gmail_api_message(raw: dict[str, Any]) -> GmailMessage:
    payload_raw = raw.get("payload")
    payload = cast("dict[str, Any]", payload_raw) if isinstance(payload_raw, dict) else {}
    headers = header_map(payload.get("headers"))
    sender = headers.get("from", "")
    sender_email = ""
    addresses = getaddresses([sender])
    if addresses:
        sender_email = addresses[0][1]
    timestamp = parse_message_timestamp(headers.get("date") or "").isoformat()
    body_plain = extract_plain_text_from_payload(payload or None)
    snippet = str(raw.get("snippet") or body_snippet(body_plain))
    return GmailMessage(
        message_id=str(raw.get("id") or ""),
        subject=headers.get("subject", ""),
        sender=sender,
        sender_email=sender_email,
        timestamp=timestamp,
        body_plain=body_plain,
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Tracker matching
# ---------------------------------------------------------------------------


def normalize_company(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"\b(inc|ltd|llc|corp|corporation|limited)\b", "", value, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" ,.-")
    return text or None


def infer_company_role(
    subject: str, body: str, sender_email: str, tracker_rows: list[dict[str, str]]
) -> tuple[str | None, str | None, str | None]:
    """Match the message to a tracker row; the returned third value is the
    job id (stated change from the old csv row number)."""
    haystack = normalize(" ".join(filter(None, [subject, body])))
    best_match: dict[str, str] | None = None
    sender_domain = sender_email.split("@", 1)[1].lower() if "@" in sender_email else ""
    sender_company = sender_domain.split(".")[0] if sender_domain else ""
    sender_company_matches: list[tuple[int, dict[str, str]]] = []
    for row in tracker_rows:
        company = row.get("company", "")
        title = row.get("title", "")
        company_ok = bool(company) and normalize(company) in haystack
        title_ok = bool(title) and normalize(title) in haystack
        if company_ok and title_ok:
            best_match = row
            break
        if company_ok and best_match is None:
            best_match = row
        if sender_company and normalize(company) == normalize(sender_company):
            title_tokens = [
                token for token in re.split(r"[^a-z0-9]+", normalize(title)) if len(token) > 3
            ]
            overlap = sum(token in haystack for token in title_tokens)
            if overlap:
                sender_company_matches.append((overlap, row))
    if not best_match and sender_company_matches:
        best_match = sorted(
            sender_company_matches,
            key=lambda item: (-item[0], int(item[1].get("id", "999999") or "999999")),
        )[0][1]
    if best_match:
        return best_match.get("company"), best_match.get("title"), best_match.get("id")

    company = normalize_company(sender_company.title()) if sender_company else None
    role_match = re.search(
        r"\b(senior|sr\.?|staff|lead)?\s*(frontend|software|product)\s+engineer[^\n,;]*",
        subject,
        flags=re.IGNORECASE,
    )
    role = role_match.group(0).strip() if role_match else None
    return company, role, None


# ---------------------------------------------------------------------------
# Classification cascade
# ---------------------------------------------------------------------------


def contains_any(text: str, tokens: list[str]) -> bool:
    value = normalize(text)
    return any(token in value for token in tokens)


def matches_any_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def summarize_message(subject: str, body: str) -> str:
    first_sentence = re.split(r"(?<=[.!?])\s+", body_snippet(body, limit=280))[0].strip()
    if first_sentence:
        return first_sentence
    return subject.strip() or "See email for details."


def suggest_next_action(kind: str) -> str:
    actions = {
        "interview_invite": "Reply and confirm interview availability.",
        "scheduling_request": "Send available time slots or use the scheduling link.",
        "assessment": "Review the assessment, confirm deadline, and plan completion time.",
        "application_confirmation": (
            "No immediate action needed. Keep the application in view for follow-up emails."
        ),
        "request_info": "Reply with the requested information or documents.",
        "recruiter_reply": "Read the reply and respond if a next step is requested.",
        "rejection": "Mark the application closed and archive the thread.",
    }
    return actions.get(kind, "Review the message.")


def classify_message(
    message: GmailMessage, tracker_rows: list[dict[str, str]]
) -> dict[str, object]:
    subject = message.subject or ""
    body = message.body_plain or ""
    full = f"{subject}\n{body}"
    has_recruiter_follow_up = contains_any(full, RECRUITER_TOKENS)

    ignore_reason = None
    if contains_any(full, IGNORED_TOKENS):
        kind = "ignored"
        ignore_reason = "marketing_or_security_email"
    elif matches_any_pattern(full, INTERVIEW_PATTERNS):
        kind = "interview_invite"
    elif contains_any(full, ASSESSMENT_TOKENS):
        kind = "assessment"
    elif contains_any(full, APPLICATION_CONFIRMATION_TOKENS) and not has_recruiter_follow_up:
        kind = "application_confirmation"
    elif contains_any(full, REJECTION_TOKENS):
        kind = "rejection"
    elif contains_any(full, REQUEST_INFO_TOKENS):
        kind = "request_info"
    elif contains_any(full, SCHEDULING_TOKENS) or contains_any(full, SCHEDULING_LINKS):
        kind = "scheduling_request"
    elif has_recruiter_follow_up:
        kind = "recruiter_reply"
    else:
        kind = "ignored"
        ignore_reason = "no_matching_category"

    company, role, tracker_row = infer_company_role(
        subject, body, message.sender_email, tracker_rows
    )
    result: dict[str, object] = {
        "kind": kind,
        "priority": PRIORITY_BY_KIND[kind],
        "company": company,
        "role": role,
        "tracker_row": tracker_row,
        "next_action": suggest_next_action(kind),
        "summary": summarize_message(subject, body),
        "from": message.sender,
        "timestamp": message.timestamp,
        "messageId": message.message_id,
        "subject": subject,
        "actionable": kind in ACTIONABLE_KINDS,
    }
    if ignore_reason:
        result["ignore_reason"] = ignore_reason
    return result


def format_telegram_message(event: dict[str, object]) -> str:
    if event["kind"] == "application_confirmation":
        lines = ["🟡 Application confirmed"]
    else:
        lines = [f"Priority: {event['priority']} • {event['kind']}"]
    if event.get("company") or event.get("role"):
        lines.append(
            f"Company/Role: {event.get('company') or 'Unknown company'} "
            f"— {event.get('role') or 'Unknown role'}"
        )
    if event.get("tracker_row"):
        lines.append(f"Tracker row: {event['tracker_row']}")
    if event["kind"] != "application_confirmation":
        lines.append(f"Next action: {event['next_action']}")
    lines.append(f"Summary: {event['summary']}")
    lines.append(f"From/time: {event.get('from') or 'Unknown sender'} • {event['timestamp']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# State, log, env
# ---------------------------------------------------------------------------


def load_state() -> dict[str, object]:
    path = state_path()
    if not path.exists():
        return {"seen_message_ids": []}
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"seen_message_ids": []}
    return (
        cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {"seen_message_ids": []}
    )


def save_state(state: dict[str, object]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def append_event(event: dict[str, object]) -> None:
    path = events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def tracker_rows(conn: sqlite3.Connection) -> list[dict[str, str]]:
    return list_jobs(conn)


def env_config() -> dict[str, object]:
    return {
        "account": os.getenv("GMAIL_ACCOUNT"),
        "client_secret_file": os.getenv("GMAIL_OAUTH_CLIENT_SECRET_FILE"),
        "token_file": os.getenv("GMAIL_OAUTH_TOKEN_FILE"),
        "lookback_days": int(os.getenv("GMAIL_POLL_LOOKBACK_DAYS", "7")),
        "max_messages": int(os.getenv("GMAIL_POLL_MAX_MESSAGES", "25")),
    }


def validate_env(config: dict[str, object]) -> None:
    missing: list[str] = []
    if not config.get("account"):
        missing.append("GMAIL_ACCOUNT")
    if not config.get("client_secret_file"):
        missing.append("GMAIL_OAUTH_CLIENT_SECRET_FILE")
    if not config.get("token_file"):
        missing.append("GMAIL_OAUTH_TOKEN_FILE")
    if missing:
        raise RuntimeError(f"missing environment variables: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Gmail API (optional dependency group: gmail)
# ---------------------------------------------------------------------------

_INSTALL_HINT = (
    "missing Gmail API dependencies. Install with:\nuv sync --project services/api --group gmail"
)


def load_gmail_credentials() -> object:
    config = env_config()
    validate_env(config)
    token_file = Path(str(config["token_file"])).expanduser()
    if not token_file.exists():
        raise RuntimeError(
            f"missing Gmail OAuth token file: {token_file}. Run: harrier gmail-oauth"
        )
    try:
        from google.auth.transport.requests import Request  # pyright: ignore[reportMissingImports]
        from google.oauth2.credentials import (  # pyright: ignore[reportMissingImports]
            Credentials,  # pyright: ignore[reportUnknownVariableType]
        )
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    credentials = Credentials.from_authorized_user_file(str(token_file), GMAIL_SCOPES)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if credentials.expired and credentials.refresh_token:  # pyright: ignore[reportUnknownMemberType]
        credentials.refresh(Request())  # pyright: ignore[reportUnknownMemberType]
        token_file.write_text(credentials.to_json(), encoding="utf-8")  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if not credentials.valid:  # pyright: ignore[reportUnknownMemberType]
        raise RuntimeError(f"invalid Gmail OAuth token at {token_file}. Run: harrier gmail-oauth")
    return credentials


def gmail_query(lookback_days: int) -> str:
    return f"newer_than:{lookback_days}d in:inbox"


def fetch_recent_messages() -> list[GmailMessage]:
    config = env_config()
    validate_env(config)
    credentials = load_gmail_credentials()
    try:
        from googleapiclient.discovery import (
            build,  # pyright: ignore[reportMissingImports, reportUnknownVariableType]
        )
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)  # pyright: ignore[reportUnknownVariableType]
    response: dict[str, Any] = (  # pyright: ignore[reportUnknownVariableType]
        service.users()  # pyright: ignore[reportUnknownMemberType]
        .messages()
        .list(
            userId="me",
            q=gmail_query(int(str(config["lookback_days"]))),
            maxResults=int(str(config["max_messages"])),
        )
        .execute()
    )
    messages: list[GmailMessage] = []
    for item in response.get("messages", []) or []:  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        raw: dict[str, Any] = (  # pyright: ignore[reportUnknownVariableType]
            service.users()  # pyright: ignore[reportUnknownMemberType]
            .messages()
            .get(userId="me", id=item["id"], format="full")
            .execute()
        )
        messages.append(normalize_gmail_api_message(raw))
    return messages
