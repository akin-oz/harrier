"""Staged contact discovery via Apify profile search (spec 016 port of
find_contacts.py).

The staging discipline: discovery writes candidates to a review artifact
under the data directory; only an explicit approval copies a candidate
into the contacts store. Nothing here writes a contact directly.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.outreach.contacts import upsert_contact
from harrier.sources.apify_linkedin import (
    API_BASE_URL,
    actor_path,
    fetch_dataset_items,
    poll_apify_run,
    request_json,
    unwrap_apify_data,
)

DEFAULT_ACTOR = "harvestapi/linkedin-profile-search"
DEFAULT_MAX_ITEMS = 10
DEFAULT_TIMEOUT_SECONDS = 480
DEFAULT_PROFILE_MODE = "Short"
FIT_RELEVANCE_ORDER = {
    "hiring_manager": 0,
    "eng_manager": 1,
    "recruiter": 2,
    "founder_cto": 3,
    "other": 4,
}
BEST_CONTACT_MIN_FIT_SCORE = 70
BEST_CONTACT_RELEVANCES = {"hiring_manager", "eng_manager", "recruiter", "founder_cto"}


def outreach_dir() -> Path:
    return data_dir() / "outreach"


def outreach_slug(company: str, role: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", f"{company}-{role}".lower()).strip("-")
    return re.sub(r"-{2,}", "-", base)


def env_config() -> dict[str, str]:
    return {
        "token": os.getenv("APIFY_TOKEN", "").strip(),
        "actor": os.getenv("APIFY_LINKEDIN_PROFILE_SEARCH_ACTOR", DEFAULT_ACTOR).strip()
        or DEFAULT_ACTOR,
    }


def role_focus_terms(role: str) -> list[str]:
    role_lower = role.lower()
    if "frontend" in role_lower or "web" in role_lower or "ui" in role_lower:
        return ["frontend engineering manager", "head of frontend", "web engineering manager"]
    if "product engineer" in role_lower or "product engineering" in role_lower:
        return ["product engineering manager", "head of product engineering", "engineering manager"]
    return ["engineering manager", "head of engineering", "talent acquisition"]


def looks_like_smaller_company(company: str) -> bool:
    text = company.strip().lower()
    if not text:
        return False
    large_company_hints = (
        "inc",
        "corp",
        "corporation",
        "systems",
        "technologies",
        "group",
        "holdings",
        "global",
        "international",
    )
    return len(text.split()) <= 2 and not any(token in text for token in large_company_hints)


def build_search_specs(company: str, role: str) -> list[dict[str, str]]:
    specs = [
        {"label": "recruiter", "query": f'"{company}" recruiter', "target_relevance": "recruiter"},
        {
            "label": "talent",
            "query": f'"{company}" "talent acquisition"',
            "target_relevance": "recruiter",
        },
    ]
    for index, focus in enumerate(role_focus_terms(role)[:2], start=1):
        specs.append(
            {
                "label": f"role_focus_{index}",
                "query": f'"{company}" "{focus}"',
                "target_relevance": "hiring_manager" if "head" in focus else "eng_manager",
            }
        )
    if looks_like_smaller_company(company):
        specs.append(
            {
                "label": "founder_cto",
                "query": f'"{company}" founder CTO',
                "target_relevance": "founder_cto",
            }
        )
    return specs


def build_best_contact_search_specs(company: str, role: str) -> list[dict[str, str]]:
    specs: list[dict[str, str]] = [
        {"label": "recruiter", "query": f'"{company}" recruiter', "target_relevance": "recruiter"}
    ]
    focus_terms = role_focus_terms(role)
    if focus_terms:
        first_focus = focus_terms[0]
        specs.append(
            {
                "label": "role_focus_1",
                "query": f'"{company}" "{first_focus}"',
                "target_relevance": "hiring_manager" if "head" in first_focus else "eng_manager",
            }
        )
    if looks_like_smaller_company(company):
        specs.append(
            {
                "label": "founder_cto",
                "query": f'"{company}" founder CTO',
                "target_relevance": "founder_cto",
            }
        )
    return specs


def apify_profile_search(
    query: str, token: str, actor: str, max_items: int, timeout_seconds: int
) -> list[dict[str, Any]]:
    endpoint = f"{API_BASE_URL}/acts/{actor_path(actor)}/runs?token={token}"
    payload: dict[str, object] = {
        "searchQuery": query,
        "profileScraperMode": DEFAULT_PROFILE_MODE,
        "takePages": 1,
        "maxItems": max(1, max_items),
    }
    run = unwrap_apify_data(
        request_json(endpoint, method="POST", payload=payload, timeout_seconds=timeout_seconds)
    )
    if not isinstance(run, dict):
        raise RuntimeError("Apify profile search did not return a run object")
    run_dict = cast("dict[str, Any]", run)
    run_id = str(run_dict.get("id") or "").strip()
    dataset_id = str(run_dict.get("defaultDatasetId") or "").strip()
    if not run_id:
        raise RuntimeError("Apify profile search response did not include a run id")
    final_run = poll_apify_run(run_id, token, timeout_seconds)
    if str(final_run.get("status") or "") != "SUCCEEDED":
        status_message = final_run.get("statusMessage") or ""
        detail = f"{final_run.get('status')} {status_message}".strip()
        raise RuntimeError(f"Apify profile search did not succeed: {detail}")
    dataset_id = str(final_run.get("defaultDatasetId") or dataset_id).strip()
    if not dataset_id:
        raise RuntimeError("Apify profile search succeeded without a dataset id")
    return fetch_dataset_items(dataset_id, token, timeout_seconds)


def normalize_location(value: object) -> str:
    if isinstance(value, dict):
        location = cast("dict[str, object]", value)
        parsed_raw = location.get("parsed")
        parsed = cast("dict[str, object]", parsed_raw) if isinstance(parsed_raw, dict) else {}
        return str(
            location.get("linkedinText") or location.get("text") or parsed.get("text") or ""
        ).strip()
    return str(value or "").strip()


def normalize_text(*parts: object) -> str:
    return " ".join(str(part or "").strip() for part in parts if str(part or "").strip()).strip()


def role_keywords(role: str) -> set[str]:
    role_lower = role.lower()
    keywords: set[str] = set()
    if any(token in role_lower for token in ("frontend", "web", "ui")):
        keywords.update({"frontend", "web", "ui"})
    if "product" in role_lower:
        keywords.update({"product", "product engineering"})
    if "engineer" in role_lower:
        keywords.add("engineering")
    return keywords


def infer_search_relevance(person_title: str, *, target_relevance: str) -> str:
    title = person_title.lower()
    if any(
        token in title
        for token in (
            "recruiter",
            "talent acquisition",
            "technical recruiter",
            "talent partner",
            "sourcer",
        )
    ):
        return "recruiter"
    if any(
        token in title
        for token in (
            "hr",
            "human resources",
            "people operations",
            "people ops",
            "hrbp",
            "people generalist",
        )
    ):
        return "other"
    if any(
        token in title
        for token in (
            "head of frontend",
            "frontend manager",
            "web manager",
            "product engineering manager",
            "hiring manager",
        )
    ):
        return "hiring_manager"
    if any(
        token in title
        for token in (
            "engineering manager",
            "head of engineering",
            "director of engineering",
            "vp engineering",
        )
    ):
        return "eng_manager"
    if any(
        token in title for token in ("founder", "co-founder", "cto", "chief technology officer")
    ):
        return "founder_cto"
    return target_relevance or "other"


def score_contact_fit(
    *,
    company: str,
    applied_job_title: str,
    person_title: str,
    location: str,
    snippet: str,
    current_company: str,
    target_relevance: str,
) -> dict[str, object]:
    title_text = person_title.lower()
    context_text = normalize_text(person_title, snippet, location, current_company).lower()
    relevance = infer_search_relevance(person_title, target_relevance=target_relevance)
    score = 20
    reasons: list[str] = []

    if current_company and current_company.strip().lower() == company.strip().lower():
        score += 30
        reasons.append("same company match")
    elif company.strip().lower() in context_text:
        score += 18
        reasons.append("likely company match")

    if relevance == "hiring_manager":
        score += 28
        reasons.append("likely hiring manager title")
    elif relevance == "eng_manager":
        score += 24
        reasons.append("engineering leadership title")
    elif relevance == "recruiter":
        score += 20
        reasons.append("direct recruiting title")
    elif relevance == "founder_cto":
        score += 14
        reasons.append("founder/CTO fallback")
    else:
        score += 6
        reasons.append("weak hiring signal")

    overlap_tokens = [token for token in role_keywords(applied_job_title) if token in context_text]
    if overlap_tokens:
        score += min(14, 4 * len(overlap_tokens))
        reasons.append(f"role/team relevance via {', '.join(sorted(overlap_tokens))}")

    if any(
        token in context_text
        for token in (
            "europe",
            "emea",
            "uk",
            "ireland",
            "portugal",
            "spain",
            "germany",
            "france",
            "netherlands",
            "remote",
        )
    ):
        score += 8
        reasons.append("region overlap")

    if (
        any(token in title_text for token in ("hr", "human resources", "people operations", "hrbp"))
        and relevance != "recruiter"
    ):
        score -= 18
        reasons.append("generic HR title")

    if relevance == "founder_cto" and not looks_like_smaller_company(company):
        score -= 10
        reasons.append("weak founder/CTO path for larger company")

    if "talent acquisition" in title_text or "technical recruiter" in title_text:
        score += 6
        reasons.append("specialized recruiting title")

    fit_score = max(0, min(100, score))
    return {
        "relevance": relevance,
        "fit_score": fit_score,
        "fit_reason": "; ".join(reasons[:4]) if reasons else "weak match",
    }


def normalize_profile_result(
    item: dict[str, Any], *, company: str, role: str, job_url: str, spec: dict[str, str]
) -> dict[str, str]:
    person_name = str(item.get("fullName") or item.get("name") or "").strip()
    if not person_name:
        first = str(item.get("firstName") or "").strip()
        last = str(item.get("lastName") or "").strip()
        person_name = " ".join(part for part in (first, last) if part).strip()
    linkedin_url = str(
        item.get("linkedinUrl") or item.get("profileUrl") or item.get("url") or ""
    ).strip()
    person_title = str(item.get("headline") or item.get("title") or "").strip()
    location = normalize_location(item.get("location"))
    snippet = str(item.get("snippet") or item.get("summary") or item.get("about") or "").strip()
    current_company = str(
        item.get("currentCompany") or item.get("companyName") or item.get("company") or ""
    ).strip()
    scored = score_contact_fit(
        company=company,
        applied_job_title=role,
        person_title=person_title,
        location=location,
        snippet=snippet,
        current_company=current_company,
        target_relevance=spec["target_relevance"],
    )
    return {
        "person_name": person_name,
        "company": company.strip(),
        "applied_job_title": role.strip(),
        "person_title": person_title,
        "relevance": str(scored["relevance"]),
        "fit_score": str(scored["fit_score"]),
        "fit_reason": str(scored["fit_reason"]),
        "location": location,
        "source": "apify_profile_search",
        "linkedin_url": linkedin_url,
        "job_url": job_url.strip(),
        "contact_status": "candidate",
        "reply_status": "",
        "review_status": "pending",
        "raw_query": spec["query"],
        "current_company": current_company,
        "snippet": snippet,
    }


def merge_ranked_contacts(candidates: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for candidate in candidates:
        key = (candidate.get("linkedin_url") or candidate.get("person_name") or "").strip().lower()
        if not key:
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(candidate)
            continue
        existing_score = int(existing.get("fit_score") or "0")
        new_score = int(candidate.get("fit_score") or "0")
        if new_score > existing_score:
            merged[key] = dict(candidate)
            existing = merged[key]
        existing_reason = (existing.get("fit_reason") or "").strip()
        candidate_reason = (candidate.get("fit_reason") or "").strip()
        if candidate_reason and candidate_reason not in existing_reason:
            existing["fit_reason"] = f"{existing_reason}; {candidate_reason}".strip("; ")
    return sorted(
        merged.values(),
        key=lambda row: (
            FIT_RELEVANCE_ORDER.get(row.get("relevance", ""), 99),
            -int(row.get("fit_score") or "0"),
            row.get("person_name", "").lower(),
        ),
    )


def has_strong_best_contact(candidates: list[dict[str, str]]) -> bool:
    for candidate in candidates:
        relevance = str(candidate.get("relevance") or "")
        fit_score = int(str(candidate.get("fit_score") or "0") or "0")
        if relevance in BEST_CONTACT_RELEVANCES and fit_score >= BEST_CONTACT_MIN_FIT_SCORE:
            return True
    return False


def candidates_output_path(company: str, role: str) -> Path:
    directory = outreach_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{outreach_slug(company, role)}-candidates.json"


def load_candidates_artifact(company: str, role: str) -> dict[str, object] | None:
    path = candidates_output_path(company, role)
    if not path.exists():
        return None
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else None


def write_candidates_artifact(company: str, role: str, payload: dict[str, object]) -> Path:
    path = candidates_output_path(company, role)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def staged_candidates(company: str, role: str) -> list[dict[str, str]]:
    """The candidates awaiting a decision, as approve and reject see them.

    The reader the Outreach page uses (spec 048). It goes through the same
    extraction `approve_candidate` and `update_candidate_review_status` use,
    so the page cannot show a candidate those two would not find, which is
    the shape of bug that would make an approval button silently do nothing.
    """
    payload = load_candidates_artifact(company, role)
    if payload is None:
        return []
    return _artifact_candidates(payload)


def _artifact_candidates(payload: dict[str, object]) -> list[dict[str, str]]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        return []
    return [
        cast("dict[str, str]", item)
        for item in cast("list[object]", candidates)
        if isinstance(item, dict)
    ]


def update_candidate_review_status(
    company: str, role: str, linkedin_url: str, review_status: str
) -> dict[str, str] | None:
    payload = load_candidates_artifact(company, role)
    if payload is None:
        return None
    for candidate in _artifact_candidates(payload):
        if str(candidate.get("linkedin_url") or "").strip().lower() == linkedin_url.strip().lower():
            candidate["review_status"] = review_status
            write_candidates_artifact(company, role, payload)
            return candidate
    return None


def approve_candidate(
    conn: sqlite3.Connection, company: str, role: str, job_url: str, linkedin_url: str
) -> dict[str, str] | None:
    """The only path from a staged candidate into the contacts store."""
    payload = load_candidates_artifact(company, role)
    if payload is None:
        return None
    for candidate in _artifact_candidates(payload):
        if str(candidate.get("linkedin_url") or "").strip().lower() != linkedin_url.strip().lower():
            continue
        # Persist the contact first; only a successful write marks the
        # artifact approved (review finding: a failed write must stay
        # retryable through the normal path).
        added = upsert_contact(
            conn,
            company=company,
            role=role,
            job_url=job_url,
            person_name=str(candidate.get("person_name") or ""),
            person_title=str(candidate.get("person_title") or ""),
            linkedin_url=str(candidate.get("linkedin_url") or ""),
            location=str(candidate.get("location") or ""),
            source=str(candidate.get("source") or "apify_profile_search"),
            relevance=str(candidate.get("relevance") or "other"),
            fit_score=str(candidate.get("fit_score") or ""),
            fit_reason=str(candidate.get("fit_reason") or ""),
            notes=f"staged_candidate_query={candidate.get('raw_query', '')}",
        )
        candidate["review_status"] = "approved"
        write_candidates_artifact(company, role, payload)
        return added
    return None


def _run_specs(
    specs: list[dict[str, str]],
    *,
    company: str,
    role: str,
    job_url: str,
    max_items: int,
    timeout_seconds: int,
    stop_on_strong: bool,
) -> tuple[list[dict[str, object]], list[dict[str, str]], int]:
    config = env_config()
    if not config["token"]:
        raise RuntimeError("missing APIFY_TOKEN")
    raw_results: list[dict[str, object]] = []
    normalized_candidates: list[dict[str, str]] = []
    searches_run = 0
    for spec in specs:
        searches_run += 1
        items = apify_profile_search(
            spec["query"], config["token"], config["actor"], max_items, timeout_seconds
        )
        raw_results.append({"spec": spec, "items_count": len(items), "items": items})
        for item in items:
            candidate = normalize_profile_result(
                item, company=company, role=role, job_url=job_url, spec=spec
            )
            if not candidate["person_name"] or not candidate["linkedin_url"]:
                continue
            normalized_candidates.append(candidate)
        if stop_on_strong and has_strong_best_contact(merge_ranked_contacts(normalized_candidates)):
            break
    return raw_results, normalized_candidates, searches_run


def _summarize(
    *,
    company: str,
    role: str,
    job_url: str,
    specs: list[dict[str, str]],
    raw_results: list[dict[str, object]],
    normalized_candidates: list[dict[str, str]],
    searches_run: int,
    max_items: int,
    search_mode: str = "",
) -> dict[str, object]:
    candidates = merge_ranked_contacts(normalized_candidates)
    search_results: list[dict[str, object]] = []
    for result in raw_results:
        spec_raw = result.get("spec")
        spec = cast("dict[str, str]", spec_raw) if isinstance(spec_raw, dict) else {}
        search_results.append(
            {
                "label": str(spec.get("label") or ""),
                "query": str(spec.get("query") or ""),
                "target_relevance": str(spec.get("target_relevance") or ""),
                "items_count": int(str(result.get("items_count") or 0)),
            }
        )
    artifact_payload: dict[str, object] = {
        "company": company,
        "role": role,
        "job_url": job_url,
        "actor": env_config()["actor"],
        "search_specs": specs,
        "search_results": search_results,
        "candidates": candidates,
        "raw_results": raw_results,
    }
    if search_mode:
        artifact_payload["search_mode"] = search_mode
    artifact_path = write_candidates_artifact(company, role, artifact_payload)
    summary: dict[str, object] = {
        "company": company,
        "role": role,
        "job_url": job_url,
        "search_specs": specs,
        "search_results": search_results,
        "searches_run": searches_run,
        "max_items_per_search": max_items,
        "raw_item_count": sum(int(str(item["items_count"])) for item in search_results),
        "candidate_count": len(candidates),
        "artifact_path": str(artifact_path),
        "candidates": candidates,
    }
    if search_mode:
        summary["search_mode"] = search_mode
    return summary


def find_contacts_for_job(
    *,
    company: str,
    role: str,
    job_url: str,
    max_items: int = DEFAULT_MAX_ITEMS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    specs = build_search_specs(company, role)
    raw_results, normalized_candidates, searches_run = _run_specs(
        specs,
        company=company,
        role=role,
        job_url=job_url,
        max_items=max_items,
        timeout_seconds=timeout_seconds,
        stop_on_strong=False,
    )
    return _summarize(
        company=company,
        role=role,
        job_url=job_url,
        specs=specs,
        raw_results=raw_results,
        normalized_candidates=normalized_candidates,
        searches_run=searches_run,
        max_items=max_items,
    )


def find_best_contacts_for_job(
    *,
    company: str,
    role: str,
    job_url: str,
    max_items: int = 4,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    specs = build_best_contact_search_specs(company, role)
    raw_results, normalized_candidates, searches_run = _run_specs(
        specs,
        company=company,
        role=role,
        job_url=job_url,
        max_items=max_items,
        timeout_seconds=timeout_seconds,
        stop_on_strong=True,
    )
    return _summarize(
        company=company,
        role=role,
        job_url=job_url,
        specs=specs,
        raw_results=raw_results,
        normalized_candidates=normalized_candidates,
        searches_run=searches_run,
        max_items=max_items,
        search_mode="best_contact_only",
    )
