"""Hunter.io lookups (spec 016 port of hunter_lib.py).

Credit budget on the free plan: 50 credits/month. Domain search is the
best value (1 credit for up to 10 unverified emails); the finder costs 1
credit per verified email; verification costs 0.5. Stated change: the
key comes from HUNTER_API_KEY only (the old mcp.json fallback is
dropped; secrets never live in committed files).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import cast

BASE_URL = "https://api.hunter.io/v2"


def _api_key() -> str:
    return os.getenv("HUNTER_API_KEY", "").strip()


def _get(endpoint: str, params: dict[str, str]) -> dict[str, object]:
    url = f"{BASE_URL}/{endpoint}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            parsed: object = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        detail = body[:200]
        try:
            err: object = json.loads(body)
            if isinstance(err, dict):
                errors = cast("dict[str, object]", err).get("errors")
                if isinstance(errors, list) and errors:
                    first = cast("list[object]", errors)[0]
                    if isinstance(first, dict):
                        detail = str(cast("dict[str, object]", first).get("details", detail))
        except json.JSONDecodeError:
            pass
        raise RuntimeError(f"Hunter API {exc.code}: {detail}") from exc
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}


def _require_key(api_key: str) -> str:
    key = api_key or _api_key()
    if not key:
        raise RuntimeError("missing HUNTER_API_KEY")
    return key


def domain_search(
    domain: str,
    *,
    api_key: str = "",
    limit: int = 10,
    seniority: str = "",
    department: str = "",
) -> dict[str, object]:
    """1 credit total for up to limit people; emails are unverified."""
    key = _require_key(api_key)
    params: dict[str, str] = {"domain": domain, "api_key": key, "limit": str(limit)}
    if seniority:
        params["seniority"] = seniority
    if department:
        params["department"] = department
    result = _get("domain-search", params)
    data_raw = result.get("data")
    data = cast("dict[str, object]", data_raw) if isinstance(data_raw, dict) else {}
    meta_raw = result.get("meta")
    meta = cast("dict[str, object]", meta_raw) if isinstance(meta_raw, dict) else {}
    emails_raw = data.get("emails")
    emails = cast("list[object]", emails_raw) if isinstance(emails_raw, list) else []
    people: list[dict[str, object]] = []
    for raw in emails:
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, object]", raw)
        people.append(
            {
                "first_name": entry.get("first_name") or "",
                "last_name": entry.get("last_name") or "",
                "position": entry.get("position") or "",
                "seniority": entry.get("seniority") or "",
                "department": entry.get("department") or "",
                "type": entry.get("type") or "",
                "confidence": entry.get("confidence"),
                "email": entry.get("value") or "",
                "linkedin": entry.get("linkedin") or "",
            }
        )
    return {
        "organization": data.get("organization") or "",
        "domain": data.get("domain") or domain,
        "pattern": data.get("pattern") or "",
        "people": people,
        "total": meta.get("results", len(people)),
    }


def find_email(
    domain: str, first_name: str, last_name: str, *, api_key: str = ""
) -> dict[str, object]:
    """1 credit for one verified email; free when nothing is found."""
    key = _require_key(api_key)
    result = _get(
        "email-finder",
        {"domain": domain, "first_name": first_name, "last_name": last_name, "api_key": key},
    )
    data_raw = result.get("data")
    data = cast("dict[str, object]", data_raw) if isinstance(data_raw, dict) else {}
    verification_raw = data.get("verification")
    verification = (
        cast("dict[str, object]", verification_raw) if isinstance(verification_raw, dict) else {}
    )
    return {
        "email": data.get("email") or "",
        "score": data.get("score") or 0,
        "position": data.get("position") or "",
        "linkedin_url": data.get("linkedin_url") or "",
        "verification_status": verification.get("status", ""),
    }


def verify_email(email: str, *, api_key: str = "") -> dict[str, object]:
    """0.5 credit to verify a known address."""
    key = _require_key(api_key)
    result = _get("email-verifier", {"email": email, "api_key": key})
    data_raw = result.get("data")
    data = cast("dict[str, object]", data_raw) if isinstance(data_raw, dict) else {}
    return {
        "email": data.get("email") or email,
        "status": data.get("status") or "unknown",
        "score": data.get("score") or 0,
        "mx_records": data.get("mx_records", False),
        "smtp_check": data.get("smtp_check", False),
        "accept_all": data.get("accept_all", False),
        "disposable": data.get("disposable", False),
    }
