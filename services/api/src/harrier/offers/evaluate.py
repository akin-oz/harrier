"""The six-block offer evaluation (spec 015 port of evaluate_offer.py).

Stated change from the old code: the prompt header is assembled from the
resume bundle and candidate document, never hardcoded personal lines,
and the LLM call goes through harrier.llm.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from harrier.apply.profile import load_candidate_document
from harrier.db import data_dir
from harrier.llm import LLMClientError, generate_text
from harrier.offers.report import ARCHETYPES, build_report
from harrier.offers.stories import capture_stories, load_seed_stories
from harrier.resume.content import load_bundle, load_truth_sources
from harrier.resume.facts import professional_experience_label

logger = logging.getLogger(__name__)

VERDICTS = ("strong_apply", "apply", "borderline", "skip")

PROMPT_TEMPLATE = """\
You are a career strategy advisor preparing a structured offer evaluation for \
{name}, a {identity} with {experience} of experience.

Stack: {stack}.
Targets: {targets}.
Compensation target: EUR {salary_min}-{salary_target}/year.

You will receive: a job description, the candidate's verified experience facts, \
recent achievements, seed STAR stories, and the archetype taxonomy.

Return a single JSON object with exactly 6 keys: block_a, block_b, block_c, block_d, block_e, block_f.

block_a (object): archetype (one of the taxonomy keys), archetype_rationale (1 sentence), \
domain, seniority_match ("aligned" | "slightly below" | "slightly above" | "misaligned"), \
remote_confirmed (true | false), comp_estimate (from JD only, else "not disclosed"), \
tldr (2 sentences max).

block_b (array of 6-10 requirement objects, most critical first): jd_requirement, \
evidence (exact phrase from verified_facts, or null if gap), gap (true | false), \
mitigation (honest positioning if gap=true, else null).

block_c (object): selling_points (top 3 for this role), honest_gaps (2-3 max, each with \
gap and mitigation). Do not invent gaps that do not exist.

block_d (object): profile_angle (1-2 sentences, no filler), cv_adjustments (3-5 specific \
bullet reframings using JD vocabulary), headline_suggestion.

block_e (array of 4-6 story objects): story_id (prefer seed story ids; new ids only for \
JD themes not covered), theme, jd_hook, opening_line, star_r (situation, task, action, \
result, reflection).

block_f (object, final verdict): verdict ("strong_apply" | "apply" | "borderline" | "skip"), \
confidence (float 0.0-1.0), reason (one sentence), deal_breakers (list, may be empty).

Verdict guidance:
- "skip" = clearly not worth applying. Use only with at least one hard deal-breaker: \
not remote, not region-eligible, wrong seniority, wrong domain stack, or compensation \
explicitly disclosed below {comp_floor}k EUR.
- "borderline" = real concerns but not disqualifying; mixed signals worth a human review.
- "apply" = solid fit, no deal-breakers.
- "strong_apply" = exceptional fit on archetype, stack, and culture signals.
Set confidence >= 0.85 only when the deal-breaker is unambiguous from the JD text. \
If unsure, use "borderline" with lower confidence rather than "skip".

