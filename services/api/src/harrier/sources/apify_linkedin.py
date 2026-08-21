"""Apify LinkedIn importer (spec 009 port). Ingestion only.

The only paid source. Cost discipline is structural: search URLs carry a
24h window (config comment), the dataset-file mode replays downloaded runs
without re-billing, and the run path caches every fetched description
(harrier.screening.descriptions.cache_job_descriptions, wired by spec 011).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from harrier.screening.normalized import NormalizedJob, make_normalized_job, normalize
from harrier.sources.feeds import read_line_config

logger = logging.getLogger(__name__)

SOURCE_NAME = "apify_linkedin"
API_BASE_URL = "https://api.apify.com/v2"
DEFAULT_ACTOR = "curious_coder/linkedin-jobs-scraper"
DEFAULT_COUNT = 150
DEFAULT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
SEARCH_URLS_PATH = Path("config") / "linkedin_search_urls.txt"
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})


class ApifyConfigError(RuntimeError):
    pass


def env_config() -> dict[str, str]:
    return {
        "token": os.getenv("APIFY_TOKEN", "").strip(),
        "actor": os.getenv("APIFY_LINKEDIN_ACTOR", DEFAULT_ACTOR).strip() or DEFAULT_ACTOR,
    }


def actor_path(actor: str) -> str:
    return actor.replace("/", "~")


def load_search_urls(path: Path | None = None) -> list[str]:
    search_path = path if path is not None else SEARCH_URLS_PATH
    urls = read_line_config(search_path)
    if not urls:
        raise ApifyConfigError(f"no LinkedIn search URLs configured in {search_path}")
    return urls


def actor_input(urls: list[str], count: int, scrape_company: bool) -> dict[str, object]:
    return {
        "urls": urls,
        "scrapeCompany": scrape_company,
        "count": count,
        "splitByLocation": False,
    }


def unwrap_apify_data(payload: object) -> object:
    if isinstance(payload, dict) and "data" in payload:
        inner: object = cast("dict[str, Any]", payload)["data"]
        return inner
    return cast("object", payload)


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout_seconds: int = 60,
    retries: int = 3,
) -> object:
    # The token rides in the query string per the Apify API; never log the
    # URL (exception messages carry only the error, not the endpoint).
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    last_error: Exception | None = None
    per_request_timeout = max(30, min(timeout_seconds, 300))
    for attempt in range(1, retries + 1):
        try:
            with urlopen(request, timeout=per_request_timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
            return json.loads(body)
        except (TimeoutError, URLError, HTTPError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            logger.warning("Apify request retry %d/%d after error: %s", attempt, retries - 1, exc)
            time.sleep(min(2 * attempt, 5))
    raise RuntimeError(f"Apify request failed after {retries} attempts: {last_error}")


def start_apify_run(
    urls: list[str],
    token: str,
    actor: str,
    count: int,
    scrape_company: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    endpoint = f"{API_BASE_URL}/acts/{actor_path(actor)}/runs?" + urlencode({"token": token})
    logger.info(
        "Apify run start: actor=%s urls=%d count=%d scrapeCompany=%s",
        actor,
        len(urls),
        count,
        scrape_company,
    )
    run = unwrap_apify_data(
        request_json(
            endpoint,
            method="POST",
            payload=actor_input(urls, count, scrape_company),
            timeout_seconds=timeout_seconds,
        )
    )
    if not isinstance(run, dict):
        raise RuntimeError("Apify run start did not return a run object")
    return cast("dict[str, Any]", run)


def poll_apify_run(run_id: str, token: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    endpoint = f"{API_BASE_URL}/actor-runs/{run_id}?" + urlencode({"token": token})
    while True:
        run = unwrap_apify_data(request_json(endpoint, timeout_seconds=min(timeout_seconds, 60)))
        if not isinstance(run, dict):
            raise RuntimeError("Apify run poll did not return a run object")
        typed = cast("dict[str, Any]", run)
        status = str(typed.get("status") or "UNKNOWN")
        logger.info("Apify polling: run_id=%s status=%s", run_id, status)
        if status in TERMINAL_STATUSES:
            return typed
        if time.time() >= deadline:
            raise TimeoutError(f"Apify run timed out after {timeout_seconds} seconds: {run_id}")
        time.sleep(POLL_INTERVAL_SECONDS)


def fetch_dataset_items(dataset_id: str, token: str, timeout_seconds: int) -> list[dict[str, Any]]:
    endpoint = f"{API_BASE_URL}/datasets/{dataset_id}/items?" + urlencode(
        {"token": token, "clean": "true", "format": "json"}
    )
    logger.info("Apify dataset fetch start: dataset_id=%s", dataset_id)
    data = unwrap_apify_data(request_json(endpoint, timeout_seconds=timeout_seconds))
    if not isinstance(data, list):
        raise RuntimeError("Apify dataset fetch did not return a list")
    items = [
        cast("dict[str, Any]", item)
        for item in cast("list[object]", data)
        if isinstance(item, dict)
    ]
    logger.info("Apify dataset fetch complete: dataset_id=%s items=%d", dataset_id, len(items))
    return items


def apify_request(
    urls: list[str],
    token: str,
    actor: str,
    count: int,
    scrape_company: bool,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    run = start_apify_run(urls, token, actor, count, scrape_company, timeout_seconds)
    run_id = str(run.get("id") or "").strip()
    dataset_id = str(run.get("defaultDatasetId") or "").strip()
    if not run_id:
        raise RuntimeError("Apify run start response did not include a run id")
    logger.info("Apify run started: run_id=%s", run_id)
    final_run = poll_apify_run(run_id, token, timeout_seconds)
    final_status = str(final_run.get("status") or "UNKNOWN")
    if final_status != "SUCCEEDED":
        status_message = str(
            final_run.get("statusMessage") or final_run.get("status_message") or ""
        )
        detail = f"run_id={run_id} status={final_status} {status_message}".strip()
        raise RuntimeError(f"Apify run did not succeed: {detail}")
    dataset_id = str(final_run.get("defaultDatasetId") or dataset_id).strip()
    if not dataset_id:
        raise RuntimeError(f"Apify run succeeded without a dataset id: run_id={run_id}")
    return fetch_dataset_items(dataset_id, token, timeout_seconds)


# The workplace declaration on the item, normalized to display labels. LinkedIn
# retired the query-level workplace filter (f_WT: its AI search converts it to
# keywords), so this per-item field is the only workplace fact the actor
# delivers (spec 053).
WORKPLACE_TYPE_LABELS: dict[str, str] = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "onsite": "On-site",
}


def workplace_declaration(item: dict[str, Any]) -> list[str]:
    """The workplace types the item declares, in declaration order.

    Unrecognized values are ignored rather than guessed at. workRemoteAllowed
    is consulted only when no recognized type exists, and only true counts:
    false is what the scraper reports for hybrid, on-site, and missing data
    alike, so it cannot name which one.
    """
    raw = cast("object", item.get("workplaceTypes"))
    if isinstance(raw, str):
        entries: list[object] = [raw]
    elif isinstance(raw, list):
        entries = cast("list[object]", raw)
    else:
        entries = []
    labels: list[str] = []
    for entry in entries:
        label = WORKPLACE_TYPE_LABELS.get(str(entry).strip().lower())
        if label and label not in labels:
            labels.append(label)
    if not labels and item.get("workRemoteAllowed") is True:
        labels.append("Remote")
    return labels


def normalize_apify_job(item: dict[str, Any]) -> NormalizedJob:
    title = str(item.get("Title") or item.get("title") or "").strip()
    description = str(
        item.get("Description") or item.get("description") or item.get("descriptionText") or ""
    ).strip()
    url = str(
        item.get("Detail URL") or item.get("detailUrl") or item.get("link") or item.get("url") or ""
    ).strip()
    location = str(item.get("Location") or item.get("location") or "").strip()
    # Declared types ride in the location string (the lever.py precedent from
    # spec 032), joined with the alternative separator so a posting offered as
    # remote or hybrid still qualifies on its remote alternative. The shared
    # location gate does the rejecting; this stays ingestion (spec 053).
    declared = workplace_declaration(item)
    if declared:
        prefix = " | ".join(declared)
        location = f"{prefix}, {location}" if location else prefix
    company = str(
        item.get("Company Name") or item.get("companyName") or item.get("company") or ""
    ).strip()
    posted_at = str(item.get("Created At") or item.get("createdAt") or "").strip()
    job_id = str(item.get("jobId") or item.get("job_id") or item.get("id") or "").strip()
    return make_normalized_job(
        source=SOURCE_NAME,
        company=company,
        title=title,
        location=location,
        url=url,
        description=description,
        created_at=posted_at,
        external_id=job_id,
        remote_signal="linkedin_search",
        board_key=normalize(company or "linkedin"),
        metadata={"source_type": "apify"},
        raw_payload=item,
    )


def _dict_items(raw: object, path: Path) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise RuntimeError(f"dataset file did not contain a list of items: {path}")
    return [
        cast("dict[str, Any]", item) for item in cast("list[object]", raw) if isinstance(item, dict)
    ]


def load_dataset_files(paths: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise RuntimeError(f"dataset file not found: {path}")
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        data = unwrap_apify_data(payload)
        if isinstance(data, list):
            file_items = _dict_items(cast("list[object]", data), path)
        elif isinstance(data, dict):
            typed = cast("dict[str, Any]", data)
            file_items = _dict_items(typed.get("items") or typed.get("results") or [], path)
        else:
            raise RuntimeError(f"unsupported dataset file payload: {path}")
        logger.info("loaded local Apify dataset: file=%s items=%d", path, len(file_items))
        items.extend(file_items)
    return items


def fetch_apify_linkedin_jobs(
    *,
    search_urls: list[str] | None = None,
    dataset_files: list[str] | None = None,
    count: int = DEFAULT_COUNT,
    scrape_company: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[NormalizedJob]:
    """Fetch and normalize, from a live actor run or downloaded dataset files.

    Screening, description caching, summaries, and notify are the run path's
    job (spec 011); this function is ingestion only."""
    files = dataset_files or []
    if files:
        raw_items = load_dataset_files(files)
    else:
        config = env_config()
        if not config["token"]:
            raise ApifyConfigError("missing APIFY_TOKEN")
        urls = search_urls if search_urls else load_search_urls()
        raw_items = apify_request(
            urls, config["token"], config["actor"], count, scrape_company, timeout_seconds
        )
    return [normalize_apify_job(item) for item in raw_items]
