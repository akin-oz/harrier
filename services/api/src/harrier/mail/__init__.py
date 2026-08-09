"""Gmail watch and classification (spec 018). Readonly; nothing sends
except the operator's own Telegram channel for actionable events.
"""

from __future__ import annotations

from harrier.mail.run import WatchSummary, migrate_seen_state, run_watch
from harrier.mail.watch import (
    ACTIONABLE_KINDS,
    GMAIL_SCOPES,
    GmailMessage,
    classify_message,
    env_config,
    events_path,
    format_telegram_message,
    infer_company_role,
    normalize_gmail_api_message,
    validate_env,
)

__all__ = [
    "ACTIONABLE_KINDS",
    "GMAIL_SCOPES",
    "GmailMessage",
    "WatchSummary",
    "classify_message",
    "env_config",
    "events_path",
    "format_telegram_message",
    "infer_company_role",
    "migrate_seen_state",
    "normalize_gmail_api_message",
    "run_watch",
    "validate_env",
]
