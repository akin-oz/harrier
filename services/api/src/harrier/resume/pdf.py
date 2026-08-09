"""PDF render and the PDF gate (spec 013 port).

Playwright imports lazily so the package works without it; the gate is
PDF or failure, and layout checks stay honest heuristics (page count via
pdfinfo when available).
"""

from __future__ import annotations

# Playwright is an optional dependency (lazy import below); its stubs are
# absent in the base environment.
# pyright: reportMissingImports=false, reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
import re
import subprocess
from pathlib import Path


def render_pdf(html_text: str, pdf_path: Path, margin_mm: int = 10) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Install it with:\n"
            "uv add --project services/api playwright\n"
            "uv run --project services/api playwright install chromium"
        ) from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_content(html_text, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(pdf_path),
                format="A4",
                print_background=True,
                margin={
                    "top": f"{margin_mm}mm",
                    "right": f"{margin_mm}mm",
                    "bottom": f"{margin_mm}mm",
                    "left": f"{margin_mm}mm",
                },
                prefer_css_page_size=True,
            )
            browser.close()
    except PlaywrightError as exc:
        raise RuntimeError(
            "Playwright could not render the PDF. If Chromium is missing, run:\n"
            "uv run --project services/api playwright install chromium\n"
            f"Underlying error: {exc}"
        ) from exc


def validate_rendered_pdf(pdf_path: Path, html_text: str, intended_pages: int = 1) -> list[str]:
    """Practical post-render checks; PDF layout checks are necessarily
    heuristic."""
    errors: list[str] = []
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        return ["PDF was not created or is empty"]
    if "�" in html_text:
        errors.append("HTML contains replacement characters")
    if re.search(r"{{[a-zA-Z0-9_]+}}", html_text):
        errors.append("HTML contains unresolved template placeholders")
    try:
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)], capture_output=True, text=True, check=False, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        errors.append("could not inspect PDF page count with pdfinfo")
        return errors
    if result.returncode != 0:
        errors.append("pdfinfo could not read rendered PDF")
        return errors
    match = re.search(r"^Pages:\s+(\d+)\s*$", result.stdout, flags=re.MULTILINE)
    if not match:
        errors.append("rendered PDF has no readable page count")
    elif int(match.group(1)) != intended_pages:
        errors.append(f"rendered PDF has {match.group(1)} pages; expected {intended_pages}")
    return errors
