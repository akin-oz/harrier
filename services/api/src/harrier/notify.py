"""Telegram notification (spec 011 port of send_telegram.py).

The only outbound messages the system sends (product invariant: nothing
auto-sends to employers; Telegram is the user's own channel). The bot token
rides in the URL path per the Bot API; the URL is never logged.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request

from harrier.demo import is_demo_mode
from harrier.runoutcome import RunOutcome

logger = logging.getLogger(__name__)


def send_telegram_message(
    message: str, token: str | None = None, chat_id: str | None = None
) -> int:
    """0 sent, 1 send failure, 2 missing configuration (old CLI semantics)."""
    if is_demo_mode():
        # The gate belongs here rather than at each call site: on a machine
        # that does hold Telegram credentials, a demo run would otherwise
        # message the owner's real chat with synthetic data (review finding
        # on PR #18).
        logger.info("demo mode: not sending the Telegram message")
        return 2
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token:
        logger.warning("missing TELEGRAM_BOT_TOKEN")
        return 2
    if not chat_id:
        logger.warning("missing TELEGRAM_CHAT_ID")
        return 2

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if 200 <= response.status < 300:
                return 0
            logger.warning("telegram send failed: HTTP %s", response.status)
            return 1
    except Exception as exc:
        logger.warning("telegram send failed: %s", exc)
        return 1


TELEGRAM_MESSAGE_LIMIT = 4096


def build_telegram_message(
    new_jobs: list[dict[str, object]], *, outcome: RunOutcome | None = None
) -> str:
    """The 8-item prospect summary (spec 011 port), bounded to Telegram's
    4096-character sendMessage limit: entries stop before the limit and the
    result is hard-capped as a last resort.

    The outcome line leads, because this message is now sent whether or not
    anything was found (spec 029) and "0 new prospects" alone is what a
    two-month outage looks like.
    """
    lines: list[str] = []
    if outcome is not None:
        if outcome.total_failure:
            lines.append(f"DISCOVERY FAILED: {outcome.describe()}")
        elif outcome.failed:
            lines.append(f"Discovery ran with failures: {outcome.describe()}")
        else:
            lines.append(f"Discovery ok: {outcome.describe()}")
        lines.append("")
    lines.append(f"Job imports: {len(new_jobs)} new prospects")
    lines.append("")
    for index, job in enumerate(new_jobs[:8], start=1):
        entry = [
            f"{index}. {job.get('company', 'Unknown')}: {job.get('title', 'Unknown')}",
            f"   {job.get('location', '')}",
            f"   score: {job.get('fit_score', '')}",
            f"   {job.get('url', '')}",
            "",
        ]
        candidate = "\n".join([*lines, *entry]).rstrip()
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            break
        lines.extend(entry)
    return "\n".join(lines).rstrip()[:TELEGRAM_MESSAGE_LIMIT]
