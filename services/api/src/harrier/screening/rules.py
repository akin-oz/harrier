"""Screening policy: title rules, remote/EMEA enforcement, scoring (spec 007).

Faithful port of the old repo's scripts/job_sources.py policy layer. The
comments on the hint sets are load-bearing history: they record false
positives that were fought and fixed; do not "clean them up".
"""

from __future__ import annotations

import re
from typing import Any, cast

from harrier.screening.normalized import NormalizedJob, normalize

# The candidate config is user-authored JSON (profile store or the committed
# example); access is funneled through the typed helpers at the bottom.
CandidateConfig = dict[str, Any]

EXCLUDED_TITLE_HINTS: frozenset[str] = frozenset(
    {
        "manager",
        "head of",
        "director",
        "recruiter",
        "designer",
        "sales",
        "support",
        "mobile",
        "android",
        "ios",
        "react native",
        "qa",
        "sdet",
        "data engineer",
        "data scientist",
        "devops",
        "sre",
        "security engineer",
        "machine learning",
        "ml engineer",
    }
)

REMOTE_NEGATIVE_HINTS: frozenset[str] = frozenset(
    {
        "hybrid",
        "on-site",
        "onsite",
        "in office",
        "in-office",
        "office required",
        "must be in office",
        "relocation",
        "must be based",
        # "office" removed: matches "Remote (Home Office)", a valid remote location
        # "flex" removed: matches "flex remote" / "flexible remote", valid remote signals
    }
)

REGION_NEGATIVE_HINTS: frozenset[str] = frozenset(
    {
        "united states",
        "usa",
        "us only",
        "canada",
        "north america",
        "americas",
        "new york",
        "san francisco",
        "boston",
        "austin",
    }
)
# NOTE: EU work permit / EU residency / EU citizenship phrases are intentionally
# NOT in any hard-rejection list. The candidate operates through an Estonian OU
# (e-Residency), so an EU legal entity satisfies "EU work permit required" and
# "EU-based contractor" requirements via B2B. See PREFERRED_SIGNAL_WEIGHTS:
# those phrases are POSITIVE signals.

REMOTE_POSITIVE_PATTERNS: tuple[str, ...] = (
    r"\bremote\b",
    r"\bdistributed\b",
    r"\bwork from home\b",
)

PREFERRED_REGION_PATTERNS: tuple[str, ...] = (
    r"\beurope\b",
    r"\bemea\b",
    r"\beu\b",
    r"\bcet\b",
    r"\bcest\b",
    r"europe time zones?",
    r"worldwide.*3 hours cet",
    r"\bworldwide\b",
    r"\bglobal\b",
    r"\banywhere\b",
    r"\bremote[- ]first\b",
    r"\bgmt\b",
    r"\butc\b",
    r"\bturkey\b",
    r"\btürkiye\b",
    r"\bmiddle east\b",
)

SKILL_SIGNALS: dict[str, int] = {
    "typescript": 7,
    "vue": 7,
    "nuxt": 6,
    "react": 7,
    "next.js": 5,
    "frontend": 5,
    "front end": 5,
    "fullstack": 4,
    "full stack": 4,
    "product engineer": 6,
    "node": 4,
}

PREFERRED_SIGNAL_WEIGHTS: dict[str, int] = {
    "ownership": 4,
    "testing": 4,
    "ci/cd": 4,
    "architectural influence": 4,
    "strong engineering culture": 3,
    "observability": 4,
    "performance": 4,
    # EU contractor signals: the Estonian OU makes these an exact fit.
    "eu work permit": 5,
    "right to work in eu": 5,
    "eu-based contractor": 5,
    "eu entity": 5,
    "based in eu": 4,
}

DEFAULT_SCORING: dict[str, object] = {
    "base_score": 30,
    "exact_title_bonus": 20,
    "include_keyword_bonus": 6,
    "include_keyword_bonus_cap": 18,
    "remote_bonus": 10,
    "preferred_region_bonus": 8,
    "skill_signals": SKILL_SIGNALS,
    "preferred_signal_weights": PREFERRED_SIGNAL_WEIGHTS,
}

DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "devtools": [
        "developer tools",
        "devtools",
        "dev tools",
        "developer platform",
        "developer experience",
        "dx",
    ],
    "productivity": ["productivity", "collaboration", "project management", "workflow"],
    "fintech": ["fintech", "financial", "payments", "banking", "trading"],
    "ai platform": [
        "ai platform",
        "machine learning platform",
        "ml platform",
        "artificial intelligence",
    ],
    "clean energy": ["clean energy", "renewable", "sustainability", "carbon", "climate"],
    "health tech": ["healthtech", "health tech", "healthcare", "medical", "biotech"],
}

SCORE_CUTOFF = 55


def text_matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _cfg_dict(cfg: CandidateConfig, key: str) -> dict[str, Any]:
    raw = cfg.get(key)
    return cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}


def _str_list(container: dict[str, Any], key: str) -> list[str]:
    raw = container.get(key)
    if not isinstance(raw, list):
        return []
    return [str(item) for item in cast("list[object]", raw)]


def title_allowed(title: str, candidate_cfg: CandidateConfig) -> bool:
    title_norm = normalize(title)
    if any(token in title_norm for token in EXCLUDED_TITLE_HINTS):
        return False
    targets = _cfg_dict(candidate_cfg, "targets")
    exact_titles = [normalize(item) for item in _str_list(targets, "titles")]
    include = [normalize(item) for item in _str_list(targets, "title_keywords_include")]
    exclude = [normalize(item) for item in _str_list(targets, "title_keywords_exclude")]
    if any(token in title_norm for token in exclude):
        return False
    if any(exact == title_norm for exact in exact_titles):
        return True
    return sum(1 for token in include if token and token in title_norm) >= 1


def is_target_title_variant(title: str, exact_titles: list[str]) -> bool:
    if title in exact_titles:
        return True
    for exact_title in exact_titles:
        if not title.startswith(exact_title):
            continue
        suffix = title[len(exact_title) :].strip(" -()")
        if suffix and text_matches_any_pattern(
            suffix, REMOTE_POSITIVE_PATTERNS + PREFERRED_REGION_PATTERNS
        ):
            return True
    return False


def remote_region_allowed(job: NormalizedJob, candidate_cfg: CandidateConfig) -> tuple[bool, str]:
    title = normalize(job["title"])
    location = normalize(job["location"])
    description = normalize(job["description"])
    combined = f"{title} {location} {description}".strip()

    if any(token in location for token in REMOTE_NEGATIVE_HINTS):
        return False, "location says hybrid/on-site"

    # Only check non-EMEA region signals in title+location, not description.
    # A job description may mention US offices while the role is EMEA-remote.
    title_location = f"{title} {location}".strip()
    if any(token in title_location for token in REGION_NEGATIVE_HINTS):
        return False, "region looks non-EMEA"

    if not text_matches_any_pattern(combined, REMOTE_POSITIVE_PATTERNS):
        return False, "remote signal missing"

    # LinkedIn jobs come from EMEA-scoped search URLs; the region filter is
    # applied at query level, so "Remote" without a region tag is valid.
    if job["remote_signal"] == "linkedin_search":
        return True, "linkedin remote-filtered search result"

    candidate = _cfg_dict(candidate_cfg, "candidate")
    preferred_regions = [normalize(item) for item in _str_list(candidate, "preferred_regions")]
    preferred_countries = [normalize(item) for item in _str_list(candidate, "preferred_countries")]
    allowed_tokens = [token for token in preferred_regions + preferred_countries if token]
    has_preferred_region_signal = any(
        token in combined for token in allowed_tokens
    ) or text_matches_any_pattern(combined, PREFERRED_REGION_PATTERNS)
    if allowed_tokens and not has_preferred_region_signal:
        return False, "preferred region missing"

    return True, "remote-only Europe/EMEA signal present"


