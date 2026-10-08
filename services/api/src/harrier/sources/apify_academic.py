"""An academic job source on Apify (spec 097). Ingestion only.

The actor itself is named only in the environment (`APIFY_ACADEMIC_ACTOR`),
never here: its name describes the kind of search. What this module holds is
the actor's interface, which is not the operator's search: the input field
each compiled value goes to (`INPUT_MAP`), the output field each shared field
comes from (`FIELD_MAP`), the countries it supports, its prices, and the hard
limits on a run. The operator's search is the `academic_searches`
configuration kind, compiled by `harrier.academic.search`.

The field names are the ones the actor's public page documents, and the
synthetic fixture (`fixtures/academic-dataset.json`) uses them, so a test
holds the map against the shape. The portal ids the actor accepts are real
vacancy platform names, so they come from the environment too
(`APIFY_ACADEMIC_PORTALS`), not from this file.

Logging is a fixed label, the run id, the terminal status and counts. The
actor's name, any compiled input value and any item text are never logged.
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, cast
from urllib.parse import urlencode

from harrier.screening.normalized import NormalizedJob, make_normalized_job, normalize
from harrier.sources.apify_linkedin import (
    API_BASE_URL,
    TERMINAL_STATUSES,
    actor_path,
    load_dataset_files,
    request_json,
    unwrap_apify_data,
)

logger = logging.getLogger(__name__)

SOURCE_NAME = "apify_academic"
LOG_LABEL = "academic source"

ACTOR_ENV = "APIFY_ACADEMIC_ACTOR"
PORTALS_ENV = "APIFY_ACADEMIC_PORTALS"
TOKEN_ENV = "APIFY_TOKEN"

# The actor's input field for each value compiled from the search entry.
INPUT_MAP: dict[str, str] = {
    "keywords": "keywords",
    "countries": "countries",
    "window": "postedWithinDays",
    "result_cap": "maxTotalResults",
    "portals": "enabledSources",
    "translation": "translateKeywords",
    "memory": "incrementalMode",
}

# Always compiled, never configurable: translation would add terms the gates
# cannot know, and the actor's own memory would mark postings seen before
# harrier judged them (spec 031).
ALWAYS_OFF: tuple[str, ...] = ("translation", "memory")

# The actor's output field for each shared field. `start` has no field of its
# own on this actor; its contract field carries a start date for some
# portals, and that is shown as `contract`.
FIELD_MAP: dict[str, str | None] = {
    "external_id": "id",
    "title": "title",
    "organisation": "institution",
    "department": "department",
    "country": "country",
    "city": "city",
    "deadline": "deadline",
    "posted_at": "postedDate",
    "description": "descriptionRaw",
    "url": "applicationUrl",
    "apply_url": "applicationUrl",
    "position_type": "jobType",
    "subject": "field",
    "funding": "fundingSource",
    "start": None,
    "contract": "contractDuration",
    "posting_language": "workingLanguage",
    "salary_text": "salaryInfo",
    "portal": "sourcePortal",
}

# The structured fields copied onto a row as components, never judged here.
COMPONENTS: tuple[str, ...] = (
    "position_type",
    "subject",
    "funding",
    "start",
    "contract",
    "posting_language",
    "salary_text",
    "apply_url",
    "portal",
)

NOT_STATED = "not stated"

# The country codes the actor documents as supported.
SUPPORTED_COUNTRIES: frozenset[str] = frozenset(
    {
        "AT", "BE", "CH", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU",
        "IE", "IT", "LT", "LU", "LV", "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
    }
)  # fmt: skip

# The actor's prices, used for the worst-case check before a run. The run
# object's own pricing is compared after it, and a difference is reported.
PRICE_PER_RESULT_USD = 0.004
START_PRICE_USD_PER_GB = 0.01
MEMORY_MB = 1024
TIMEOUT_SECONDS = 1800
POLL_INTERVAL_SECONDS = 5

# Hard limits on one run (spec 097, decision 5). An entry above either is
# refused at the write and clamped where it is read (the spec 035 rule).
ACADEMIC_MAX_RESULTS = 500
ACADEMIC_MAX_CHARGE_USD = 5.0

STOPPED_STATUSES = frozenset({"ABORTED", "FAILED", "TIMED-OUT"})


class AcademicSourceError(RuntimeError):
    """The source could not produce a dataset. The message names no input."""


def known_portals() -> frozenset[str]:
    """The portal ids `portals` may name, from the environment."""
    raw = os.getenv(PORTALS_ENV, "")
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def worst_case_usd(max_results: int) -> float:
    """The most one run can cost: the start price for its memory, and every
    result the cap allows."""
    gigabytes = math.ceil(MEMORY_MB / 1024)
    return round(START_PRICE_USD_PER_GB * gigabytes + max_results * PRICE_PER_RESULT_USD, 6)


def compile_input(
    *,
    keywords: list[str],
    countries: list[str],
    window_days: int,
    max_results: int,
    portals: list[str] | None,
) -> dict[str, object]:
    """The actor's input, from values compiled out of a search entry."""
    compiled: dict[str, object] = {
        INPUT_MAP["keywords"]: list(keywords),
        INPUT_MAP["countries"]: list(countries),
        INPUT_MAP["window"]: window_days,
        INPUT_MAP["result_cap"]: max_results,
    }
    if portals:
        compiled[INPUT_MAP["portals"]] = list(portals)
    for name in ALWAYS_OFF:
        compiled[INPUT_MAP[name]] = False
    return compiled


