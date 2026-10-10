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
import secrets
import subprocess
from collections.abc import Callable
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


def allowed_pages_text(allowed_pages: tuple[int, ...]) -> str:
    """The allowed counts as the gate's message names them: "1", "1 or 2"."""
    counts = [str(count) for count in sorted(allowed_pages)]
    if len(counts) == 1:
        return counts[0]
    return f"{', '.join(counts[:-1])} or {counts[-1]}"


def validate_rendered_pdf(
    pdf_path: Path, html_text: str, *, allowed_pages: tuple[int, ...]
) -> list[str]:
    """Practical post-render checks; PDF layout checks are necessarily
    heuristic.

    `allowed_pages` is the track kind's (`KindRules.pages`): exactly one page
    on the industry kind, one or two on the academic (spec 101).
    """
    if not allowed_pages:
        raise ValueError("the PDF gate needs at least one allowed page count")
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
    elif int(match.group(1)) not in allowed_pages:
        expected = allowed_pages_text(allowed_pages)
        errors.append(f"rendered PDF has {match.group(1)} pages; expected {expected}")
    return errors


def render_validated_pdf(
    html_text: str,
    pdf_path: Path,
    render: Callable[[str, Path], None],
    validate: Callable[[Path, str], list[str]],
) -> list[str]:
    """Render beside `pdf_path`, gate the result, and move it in only if it passes.

    The artifact endpoint serves whatever file sits at `pdf_path` (spec 047),
    so that path must never hold a PDF the gate has not passed: not one that
    failed, not one still being checked, and not an earlier run's (spec 067).
    The earlier PDF goes first, the render lands on a hidden temporary name
    in the same directory, and a rename moves it in, which is atomic within
    one filesystem. Returns the gate's errors; on any error or exception
    nothing is left at either path.
    """
    pdf_path.unlink(missing_ok=True)
    # A name the render creates, rather than a file created here, so the PDF
    # keeps the permissions it always had.
    temporary = pdf_path.with_name(f".{pdf_path.name}.{secrets.token_hex(4)}.tmp")
    try:
        render(html_text, temporary)
        errors = validate(temporary, html_text)
        if not errors:
            temporary.replace(pdf_path)
        return errors
    finally:
        temporary.unlink(missing_ok=True)