def scoring_config(candidate_cfg: CandidateConfig) -> dict[str, Any]:
    raw = _cfg_dict(candidate_cfg, "scoring")

    def _int(key: str) -> int:
        return int(raw.get(key, DEFAULT_SCORING[key]))

    config: dict[str, Any] = {
        "base_score": _int("base_score"),
        "exact_title_bonus": _int("exact_title_bonus"),
        "include_keyword_bonus": _int("include_keyword_bonus"),
        "include_keyword_bonus_cap": _int("include_keyword_bonus_cap"),
        "remote_bonus": _int("remote_bonus"),
        "preferred_region_bonus": _int("preferred_region_bonus"),
        "skill_signals": dict(SKILL_SIGNALS),
        "preferred_signal_weights": dict(PREFERRED_SIGNAL_WEIGHTS),
    }
    skill_signals = raw.get("skill_signals")
    if isinstance(skill_signals, dict):
        signals = cast("dict[str, Any]", skill_signals)
        cast("dict[str, int]", config["skill_signals"]).update(
            {str(key): int(value) for key, value in signals.items()}
        )
    preferred = raw.get("preferred_signal_weights")
    if isinstance(preferred, dict):
        weights = cast("dict[str, Any]", preferred)
        cast("dict[str, int]", config["preferred_signal_weights"]).update(
            {str(key): int(value) for key, value in weights.items()}
        )
    return config


def score_job(job: NormalizedJob, candidate_cfg: CandidateConfig) -> tuple[int, list[str]]:
    title = normalize(job["title"])
    text = normalize(f"{job['title']} {job['description']}")
    scoring_text = normalize(f"{job['title']} {job['location']} {job['description']}")
    targets = _cfg_dict(candidate_cfg, "targets")
    exact_titles = [normalize(item) for item in _str_list(targets, "titles")]
    scoring = scoring_config(candidate_cfg)
    score = int(scoring["base_score"])
    reasons: list[str] = []

    if is_target_title_variant(title, exact_titles):
        score += int(scoring["exact_title_bonus"])
        reasons.append("exact target title")

    include_tokens = [normalize(item) for item in _str_list(targets, "title_keywords_include")]
    matched_include = [token for token in include_tokens if token and token in text]
    if matched_include:
        score += min(
            int(scoring["include_keyword_bonus_cap"]),
            len(matched_include) * int(scoring["include_keyword_bonus"]),
        )
        reasons.append("title/stack keywords=" + ",".join(matched_include[:4]))

    for token, weight in cast("dict[str, int]", scoring["skill_signals"]).items():
        if token in text:
            score += weight

    for token, weight in cast("dict[str, int]", scoring["preferred_signal_weights"]).items():
        if token in text:
            score += weight

    if text_matches_any_pattern(scoring_text, REMOTE_POSITIVE_PATTERNS):
        score += int(scoring["remote_bonus"])
        reasons.append("explicit remote signal")
    if text_matches_any_pattern(scoring_text, PREFERRED_REGION_PATTERNS):
        score += int(scoring["preferred_region_bonus"])
        reasons.append("preferred region signal")

    # Domain preference bonus: highest single match wins, no stacking.
    domain_cfg = _cfg_dict(candidate_cfg, "scoring").get("domain_bonus", {})
    domain_cfg = cast("dict[str, Any]", domain_cfg) if isinstance(domain_cfg, dict) else {}
    primary_bonus = int(domain_cfg.get("primary", 5))
    secondary_bonus = int(domain_cfg.get("secondary", 3))
    prefs = _cfg_dict(candidate_cfg, "preferences")
    primary_domains = [normalize(d) for d in _str_list(prefs, "domains_preferred")]
    secondary_domains = [normalize(d) for d in _str_list(prefs, "domains_secondary")]
    domain_text = normalize(f"{job['company']} {job['title']} {job['description']}")
    best_domain_bonus = 0
    matched_domain = ""
    for domain in primary_domains:
        for keyword in DOMAIN_KEYWORDS.get(domain, []):
            if keyword in domain_text:
                best_domain_bonus = primary_bonus
                matched_domain = domain
                break
        if best_domain_bonus:
            break
    if not best_domain_bonus:
        for domain in secondary_domains:
            for keyword in DOMAIN_KEYWORDS.get(domain, []):
                if keyword in domain_text:
                    best_domain_bonus = secondary_bonus
                    matched_domain = domain
                    break
            if best_domain_bonus:
                break
    if best_domain_bonus:
        score += best_domain_bonus
        reasons.append(f"domain={matched_domain}")

    return min(score, 120), reasons