def run_options(*, max_results: int, max_charge_usd: float) -> dict[str, str]:
    """The run's query options. `maxItems` bounds only actors priced per
    result; the input's result cap is what bounds this one, and the charge
    ceiling is the platform's backstop."""
    return {
        "maxItems": str(max_results),
        "maxTotalChargeUsd": f"{max_charge_usd:.2f}",
        "memory": str(MEMORY_MB),
        "timeout": str(TIMEOUT_SECONDS),
    }


Transport = Callable[..., object]


def _default_transport(url: str, *, method: str = "GET", payload: object = None) -> object:
    return request_json(
        url,
        method=method,
        payload=cast("dict[str, object] | None", payload),
        timeout_seconds=TIMEOUT_SECONDS,
    )


@dataclass
class ActorRun:
    """What a finished run left behind, read from its run object."""

    run_id: str
    status: str
    dataset_id: str
    items: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    charged_events: dict[str, int] = field(default_factory=dict[str, int])
    charged_usd: float | None = None
    pricing_differs: bool = False
    stopped_at_ceiling: bool = False


def charged_total(run: dict[str, Any]) -> tuple[dict[str, int], float | None, bool]:
    """The run's charged event counts, their priced total from its own
    `pricingInfo`, and whether that pricing differs from this module's."""
    raw_counts = run.get("chargedEventCounts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for name, value in cast("dict[str, object]", raw_counts).items():
            if isinstance(value, int | float) and not isinstance(value, bool):
                counts[str(name)] = int(value)
    pricing = run.get("pricingInfo")
    prices: dict[str, float] = {}
    if isinstance(pricing, dict):
        per_event = cast("dict[str, Any]", pricing).get("pricingPerEvent")
        events: object = (
            cast("dict[str, Any]", per_event).get("actorChargeEvents")
            if isinstance(per_event, dict)
            else None
        )
        if isinstance(events, dict):
            for name, entry in cast("dict[str, object]", events).items():
                if isinstance(entry, dict):
                    price = cast("dict[str, object]", entry).get("eventPriceUsd")
                    if isinstance(price, int | float) and not isinstance(price, bool):
                        prices[str(name)] = float(price)
    if not counts or not prices:
        return counts, None, False
    total = round(sum(prices.get(name, 0.0) * count for name, count in counts.items()), 6)
    ours = {PRICE_PER_RESULT_USD, START_PRICE_USD_PER_GB}
    differs = any(price not in ours for price in prices.values())
    return counts, total, differs


def start_run(
    token: str,
    actor: str,
    compiled: dict[str, object],
    *,
    max_results: int,
    max_charge_usd: float,
    transport: Transport,
) -> dict[str, Any]:
    query = {"token": token, **run_options(max_results=max_results, max_charge_usd=max_charge_usd)}
    endpoint = f"{API_BASE_URL}/acts/{actor_path(actor)}/runs?" + urlencode(query)
    run = unwrap_apify_data(transport(endpoint, method="POST", payload=compiled))
    if not isinstance(run, dict):
        raise AcademicSourceError("the run start did not return a run object")
    return cast("dict[str, Any]", run)


def abort_run(run_id: str, token: str, *, transport: Transport) -> None:
    endpoint = f"{API_BASE_URL}/actor-runs/{run_id}/abort?" + urlencode({"token": token})
    transport(endpoint, method="POST", payload={})


def poll_run(
    run_id: str,
    token: str,
    *,
    transport: Transport,
    timeout_seconds: int = TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Poll to a terminal status. When harrier's own wait ends first, the run
    is aborted on Apify before the timeout is reported, so it stops charging."""
    deadline = clock() + timeout_seconds
    endpoint = f"{API_BASE_URL}/actor-runs/{run_id}?" + urlencode({"token": token})
    while True:
        run = unwrap_apify_data(transport(endpoint))
        if not isinstance(run, dict):
            raise AcademicSourceError("a run poll did not return a run object")
        typed = cast("dict[str, Any]", run)
        status = str(typed.get("status") or "UNKNOWN")
        if status in TERMINAL_STATUSES:
            return typed
        if clock() >= deadline:
            abort_run(run_id, token, transport=transport)
            logger.warning("%s: run %s aborted after harrier's wait ended", LOG_LABEL, run_id)
            raise AcademicSourceError(f"run {run_id} did not finish in time and was aborted")
        sleep(POLL_INTERVAL_SECONDS)


def fetch_items(dataset_id: str, token: str, *, transport: Transport) -> list[dict[str, Any]]:
    endpoint = f"{API_BASE_URL}/datasets/{dataset_id}/items?" + urlencode(
        {"token": token, "clean": "true", "format": "json"}
    )
    data = unwrap_apify_data(transport(endpoint))
    if not isinstance(data, list):
        raise AcademicSourceError("the dataset read did not return a list")
    return [
        cast("dict[str, Any]", item)
        for item in cast("list[object]", data)
        if isinstance(item, dict)
    ]


def _finish(
    run: dict[str, Any], token: str, *, max_charge_usd: float, transport: Transport
) -> ActorRun:
    run_id = str(run.get("id") or "")
    status = str(run.get("status") or "UNKNOWN")
    counts, total, differs = charged_total(run)
    at_ceiling = total is not None and total >= max_charge_usd - PRICE_PER_RESULT_USD
    outcome = ActorRun(
        run_id=run_id,
        status=status,
        dataset_id=str(run.get("defaultDatasetId") or ""),
        charged_events=counts,
        charged_usd=total,
        pricing_differs=differs,
        stopped_at_ceiling=at_ceiling,
    )
    logger.info("%s: run %s ended %s", LOG_LABEL, run_id, status)
    # A run Apify stopped at the charge ceiling is read whatever its status;
    # any other run that did not succeed leaves nothing to screen.
    if status != "SUCCEEDED" and not at_ceiling:
        raise AcademicSourceError(f"run {run_id} ended {status}")
    if not outcome.dataset_id:
        raise AcademicSourceError(f"run {run_id} has no dataset")
    outcome.items = fetch_items(outcome.dataset_id, token, transport=transport)
    logger.info("%s: run %s returned %d items", LOG_LABEL, run_id, len(outcome.items))
    return outcome


def run_actor(
    compiled: dict[str, object],
    *,
    max_results: int,
    max_charge_usd: float,
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ActorRun:
    """Start the actor, wait for it, and read its dataset."""
    token = os.getenv(TOKEN_ENV, "").strip()
    actor = os.getenv(ACTOR_ENV, "").strip()
    if not token:
        raise AcademicSourceError(f"missing {TOKEN_ENV}")
    if not actor:
        raise AcademicSourceError(f"missing {ACTOR_ENV}")
    send = transport if transport is not None else _default_transport
    started = start_run(
        token,
        actor,
        compiled,
        max_results=max_results,
        max_charge_usd=max_charge_usd,
        transport=send,
    )
    run_id = str(started.get("id") or "").strip()
    if not run_id:
        raise AcademicSourceError("the run start did not include a run id")
    logger.info("%s: run %s started", LOG_LABEL, run_id)
    final = poll_run(run_id, token, transport=send, sleep=sleep, clock=clock)
    return _finish(final, token, max_charge_usd=max_charge_usd, transport=send)


def read_existing_run(
    run_id: str, *, max_charge_usd: float, transport: Transport | None = None
) -> ActorRun:
    """An existing run's dataset, without starting a new run."""
    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        raise AcademicSourceError(f"missing {TOKEN_ENV}")
    send = transport if transport is not None else _default_transport
    endpoint = f"{API_BASE_URL}/actor-runs/{run_id}?" + urlencode({"token": token})
    try:
        run = unwrap_apify_data(send(endpoint))
    except RuntimeError as exc:
        raise AcademicSourceError(
            f"run {run_id} could not be read; Apify deletes unnamed datasets after "
            "its retention period"
        ) from exc
    if not isinstance(run, dict):
        raise AcademicSourceError(f"run {run_id} could not be read")
    return _finish(
        cast("dict[str, Any]", run), token, max_charge_usd=max_charge_usd, transport=send
    )


def read_dataset_files(paths: list[str]) -> list[dict[str, Any]]:
    """Saved datasets, replayed without a request (spec 009's mode)."""
    return load_dataset_files(paths)


# --- deadlines ---------------------------------------------------------------

_MONTHS: dict[str, int] = {
    name: index
    for index, names in enumerate(
        (
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}
_ISO = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_NUMERIC = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})")
_DAY_MONTH = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\.?\s+([a-z]+)\.?,?\s+(\d{4})")
_MONTH_DAY = re.compile(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
# "1-15 June 2026": a day range before a month name. The dashes are the
# hyphen, the en dash and the em dash, written as escapes.
_DAY_RANGE = re.compile("\\b\\d{1,2}\\s*[-\u2013\u2014]\\s*\\d{1,2}\\s+[a-z]")


def _real(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_deadline(raw: object) -> tuple[str, bool]:
    """A deadline as an ISO date, or empty; and whether text was there but
    could not be read.

    Conservative on purpose (spec 097): an ISO date, a date with a written
    month name, and a numeric date whose order is unambiguous are converted.
    A numeric date whose day and month could be swapped, a range, or text
    without a date is not, because a wrong guess can move an open deadline
    into the past. A time or zone after the date is dropped.
    """
    if raw is None:
        return "", False
    text = str(raw).strip().lower()
    if not text:
        return "", False
    found: list[str] = []
    for match in _ISO.finditer(text):
        found.append(_real(int(match[1]), int(match[2]), int(match[3])) or "")
    for match in _NUMERIC.finditer(text):
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        if first > 12 and second <= 12:
            found.append(_real(year, second, first) or "")
        elif (second > 12 and first <= 12) or first == second:
            found.append(_real(year, first, second) or "")
        else:
            found.append("")
    for match in _DAY_MONTH.finditer(text):
        month = _MONTHS.get(match[2])
        if month is not None:
            found.append(_real(int(match[3]), month, int(match[1])) or "")
    for match in _MONTH_DAY.finditer(text):
        month = _MONTHS.get(match[1])
        if month is not None:
            found.append(_real(int(match[3]), month, int(match[2])) or "")
    distinct = set(found)
    if len(distinct) != 1 or "" in distinct or _DAY_RANGE.search(text):
        return "", True
    return distinct.pop(), False


# --- normalization -------------------------------------------------------------


def _field(item: dict[str, Any], name: str) -> str:
    source_field = FIELD_MAP.get(name)
    if source_field is None:
        return ""
    value: object = item.get(source_field)
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(part).strip() for part in cast("list[object]", value) if part)
    return str(value).strip()


@dataclass
class Normalized:
    jobs: list[NormalizedJob] = field(default_factory=list[NormalizedJob])
    missing_title_or_url: int = 0
    deadline_unreadable: int = 0
    without_posting_date: int = 0


def normalize_items(items: list[dict[str, Any]]) -> Normalized:
    """Each item through `FIELD_MAP` into the shared job shape. An item with
    no title or no URL is skipped and counted; it is never half-filled."""
    result = Normalized()
    for item in items:
        title = _field(item, "title")
        url = _field(item, "url")
        if not title or not url:
            result.missing_title_or_url += 1
            continue
        raw_deadline = item.get(FIELD_MAP["deadline"] or "")
        deadline, unreadable = parse_deadline(raw_deadline)
        if unreadable:
            result.deadline_unreadable += 1
        posted = _field(item, "posted_at")
        if not posted:
            result.without_posting_date += 1
        organisation = _field(item, "organisation")
        location = ", ".join(
            part for part in (_field(item, "city"), _field(item, "country")) if part
        )
        metadata: dict[str, object] = {
            name: (_field(item, name) or NOT_STATED) for name in COMPONENTS
        }
        metadata["organisation"] = organisation
        metadata["department"] = _field(item, "department")
        if unreadable:
            metadata["deadline_text"] = str(raw_deadline).strip()
        job = make_normalized_job(
            source=SOURCE_NAME,
            company=organisation,
            title=title,
            location=location,
            url=url,
            description=_field(item, "description"),
            created_at=posted,
            external_id=_field(item, "external_id"),
            board_key=normalize(organisation or SOURCE_NAME),
            metadata=metadata,
            raw_payload=item,
            deadline=deadline,
        )
        result.jobs.append(job)
    return result
