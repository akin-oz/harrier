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
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from harrier.connection import URL_VARIABLE
from harrier.filestore import FileStore, LocalFileStore, bind_files, files, hosted_files

# The private directory a hosted render uses (spec 107, "PDF rendering").
HOSTED_RENDER_DIRECTORY_MODE = 0o700

# What Chromium and `pdfinfo` keep of a hosted run's environment: enough to
# find the browser, fonts and a temporary directory, and nothing a run holds
# as a secret (the run login's URL, the owner's credentials). The security
# review of PR #368 found both tools inheriting them once `tailor` and
# `cover-letter` ran hosted.
RENDER_ENVIRONMENT: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "PLAYWRIGHT_BROWSERS_PATH",
    "FONTCONFIG_PATH",
    "FONTCONFIG_FILE",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)


def render_environment() -> dict[str, str] | None:
    """The environment a render tool starts with: None locally, where it
    inherits the process's as before; in a hosted process, only
    `RENDER_ENVIRONMENT`'s names, and the store's URL variable empty.

    Empty, not absent: an absent `HARRIER_DATABASE_URL` lets a `.env` in the
    working directory name the store (spec 103), and an empty one names
    nothing secret."""
    if not hosted_files():
        return None
    narrowed = {name: os.environ[name] for name in RENDER_ENVIRONMENT if name in os.environ}
    narrowed[URL_VARIABLE] = ""
    return narrowed


@contextmanager
def _render_environ() -> Generator[None]:
    """The process environment narrowed to `render_environment()` for the
    block, and restored after. Playwright's driver copies `os.environ` when
    it starts and ignores `launch(env=...)` for itself, so the narrowing has
    to be on the process. A run is one thread, so nothing else reads the
    environment meanwhile."""
    narrowed = render_environment()
    if narrowed is None:
        yield
        return
    saved = dict(os.environ)
    for name in set(saved) - set(narrowed):
        del os.environ[name]
    os.environ.update(narrowed)
    try:
        yield
    finally:
        for name in set(os.environ) - set(saved):
            del os.environ[name]
        os.environ.update(saved)


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
        with _render_environ(), sync_playwright() as playwright:
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
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=render_environment(),
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
    key: str | Path,
    render: Callable[[str, Path], None],
    validate: Callable[[Path, str], list[str]],
) -> list[str]:
    """Render beside the file at `key`, gate the result, and move it in only
    if it passes. A `Path` names a file of the caller's choosing instead of
    a key, and is rendered the same way beside it.

    The artifact endpoint serves whatever file the key holds (spec 047), so
    the key must never hold a PDF the gate has not passed: not one that
    failed, not one still being checked, and not an earlier run's (spec 067).
    The earlier PDF goes first, the render lands on a hidden temporary name
    in the same directory, and a rename moves it in, which is atomic within
    one filesystem. Returns the gate's errors; on any error or exception
    nothing is left at either path. A store with no local files renders in
    a private directory instead and uploads only a passing PDF (spec 107).
    """
    if isinstance(key, Path):
        with bind_files(LocalFileStore(key.parent)):
            return render_validated_pdf(html_text, key.name, render, validate)
    store = files()
    store.delete(key)
    pdf_path = store.local_path(key)
    if pdf_path is None:
        return _render_and_upload(store, html_text, key, render, validate)
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


def _render_and_upload(
    store: FileStore,
    html_text: str,
    key: str,
    render: Callable[[str, Path], None],
    validate: Callable[[Path, str], list[str]],
) -> list[str]:
    """A store with no local files (hosted, spec 107): render into a new
    directory of mode 0700 on this machine's disk, run the gate there, and
    upload only a passing PDF, once. The directory goes in a `finally`, pass
    or fail. The caller deleted the earlier object first, so a failure
    leaves the key empty, as locally."""
    directory = Path(tempfile.mkdtemp(prefix="harrier-pdf-"))
    try:
        # mkdtemp creates it 0700 already; said again so the mode does not
        # rest on a library default.
        os.chmod(directory, HOSTED_RENDER_DIRECTORY_MODE)
        rendered = directory / PurePosixPath(key).name
        render(html_text, rendered)
        errors = validate(rendered, html_text)
        if not errors:
            store.put(key, rendered.read_bytes(), atomic=True)
        return errors
    finally:
        shutil.rmtree(directory, ignore_errors=True)