STRICT RULES:
1. block_b evidence must quote or closely paraphrase verified_facts. Do NOT invent experience.
2. block_e must prefer seed story IDs where applicable.
3. comp_estimate: JD data only; do not extrapolate.
4. No corporate filler. No invented skills. Direct, senior-level language.
5. block_f.verdict must be one of the four exact strings. Be conservative with "skip".
6. Return valid JSON only: no markdown fences, no prose outside the JSON object.
7. FORMATTING: Never use em dashes anywhere in the output. Use commas, semicolons, colons, or hyphens instead.
"""


@dataclass(frozen=True)
class Verdict:
    verdict: str
    confidence: float
    reason: str
    deal_breakers: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationResult:
    report_path: Path
    verdict: Verdict
    data: dict[str, object]


class EvaluationError(RuntimeError):
    pass


def reports_dir() -> Path:
    return data_dir() / "reports"


def slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return re.sub(r"-{2,}", "-", value)


def report_path_for(company: str, role: str) -> Path:
    return reports_dir() / f"{slugify(f'{company}-{role}')}.md"


def build_system_prompt(conn: sqlite3.Connection) -> str:
    """Assemble the persona header from data (stated change)."""
    bundle = load_bundle(conn)
    candidate = load_candidate_document(conn)
    compensation_raw = candidate.get("compensation")
    compensation = (
        cast("dict[str, object]", compensation_raw) if isinstance(compensation_raw, dict) else {}
    )
    salary_min = compensation.get("salary_min_eur", 0)
    salary_target = compensation.get("salary_target_eur", 0)
    salary_min_int = salary_min if isinstance(salary_min, int) else 0
    comp_floor = max(0, (salary_min_int - 10000) // 1000)
    targets_raw = candidate.get("targets")
    targets = cast("dict[str, object]", targets_raw) if isinstance(targets_raw, dict) else {}
    titles_raw = targets.get("titles")
    titles = (
        [str(item) for item in cast("list[object]", titles_raw)]
        if isinstance(titles_raw, list)
        else []
    )
    return PROMPT_TEMPLATE.format(
        name=bundle.name,
        identity=bundle.primary_identity,
        experience=professional_experience_label(bundle),
        stack=", ".join(bundle.all_skills),
        targets="; ".join(titles) if titles else "remote roles matching the candidate profile",
        salary_min=f"{salary_min:,}" if isinstance(salary_min, int) else str(salary_min),
        salary_target=f"{salary_target:,}"
        if isinstance(salary_target, int)
        else str(salary_target),
        comp_floor=comp_floor,
    )


def parse_json_response(text: str) -> dict[str, object] | None:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    if not match:
        return None
    try:
        parsed: object = json.loads(match.group())
    except json.JSONDecodeError as exc:
        logger.warning("JSON parse error: %s", exc)
        return None
    return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else None


def parse_verdict(data: dict[str, object]) -> Verdict:
    """The machine verdict contract: an invalid verdict becomes borderline
    and an invalid confidence becomes 0.0, so malformed output can never
    clear an auto-reject threshold."""
    block_raw = data.get("block_f")
    block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else {}
    verdict = str(block.get("verdict", ""))
    if verdict not in VERDICTS:
        verdict = "borderline"
    try:
        confidence = float(str(block.get("confidence", "")))
    except (TypeError, ValueError):
        confidence = 0.0
    if not 0.0 <= confidence <= 1.0:
        confidence = 0.0
    reason = str(block.get("reason", "")).replace("\n", " ").strip()
    deal_breakers_raw = block.get("deal_breakers")
    deal_breakers = (
        tuple(str(item) for item in cast("list[object]", deal_breakers_raw))
        if isinstance(deal_breakers_raw, list)
        else ()
    )
    return Verdict(
        verdict=verdict, confidence=confidence, reason=reason, deal_breakers=deal_breakers
    )


def evaluate_offer(
    conn: sqlite3.Connection,
    company: str,
    role: str,
    job_url: str,
    jd_text: str,
    output_dir: Path | None = None,
) -> EvaluationResult:
    if not jd_text.strip():
        raise EvaluationError("no job description available")
    sources = load_truth_sources(conn)
    payload = {
        "company": company,
        "role": role,
        "job_url": job_url,
        "job_description": jd_text[:8000],
        "verified_facts": sources.truth_text[:4000],
        "recent_achievements": sources.achievements_text[:2000],
        "seed_stories": load_seed_stories(conn),
        "archetype_taxonomy": ARCHETYPES,
    }
    try:
        raw = generate_text(build_system_prompt(conn), json.dumps(payload, ensure_ascii=False))
    except LLMClientError as exc:
        raise EvaluationError(f"AI request failed: {exc}") from exc
    if not raw.strip():
        raise EvaluationError("AI backend returned no content")
    data = parse_json_response(raw)
    if data is None:
        raise EvaluationError("failed to parse AI response as JSON")

    directory = output_dir if output_dir is not None else reports_dir()
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / f"{slugify(f'{company}-{role}')}.md"
    report_path.write_text(build_report(data, company, role, job_url), encoding="utf-8")

    stories_raw = data.get("block_e")
    stories = (
        [
            cast("dict[str, object]", item)
            for item in cast("list[object]", stories_raw)
            if isinstance(item, dict)
        ]
        if isinstance(stories_raw, list)
        else []
    )
    if stories:
        capture_stories(conn, stories, company, role)

    return EvaluationResult(report_path=report_path, verdict=parse_verdict(data), data=data)
