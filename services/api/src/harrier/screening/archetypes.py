"""Archetype detection: the single implementation (spec 007).

The old repo carried three copies (job_sources.py, evaluate_offer.py,
tailor_resume.py); specs 013 and 015 consume this one.
"""

from __future__ import annotations

from harrier.screening.normalized import normalize

_ARCHETYPE_SIGNALS: list[tuple[str, frozenset[str]]] = [
    (
        "design_system",
        frozenset(
            {
                "design system",
                "component library",
                "storybook",
                "design token",
                "ui library",
                "component platform",
                "ui kit",
                "design infrastructure",
            }
        ),
    ),
    (
        "platform_frontend",
        frozenset(
            {
                "developer experience",
                "developer tooling",
                "developer platform",
                "internal platform",
                "dx engineering",
                "platform team",
                "devex",
                "build system",
                "monorepo",
                "tooling team",
            }
        ),
    ),
    (
        "performance_ux",
        frozenset(
            {
                "core web vitals",
                "web vitals",
                "lighthouse",
                "rendering performance",
                "ux engineering",
                "frontend performance",
                "paint time",
                "cls",
                "lcp",
            }
        ),
    ),
    (
        "fullstack_light",
        frozenset(
            {
                "fullstack",
                "full stack",
                "full-stack",
                "backend",
                "api development",
                "node.js",
                "server-side",
                "graphql api",
            }
        ),
    ),
    (
        "product_engineer",
        frozenset(
            {
                "product engineer",
                "product thinking",
                "cross-functional",
                "product roadmap",
                "product discovery",
                "user research",
                "product intuition",
            }
        ),
    ),
]


def detect_archetype(title: str, description: str) -> str:
    """Classify a job into a target archetype.

    Returns one of: design_system, product_engineer, platform_frontend,
    performance_ux, fullstack_light, general_frontend.
    Title is checked first (higher signal), then the description needs at
    least two signals to classify.
    """
    title_lower = normalize(title or "")
    desc_lower = normalize(description or "")

    for archetype, signals in _ARCHETYPE_SIGNALS:
        if any(sig in title_lower for sig in signals):
            return archetype

    if "product engineer" in title_lower:
        return "product_engineer"

    combined = f"{title_lower} {desc_lower}"
    for archetype, signals in _ARCHETYPE_SIGNALS:
        matches = sum(1 for sig in signals if sig in combined)
        if matches >= 2:
            return archetype

    return "general_frontend"
