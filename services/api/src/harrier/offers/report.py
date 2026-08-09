"""The six-block evaluation report (spec 015 port of the old formatters).

Block F (the verdict) leads so the decision is visible without scrolling;
A through E follow in the old order.
"""

from __future__ import annotations

from datetime import date
from typing import cast

ARCHETYPES: dict[str, str] = {
    "design_system": "Design System / Component Platform",
    "product_engineer": "Product Engineer",
    "platform_frontend": "Platform / Developer Tooling Frontend",
    "performance_ux": "Performance & UX Excellence",
    "fullstack_light": "Fullstack-Light (Frontend + Node/API)",
    "general_frontend": "Senior Frontend Engineer",
}

VERDICT_BADGES = {
    "strong_apply": "STRONG APPLY",
    "apply": "APPLY",
    "borderline": "BORDERLINE",
    "skip": "SKIP",
}


def _as_dict(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _fmt_block_a(block: dict[str, object], company: str, role: str, job_url: str) -> str:
    archetype = str(block.get("archetype", ""))
    archetype_label = ARCHETYPES.get(archetype, archetype or "unknown")
    remote_str = "Yes" if block.get("remote_confirmed") else "No"
    return "\n".join(
        [
            "## A: Role Classification",
            "",
            "| Field | Value |",
            "|---|---|",
            f"| **Company** | {company} |",
            f"| **Role** | {role} |",
            f"| **Archetype** | {archetype_label} (`{archetype}`) |",
            f"| **Rationale** | {block.get('archetype_rationale', '')} |",
            f"| **Domain** | {block.get('domain', '')} |",
            f"| **Seniority Match** | {block.get('seniority_match', '')} |",
            f"| **Remote Confirmed** | {remote_str} |",
            f"| **Comp Estimate** | {block.get('comp_estimate', 'not disclosed')} |",
            f"| **URL** | {job_url} |",
            "",
            f"> **TLDR:** {block.get('tldr', '')}",
        ]
    )


def _fmt_block_b(requirements: list[object]) -> str:
    lines = ["## B: CV Match, JD Requirements to Experience", ""]
    for raw in requirements:
        item = _as_dict(raw)
        gap = bool(item.get("gap", False))
        marker = " [GAP]" if gap else " [OK]"
        lines.append(f"### {item.get('jd_requirement', '')}{marker}")
        evidence = item.get("evidence")
        if evidence:
            lines.append(f"**Evidence:** {evidence}")
        else:
            lines.append("**Evidence:** _(no direct match in verified facts)_")
        if gap and item.get("mitigation"):
            lines.append(f"**Mitigation:** {item['mitigation']}")
        lines.append("")
    return "\n".join(lines)


def _fmt_block_c(block: dict[str, object]) -> str:
    lines = ["## C: Fit Assessment", "", "### Selling Points", ""]
    lines.extend(f"- {point}" for point in _as_list(block.get("selling_points")))
    lines += ["", "### Honest Gaps", ""]
    for raw in _as_list(block.get("honest_gaps")):
        item = _as_dict(raw)
        lines.append(f"- **{item.get('gap', '')}**")
        if item.get("mitigation"):
            lines.append(f"  - _Mitigation:_ {item['mitigation']}")
    return "\n".join(lines)


def _fmt_block_d(block: dict[str, object]) -> str:
    lines = [
        "## D: Application Strategy",
        "",
        f"**Profile Angle:** {block.get('profile_angle', '')}",
        "",
        f"**Suggested Headline:** `{block.get('headline_suggestion', '')}`",
        "",
        "### Top CV Adjustments",
        "",
    ]
    lines.extend(f"- {adjustment}" for adjustment in _as_list(block.get("cv_adjustments")))
    return "\n".join(lines)


def _fmt_block_e(stories: list[object]) -> str:
    lines = ["## E: Interview Prep, STAR+R Stories", ""]
    for raw in stories:
        story = _as_dict(raw)
        star = _as_dict(story.get("star_r"))
        lines += [
            f"### {story.get('theme', '')}",
            f"**JD Hook:** _{story.get('jd_hook', '')}_",
            f'**Opening:** "{story.get("opening_line", "")}"',
            "",
            f"- **S:** {star.get('situation', '')}",
            f"- **T:** {star.get('task', '')}",
            f"- **A:** {star.get('action', '')}",
            f"- **R:** {star.get('result', '')}",
            f"- **+R:** {star.get('reflection', '')}",
            "",
        ]
    return "\n".join(lines)


def _fmt_block_f(block: dict[str, object]) -> str:
    verdict = str(block.get("verdict", "borderline"))
    badge = VERDICT_BADGES.get(verdict, verdict)
    confidence = block.get("confidence", 0.0)
    try:
        confidence_str = f"{float(str(confidence)):.2f}"
    except (TypeError, ValueError):
        confidence_str = str(confidence)
    lines = [
        "## F: Verdict",
        "",
        f"**{badge}** (confidence={confidence_str})",
        "",
        f"_{block.get('reason', '')}_",
        "",
    ]
    deal_breakers = _as_list(block.get("deal_breakers"))
    if deal_breakers:
        lines.append("**Deal-breakers:**")
        lines.extend(f"- {item}" for item in deal_breakers)
    return "\n".join(lines)


def build_report(data: dict[str, object], company: str, role: str, job_url: str) -> str:
    today = date.today().isoformat()
    parts = [
        f"# Offer Evaluation: {company}, {role}",
        "",
        f"_Generated: {today}_",
        "",
        _fmt_block_f(_as_dict(data.get("block_f"))),
        "",
        _fmt_block_a(_as_dict(data.get("block_a")), company, role, job_url),
        "",
        _fmt_block_b(_as_list(data.get("block_b"))),
        "",
        _fmt_block_c(_as_dict(data.get("block_c"))),
        "",
        _fmt_block_d(_as_dict(data.get("block_d"))),
        "",
        _fmt_block_e(_as_list(data.get("block_e"))),
    ]
    return "\n".join(parts)
