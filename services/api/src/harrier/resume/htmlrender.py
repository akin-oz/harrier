"""HTML render from the repo templates (spec 013 port).

templates/resume-template.html and .css hold placeholders only; every
value is escaped, and an unresolved placeholder fails the render.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import cast

from harrier.resume.content import ResumeBundle
from harrier.resume.markdown import normalize_visible_url_text

TEMPLATE_DIR = Path("templates")
HTML_TEMPLATE = "resume-template.html"
CSS_TEMPLATE = "resume-template.css"


def _read_template(template_dir: Path, name: str) -> str:
    return (template_dir / name).read_text(encoding="utf-8")


def _extract_section(lines: list[str], header: str) -> list[str]:
    try:
        start = lines.index(header) + 1
    except ValueError:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return [line for line in lines[start:end] if line.strip()]


def _render_list_items(items: list[str]) -> str:
    return "\n".join(f"<li>{html.escape(item)}</li>" for item in items)


def _parse_experience(lines: list[str]) -> list[dict[str, object]]:
    roles: list[dict[str, object]] = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith("### "):
            index += 1
            continue
        company_title = lines[index][4:]
        period = lines[index + 1] if index + 1 < len(lines) else ""
        bullets: list[str] = []
        index += 2
        while index < len(lines) and lines[index].startswith("- "):
            bullets.append(lines[index][2:])
            index += 1
        company, title = company_title.split(" — ", 1)
        roles.append({"company": company, "title": title, "period": period, "bullets": bullets})
    return roles


def _render_experience_html(entries: list[dict[str, object]]) -> str:
    articles: list[str] = []
    for entry in entries:
        bullets = entry["bullets"]
        bullet_items = cast("list[object]", bullets) if isinstance(bullets, list) else []
        bullets_html = _render_list_items([str(item) for item in bullet_items])
        articles.append(
            "\n".join(
                [
                    '<article class="experience-item">',
                    '  <div class="experience-head">',
                    "    <div>",
                    f'      <h3 class="role-title">{html.escape(str(entry["title"]))}</h3>',
                    f'      <p class="company">{html.escape(str(entry["company"]))}</p>',
                    "    </div>",
                    f'    <p class="period">{html.escape(str(entry["period"]))}</p>',
                    "  </div>",
                    f"  <ul>{bullets_html}</ul>",
                    "</article>",
                ]
            )
        )
    return "\n".join(articles)


def render_html(markdown: str, bundle: ResumeBundle, template_dir: Path | None = None) -> str:
    directory = template_dir if template_dir is not None else TEMPLATE_DIR
    template = _read_template(directory, HTML_TEMPLATE)
    css = _read_template(directory, CSS_TEMPLATE)
    template = re.sub(r'<link rel="preconnect"[^>]+>\s*', "", template)
    template = re.sub(
        r'<link href="https://fonts.googleapis.com/[^"]+" rel="stylesheet">\s*', "", template
    )
    template = template.replace(
        '<link rel="stylesheet" href="./resume-template.css" />',
        f"<style>\n{css}\n</style>",
    )

    lines = markdown.splitlines()
    profile = " ".join(_extract_section(lines, "## PROFILE"))
    achievements = [
        line[2:]
        for line in _extract_section(lines, "## SELECTED ACHIEVEMENTS")
        if line.startswith("- ")
    ]
    certifications = _extract_section(lines, "## CERTIFICATIONS")
    education = _extract_section(lines, "## EDUCATION")
    technical_skills = " ".join(_extract_section(lines, "## TECHNICAL SKILLS"))
    visible_role_title = lines[1].strip() if len(lines) > 1 else ""

    replacements = {
        "name": bundle.name,
        "headline": visible_role_title,
        "location": bundle.location,
        "linkedin_url": bundle.linkedin,
        "linkedin_label": normalize_visible_url_text(bundle.linkedin),
        "email": bundle.email,
        "phone": bundle.phone,
        "profile": profile,
        "education_degree": education[0] if education else "",
        "education_school": education[1] if len(education) > 1 else "",
        "technical_skills": technical_skills,
    }
    for key, value in replacements.items():
        template = template.replace(f"{{{{{key}}}}}", html.escape(str(value)))
    template = template.replace("{{selected_achievements_html}}", _render_list_items(achievements))
    experience = _parse_experience(_extract_section(lines, "## EXPERIENCE"))
    template = template.replace("{{experience_html}}", _render_experience_html(experience))
    template = template.replace("{{certifications_html}}", _render_list_items(certifications))
    unresolved = sorted(set(re.findall(r"{{([a-zA-Z0-9_]+)}}", template)))
    if unresolved:
        raise ValueError(f"unresolved resume template placeholders: {', '.join(unresolved)}")
    return template
