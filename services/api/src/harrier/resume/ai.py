"""AI evidence reordering through the harrier.llm seam (spec 013 port).

The model ranks verified evidence IDs only; it never writes, rephrases,
or adds content. Every returned ID is validated against the pool and the
truth documents, and any failure returns None so the caller falls back to
the deterministic plan.
"""

from __future__ import annotations

import json
import logging
import re
from typing import cast

from harrier.llm import LLMClientError, generate_text
from harrier.resume.content import ResumeBundle, TruthSources

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TAILOR = """\
You rank verified resume evidence for a target job.

SOURCE OF TRUTH:
- Candidate source data and the verified bullet pool are factual truth.
- The job description is only a relevance signal. It is never a source of candidate facts.

HARD RULES:
1. Return only IDs from the supplied bullet_pool. Do not write, rephrase, infer, or add evidence.
2. Never infer a technology, responsibility, metric, domain, seniority, or professional identity from the job description.
3. Prefer the strongest supported evidence: measurable outcomes, scale, ownership, architecture decisions, production impact, reliability/observability, and technical leadership.
4. Do not select evidence that semantically duplicates a selected achievement. Different evidence types are more valuable than keyword repetition.
5. Exact keyword matching is weaker than strong relevant evidence. Do not select generic evidence merely because it mirrors JD wording.
6. A technology can be prioritized only when it appears in candidate evidence. Do not treat the most recent technology as the only or strongest technology.
7. Do not attempt to set the professional title, profile, skills order, or dates. Those are derived and validated by the application.

RETURN FORMAT:
Return strict JSON only. Each array is an ordered preference list; the application retains only its pre-approved, deduplicated evidence IDs.
{
  "role1_bullets": ["id", ...],
  "role2_bullets": ["id", ...],
  "role3_bullets": ["id", ...],
  "selected_achievements": ["id", ...]
}
"""


def _validate_bullet_ids(
    bundle: ResumeBundle,
    sources: TruthSources,
    ids: list[object],
    pool_prefix: str,
    min_count: int,
) -> list[str]:
    valid: list[str] = []
    for bullet_id in ids:
        if not isinstance(bullet_id, str) or not bullet_id.startswith(pool_prefix):
            continue
        text = bundle.bullet_pool.get(bullet_id)
        if not text:
            logger.warning("unknown bullet ID %r skipped", bullet_id)
            continue
        if sources.contains(text):
            valid.append(bullet_id)
        else:
            logger.warning("bullet %r failed truth validation; skipped", bullet_id)
    if len(valid) < min_count:
        logger.warning(
            "only %d valid bullets for pool %r (need %d); using fallback",
            len(valid),
            pool_prefix,
            min_count,
        )
        return []
    return valid


def build_ai_tailored_content(
    bundle: ResumeBundle,
    sources: TruthSources,
    jd_text: str,
    company: str,
    role: str,
    archetype: str | None = None,
) -> dict[str, list[str]] | None:
    """Ask the model for evidence orderings; None on any failure."""
    payload = {
        "company": company,
        "role": role,
        "job_description": jd_text[:6000],
        "bullet_pool": bundle.bullet_pool,
        "verified_facts": sources.truth_text,
        "archetype": archetype or "general",
    }
    try:
        output_text = generate_text(SYSTEM_PROMPT_TAILOR, json.dumps(payload, ensure_ascii=False))
    except LLMClientError as exc:
        logger.warning("AI tailoring request failed: %s", exc)
        return None
    if not output_text.strip():
        logger.warning("AI backend returned empty response")
        return None

    try:
        cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", output_text).strip()
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            raise ValueError("no JSON object found in response")
        data: object = json.loads(match.group())
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("failed to parse AI response: %s", exc)
        return None
    if not isinstance(data, dict):
        return None
    parsed = cast("dict[str, object]", data)

    result: dict[str, list[str]] = {}
    for position, role_entry in enumerate(bundle.roles, start=1):
        key = f"role{position}_bullets"
        raw = parsed.get(key)
        ids = cast("list[object]", raw) if isinstance(raw, list) else []
        minimum = min(3, role_entry.bullet_count)
        valid = _validate_bullet_ids(bundle, sources, ids, f"{role_entry.id}_", minimum)
        if valid:
            result[key] = valid
    raw_achievements = parsed.get("selected_achievements")
    achievement_ids = (
        cast("list[object]", raw_achievements) if isinstance(raw_achievements, list) else []
    )
    valid_achievements = _validate_bullet_ids(bundle, sources, achievement_ids, "ach_", 3)
    if valid_achievements:
        result["selected_achievements"] = valid_achievements

    if not result:
        logger.warning("AI tailoring produced no usable content; falling back")
        return None
    return result
