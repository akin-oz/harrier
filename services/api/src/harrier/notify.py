"""Telegram notification (spec 011 port of send_telegram.py).

The only outbound messages the system sends (product invariant: nothing
auto-sends to employers; Telegram is the user's own channel). The bot token
rides in the URL path per the Bot API; the URL is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Literal, cast, get_args

from harrier.credentials import FIELD_MAX_BYTES
from harrier.demo import is_demo_mode
from harrier.runoutcome import RunOutcome
from harrier.sources import scrub_secrets

logger = logging.getLogger(__name__)


def redact_values(text: str, *values: str) -> str:
    """`text` with each value removed, as given and as `repr` escapes it:
    `http.client.InvalidURL` quotes the request path so, a tab as `\\t`.
    The send holds the token, so it removes it by value; `scrub_secrets`
    matches only a well-formed `/bot<token>`, and a stray space or tab ends
    its match (privacy review of PR #312). The chat id travels in the
    request body, which no error here quotes."""
    forms = {form for value in values if value for form in (value, repr(value)[1:-1])}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "REDACTED")
    return text


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
        # The exception text can embed the request path, `/bot<token>/...`:
        # `http.client.InvalidURL` quotes it, escaped, whitespace and all.
        # Hosted, logs leave the machine (spec 109, A failed send).
        reason = redact_values(str(exc), token)
        logger.warning("telegram send failed: %s", scrub_secrets(reason))
        return 1


# --- checking a bot and a chat before they are saved (spec 109) ---

TELEGRAM_API = "https://api.telegram.org"
# The one message harrier sends outside a run. It names nothing about the
# owner or their search (spec 109, Saving).
CONFIRMATION_TEXT = "harrier: Telegram notifications are connected for this account."
# As `send_telegram_message` uses.
BOT_API_TIMEOUT_SECONDS = 30
# A Bot API answer is a few hundred bytes. Chosen, not measured: it bounds
# what one answer can make the check read.
BOT_API_ANSWER_LIMIT = 65_536

TelegramReason = Literal[
    "token_shape", "chat_id_shape", "token_rejected", "chat_unreachable", "telegram_unavailable"
]
TELEGRAM_REASONS: tuple[TelegramReason, ...] = get_args(TelegramReason)

# The forms the Bot API documents, in ASCII only: `\d` would take any
# Unicode digit, and `$` would take a trailing newline. A value is bounded
# by the credential store's field rule, so a pair that passes here is one
# the store accepts (spec 116).
TOKEN_SHAPE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+", re.ASCII)
CHAT_ID_SHAPE = re.compile(r"-?[0-9]+", re.ASCII)

# `urllib.request.urlopen`'s calling form, replaced by a fake in tests.
Opener = Callable[..., Any]

BotApiAnswer = Literal["ok", "refused", "unavailable"]


def _within_bound(value: str) -> bool:
    return len(value) <= FIELD_MAX_BYTES


def _call_bot_api(
    opener: Opener, token: str, method: str, fields: Mapping[str, str] | None
) -> BotApiAnswer:
    """One Bot API call. "refused" is Telegram saying `ok: false`: the
    mapping reads that field, which the API documents, not `error_code`,
    which it says is subject to change. Anything that is not an answer
    about the token or the chat is "unavailable": a transport failure, a
    timeout, an answer that is not a JSON object with an `ok` field, and a
    5xx or 429 status, which speak for the service and not for the bot.

    Nothing raised here is kept or logged: the URL holds the token."""
    data = urllib.parse.urlencode(fields).encode("utf-8") if fields is not None else None
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"} if data else {},
        method="POST" if data is not None else "GET",
    )
    status = 0
    body = b""
    try:
        try:
            with opener(request, timeout=BOT_API_TIMEOUT_SECONDS) as response:
                status = int(response.status)
                body = response.read(BOT_API_ANSWER_LIMIT)
        except urllib.error.HTTPError as error:
            # The Bot API answers a refusal with a 4xx and a JSON body.
            status = error.code
            body = error.read(BOT_API_ANSWER_LIMIT)
    except Exception:
        # Any exception type, as `send_telegram_message` catches: an error
        # that escaped would reach a log with the URL, and the token, in its
        # text. Not `BaseException`: a stop signal still stops.
        return "unavailable"
    if status >= 500 or status == 429:
        return "unavailable"
    try:
        answer: object = json.loads(body)
    except (ValueError, RecursionError):
        return "unavailable"
    ok = cast(dict[str, object], answer).get("ok") if isinstance(answer, dict) else None
    if ok is True and 200 <= status < 300:
        return "ok"
    if ok is False:
        return "refused"
    return "unavailable"


def check_telegram(
    token: str, chat_id: str, *, opener: Opener | None = None
) -> TelegramReason | None:
    """None when `token` is a working bot and the bot can write to
    `chat_id`, else the reason it is not (spec 109, Saving).

    In order: the shapes, `getMe` with the token, then one `sendMessage` of
    `CONFIRMATION_TEXT` to the chat. A call is made only when the one
    before it passed, so a refused token never sends. The values are used
    as given: the caller strips them. Used only by the save route; no run
    calls it. Nothing is logged here, and no error text leaves this
    function, because the token is in every URL it builds.
    """
    if not _within_bound(token) or not TOKEN_SHAPE.fullmatch(token):
        return "token_shape"
    if not _within_bound(chat_id) or not CHAT_ID_SHAPE.fullmatch(chat_id):
        return "chat_id_shape"
    send = opener if opener is not None else urllib.request.urlopen
    got_me = _call_bot_api(send, token, "getMe", None)
    if got_me != "ok":
        return "token_rejected" if got_me == "refused" else "telegram_unavailable"
    sent = _call_bot_api(
        send, token, "sendMessage", {"chat_id": chat_id, "text": CONFIRMATION_TEXT}
    )
    if sent != "ok":
        return "chat_unreachable" if sent == "refused" else "telegram_unavailable"
    return None


TELEGRAM_MESSAGE_LIMIT = 4096


def build_telegram_message(
    new_jobs: list[dict[str, object]], *, outcome: RunOutcome | None = None
) -> str:
    """The 8-item prospect summary (spec 011 port), bounded to Telegram's
    4096-character sendMessage limit: entries stop before the limit and the
    result is hard-capped as a last resort.

    The outcome line leads, because this message is now sent whether or not
    anything was found (spec 029) and "0 new prospects" alone is what an outage
    of any length looks like.
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


def build_academic_telegram_message(
    label: str,
    kept: list[dict[str, object]],
    *,
    fetched: int,
    failed: str = "",
) -> str:
    """An academic track's discovery summary (spec 097).

    The track's label, the counts, and for each kept row only its title,
    organisation, deadline and URL: the deadline stands where the industry
    message shows a score. Never a search term, a flag or description text.
    """
    lines: list[str] = []
    if failed:
        lines.append(f"DISCOVERY FAILED on {label}: {failed}")
    else:
        lines.append(f"Discovery ok on {label}: {fetched} fetched, {len(kept)} new")
    lines.append("")
    for index, row in enumerate(kept[:8], start=1):
        deadline = str(row.get("deadline", "") or "") or "no deadline"
        entry = [
            f"{index}. {row.get('company', 'Unknown')}: {row.get('title', 'Unknown')}",
            f"   deadline: {deadline}",
            f"   {row.get('url', '')}",
            "",
        ]
        candidate = "\n".join([*lines, *entry]).rstrip()
        if len(candidate) > TELEGRAM_MESSAGE_LIMIT:
            break
        lines.extend(entry)
    return "\n".join(lines).rstrip()[:TELEGRAM_MESSAGE_LIMIT]
