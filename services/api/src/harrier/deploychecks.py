"""The deploy's checks that need no store: the project's Auth settings, the
API app's machine count, the scheduler's first tick after an update, and the
month's projected cost (spec 110).

`harrier deploy` runs each. Every decision here is a function of data the
caller parsed or typed, so the tests run them with no network, no Fly and no
Supabase: the one network call, `fetch_auth_settings`, is a seam the tests
replace, and the clock and the event reader of the tick check are arguments.

Nothing here prints a key, a URL or a response body.
"""

from __future__ import annotations

import base64
import calendar
import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import urlsplit

# --- the project's Auth settings ---

SUPABASE_URL_VARIABLE = "HARRIER_SUPABASE_URL"
PUBLISHABLE_KEY_VARIABLE = "HARRIER_SUPABASE_PUBLISHABLE_KEY"
AUTH_SETTINGS_TIMEOUT_SECONDS = 10
INVITE_ONLY = "auth settings: invite only"

# The `external` entries that are not an OAuth provider: the two ways a
# person signs in with an address or a number the project is told, and the
# anonymous sign-in, which is judged on its own.
NOT_A_PROVIDER = frozenset({"email", "phone", "anonymous_users"})

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class DeployCheckFailed(Exception):
    """A check failed, or its input could not be read. The text is the line
    to print after `error: `, and holds no key, URL or response body."""


def needs(command: str, variable: str) -> DeployCheckFailed:
    return DeployCheckFailed(f"harrier deploy {command} needs {variable}")


Fetch = Callable[[str, str], tuple[int, bytes]]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is an answer, not somewhere to send the key: the `apikey`
    header would follow it to whatever host it names."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_auth_settings(base_url: str, key: str) -> tuple[int, bytes]:
    """`GET <base_url>/auth/v1/settings` with the key as the `apikey` header,
    waiting at most 10 seconds. Returns the status and the body; raises
    OSError for no answer. The seam the tests replace."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/auth/v1/settings", headers={"apikey": key}
    )
    try:
        with _OPENER.open(request, timeout=AUTH_SETTINGS_TIMEOUT_SECONDS) as response:
            return int(response.status), bytes(response.read())
    except urllib.error.HTTPError as error:
        # An answer with a status, a redirect included: the caller names it.
        return int(error.code), b""


def auth_settings_refusal(settings: Mapping[str, object]) -> str | None:
    """The first field of the parsed settings that is not invite only, as
    `<field> is <value>`, or None.

    In order: `disable_signup` not `true` (false or missing: invite only
    cannot be shown), `external` not an object, `external.anonymous_users`
    true, any entry of `external` that is not a boolean, then any other entry
    that is true but `email` and `phone`, as `external.<name>`.
    """
    signup = settings.get("disable_signup")
    if signup is not True:
        return f"disable_signup is {_shown(signup, missing='missing')}"
    external = settings.get("external")
    if not isinstance(external, dict):
        # A project that does not say which providers are on cannot be shown
        # to have none on.
        return f"external is {'missing' if external is None else 'not an object'}"
    external_map = cast("Mapping[str, object]", external)
    if external_map.get("anonymous_users") is True:
        return "external.anonymous_users is true"
    for name in sorted(external_map):
        if not isinstance(external_map[name], bool):
            return f"external.{name} is not a boolean"
    for name in sorted(external_map):
        if name in NOT_A_PROVIDER:
            continue
        if external_map[name] is True:
            return f"external.{name} is true"
    return None


def _shown(value: object, *, missing: str) -> str:
    if value is None:
        return missing
    if isinstance(value, bool):
        return "true" if value else "false"
    return "not a boolean"


def check_auth_settings(environ: Mapping[str, str], fetch: Fetch = fetch_auth_settings) -> str:
    """`harrier deploy check-auth-settings`: the line to print on a pass.

    Raises DeployCheckFailed for a missing variable, an answer that is not a
    200 with a JSON object, and the first field that is not invite only.
    """
    url = environ.get(SUPABASE_URL_VARIABLE, "").strip()
    if not url:
        raise needs("check-auth-settings", SUPABASE_URL_VARIABLE)
    key = environ.get(PUBLISHABLE_KEY_VARIABLE, "").strip()
    if not key:
        raise needs("check-auth-settings", PUBLISHABLE_KEY_VARIABLE)
    parts = urlsplit(url)
    if parts.scheme != "https" and not (
        parts.scheme == "http" and (parts.hostname or "") in LOOPBACK_HOSTS
    ):
        # The key travels in a header: not over a plain connection to a host
        # that is not this machine.
        raise DeployCheckFailed(
            f"harrier deploy check-auth-settings needs {SUPABASE_URL_VARIABLE} "
            "to be an https:// URL"
        )
    try:
        status, body = fetch(url, key)
    except (OSError, ValueError, http.client.HTTPException) as error:
        # The class only: a malformed port or header would quote the URL or
        # the key.
        raise DeployCheckFailed(f"auth settings: no answer ({type(error).__name__})") from None
    if status != 200:
        raise DeployCheckFailed(f"auth settings: the project answered {status}")
    try:
        parsed: object = json.loads(body)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise DeployCheckFailed("auth settings: the answer is not a JSON object")
    refused = auth_settings_refusal(cast("Mapping[str, object]", parsed))
    if refused is not None:
        raise DeployCheckFailed(f"auth settings: {refused}")
    return INVITE_ONLY


# --- the Fly configuration's project values ---

# The two values in `[env]` of `deploy/fly.<env>.toml` that name the Supabase
# project. The files carry a placeholder for each and the deploy fills a copy
# (spec 110, "Amendment (2026-10-14): what building it settled").
FLY_PLACEHOLDERS: dict[str, str] = {
    "__HARRIER_SUPABASE_URL__": SUPABASE_URL_VARIABLE,
    "__HARRIER_SUPABASE_PUBLISHABLE_KEY__": PUBLISHABLE_KEY_VARIABLE,
}
SECRET_KEY_PREFIX = "sb_secret_"


def _holds_a_secret_key(value: str) -> bool:
    """Supabase's `sb_secret_` keys, and the legacy `service_role` JWT. The
    two shapes this knows are all it knows (spec 114 refuses the same two)."""
    if value.startswith(SECRET_KEY_PREFIX):
        return True
    parts = value.split(".")
    if len(parts) != 3:
        return False
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims: object = json.loads(base64.urlsafe_b64decode(padded))
    except ValueError:
        return False
    return isinstance(claims, dict) and cast("Mapping[str, object]", claims).get("role") == (
        "service_role"
    )


def fill_fly_config(text: str, environ: Mapping[str, str]) -> str:
    """The Fly configuration with each placeholder replaced by its variable.

    Raises DeployCheckFailed naming the variable, never its value, when one
    is unset or empty, holds a character a TOML string would need escaped (a
    quote, a backslash or a control character), or holds a secret key: the
    publishable key goes into a file Fly stores in the clear. Raises it too
    when a placeholder remains, so a file with a new one cannot deploy
    unfilled.
    """
    filled = text
    for placeholder, variable in FLY_PLACEHOLDERS.items():
        if placeholder not in filled:
            continue
        value = environ.get(variable, "").strip()
        if not value:
            raise needs("fill-fly-config", variable)
        if any(character in '"\\' or ord(character) in (*range(32), 127) for character in value):
            raise DeployCheckFailed(f"{variable} holds a quote, a backslash or a control character")
        if _holds_a_secret_key(value):
            raise DeployCheckFailed(f"{variable} holds a secret key; it takes the publishable key")
        filled = filled.replace(placeholder, value)
    left = sorted(set(re.findall(r"__HARRIER_[A-Z_]+__", filled)))
    if left:
        raise DeployCheckFailed(f"the configuration still holds a placeholder: {', '.join(left)}")
    return filled


# --- the API app's machine count ---

# Fly's default process group, the API's: the scheduler machine is in an app
# of its own (spec 110, Open decision 10).
API_PROCESS_GROUP = "app"


def api_machines(machines: object) -> list[Mapping[str, object]]:
    """The machines of the API's process group in a parsed `fly machines list
    --json`, whatever their state: started or stopped. A machine whose
    config names no group is the default group's, so it counts. Raises
    DeployCheckFailed when the list is not a list of objects."""
    if not isinstance(machines, list):
        raise DeployCheckFailed("machine list: not a JSON list")
    found: list[Mapping[str, object]] = []
    for item in cast("list[object]", machines):
        if not isinstance(item, dict):
            raise DeployCheckFailed("machine list: an entry is not a JSON object")
        machine = cast("Mapping[str, object]", item)
        if machine.get("state") in ("destroyed", "destroying"):
            continue
        if _process_group(machine) == API_PROCESS_GROUP:
            found.append(machine)
    return found


def _process_group(machine: Mapping[str, object]) -> str:
    config = machine.get("config")
    if not isinstance(config, dict):
        return API_PROCESS_GROUP
    metadata = cast("Mapping[str, object]", config).get("metadata")
    if not isinstance(metadata, dict):
        return API_PROCESS_GROUP
    group = cast("Mapping[str, object]", metadata).get("fly_process_group")
    return group if isinstance(group, str) and group else API_PROCESS_GROUP


def check_machine_count(listing: bytes | str) -> str:
    """`harrier deploy check-machine-count`: the line to print when the API
    process group has exactly one machine, started or stopped (spec 106,
    "Where a run lives on Fly"). Raises DeployCheckFailed otherwise."""
    try:
        parsed: object = json.loads(listing)
    except ValueError:
        raise DeployCheckFailed("machine list: not JSON") from None
    count = len(api_machines(parsed))
    if count != 1:
        raise DeployCheckFailed(
            f"the API app has {count} machines in the {API_PROCESS_GROUP} process group; "
            "exactly one is expected"
        )
    return "api machines: 1"


# --- the scheduler's first tick after an update (spec 108, Amendment 2026-10-12) ---

TICK_WAIT_MINUTES = 70
TICK_POLL_SECONDS = 60
FLY_SCHEDULER_SOURCE = "flyd"
TICKED = "scheduler ticked after the update"


def _event_seconds(timestamp: object) -> float | None:
    """An event's time as epoch seconds. Fly reports epoch milliseconds in
    its Machines API; an ISO 8601 string is read as well. Not confirmed on
    a real machine (spec 110, Honest limitations)."""
    if isinstance(timestamp, bool):
        return None
    if isinstance(timestamp, int | float):
        value = float(timestamp)
        # Milliseconds since the epoch are above 1e11 from 1973 on; seconds
        # are below it until the year 5138.
        return value / 1000 if value > 1e11 else value
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    return None


def ticked_after(events: object, updated_at: float) -> bool:
    """Whether the machine's events hold a `start` from `flyd` later than
    `updated_at` (epoch seconds). A start by hand, or an event of another
    type, does not count: the schedule is what is being checked."""
    if not isinstance(events, list):
        return False
    for item in cast("list[object]", events):
        if not isinstance(item, dict):
            continue
        event = cast("Mapping[str, object]", item)
        if event.get("type") != "start" or event.get("source") != FLY_SCHEDULER_SOURCE:
            continue
        at = _event_seconds(event.get("timestamp"))
        if at is not None and at > updated_at:
            return True
    return False


def wait_for_tick(
    read_events: Callable[[], object],
    updated_at: float,
    machine: str,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    minutes: int = TICK_WAIT_MINUTES,
    poll_seconds: int = TICK_POLL_SECONDS,
) -> str:
    """Read the machine's events until one is a tick after the update, for at
    most `minutes`. Returns `TICKED`; raises DeployCheckFailed naming the
    machine id when none appears, so the operator starts it by hand.

    One read at the start and one after each wait, so a tick that already
    happened is seen at once, and the last read is at the limit."""
    deadline = now() + minutes * 60
    while True:
        if ticked_after(read_events(), updated_at):
            return TICKED
        remaining = deadline - now()
        if remaining <= 0:
            raise DeployCheckFailed(
                f"the scheduler machine {machine} did not tick within {minutes} minutes of "
                "the update; start it by hand with fly machine start"
            )
        sleep(min(float(poll_seconds), remaining))


# --- the monthly cost projection ---

BUDGET_USD = Decimal(5)
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class CostLine:
    name: str
    usd: Decimal


def parse_cost_line(text: str) -> CostLine:
    """`<name>=<usd>`: a name, then a number of 0 or more. Raises ValueError
    naming what is wrong."""
    name, separator, amount = text.partition("=")
    if not separator:
        raise ValueError(f"{text!r} is not <name>=<usd>")
    name = name.strip()
    if not name:
        raise ValueError(f"{text!r} has no name before the =")
    try:
        usd = Decimal(amount.strip())
    except InvalidOperation:
        raise ValueError(f"the amount of {name} is not a number") from None
    if not usd.is_finite():
        raise ValueError(f"the amount of {name} is not a number")
    if usd < 0:
        raise ValueError(f"the amount of {name} is negative")
    return CostLine(name, usd)


def parse_as_of(text: str) -> date:
    """`YYYY-MM-DD`, a calendar date. Raises ValueError."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a calendar date, YYYY-MM-DD") from None


@dataclass(frozen=True)
class Projection:
    month: str
    spent: Decimal
    projected: Decimal
    days_elapsed: int
    days_in_month: int
    largest: CostLine
    over_budget: bool

    def report(self) -> str:
        return (
            f"projected {_money(self.projected)} USD for {self.month}, from "
            f"{_money(self.spent)} USD over {self.days_elapsed} of {self.days_in_month} days; "
            f"largest line: {self.largest.name} ({_money(self.largest.usd)} USD)"
        )


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def project_cost(as_of: date, lines: Sequence[CostLine]) -> Projection:
    """The month projected as spent so far / days elapsed x days in the
    month, where days elapsed is `as_of`'s day of the month. Over budget is
    decided before rounding, by cross multiplication, so it is exact."""
    if not lines:
        raise ValueError("no cost lines")
    spent = sum((line.usd for line in lines), Decimal(0))
    days_elapsed = as_of.day
    days_in_month = calendar.monthrange(as_of.year, as_of.month)[1]
    projected = spent * days_in_month / days_elapsed
    # The first of the largest, in the order given.
    largest = max(lines, key=lambda line: line.usd)
    return Projection(
        month=f"{as_of.year:04d}-{as_of.month:02d}",
        spent=spent,
        projected=projected,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        largest=largest,
        over_budget=spent * days_in_month > BUDGET_USD * days_elapsed,
    )
