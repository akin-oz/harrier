"""The artifact gate, exercised rather than mocked (spec 045).

`validate_rendered_pdf` is the check behind the product invariant that resume
and cover letter generation succeed only if the PDF exists and validates.
Every test that reached it injected a fake in its place, so the real function
had no coverage at all: its replacement-character, placeholder and page-count
checks were disabled together and the whole suite stayed green.

`pdfinfo` is a poppler binary that is not guaranteed on any machine, so the
page-count cases drive it through a stubbed `subprocess.run`. The branch under
test is the gate's reading of the output, not poppler's ability to produce it,
and stubbing the boundary is what lets these run on a fresh clone.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harrier.resume import pdf as pdf_module
from harrier.resume.pdf import render_validated_pdf, validate_rendered_pdf

CLEAN_HTML = "<html><body><h1>Deniz Örnek</h1></body></html>"


def _pdf(tmp_path: Path, *, empty: bool = False) -> Path:
    target = tmp_path / "resume.pdf"
    target.write_bytes(b"" if empty else b"%PDF-1.7\n%stub\n")
    return target


def _stub_pdfinfo(
    monkeypatch: pytest.MonkeyPatch, *, pages: int | None = 1, returncode: int = 0
) -> None:
    stdout = "" if pages is None else f"Producer: stub\nPages:          {pages}\n"

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["pdfinfo"], returncode=returncode, stdout=stdout)

    monkeypatch.setattr(pdf_module.subprocess, "run", fake_run)


def test_a_missing_pdf_fails_the_gate(tmp_path: Path) -> None:
    errors = validate_rendered_pdf(tmp_path / "absent.pdf", CLEAN_HTML)
    assert errors == ["PDF was not created or is empty"]


def test_an_empty_pdf_fails_the_gate(tmp_path: Path) -> None:
    errors = validate_rendered_pdf(_pdf(tmp_path, empty=True), CLEAN_HTML)
    assert errors == ["PDF was not created or is empty"]


def test_a_replacement_character_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mojibake name reaches the recruiter looking like a broken document,
    and the candidate's name is exactly where it shows up."""
    _stub_pdfinfo(monkeypatch)
    errors = validate_rendered_pdf(_pdf(tmp_path), "<p>Deniz �rnek</p>")
    assert "HTML contains replacement characters" in errors


def test_an_unresolved_placeholder_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal {{company}} in a recruiter-facing artifact is the single most
    embarrassing way this tool could fail."""
    _stub_pdfinfo(monkeypatch)
    errors = validate_rendered_pdf(_pdf(tmp_path), "<p>Dear {{company}},</p>")
    assert "HTML contains unresolved template placeholders" in errors


def test_a_page_count_that_differs_from_the_intent_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pdfinfo(monkeypatch, pages=3)
    errors = validate_rendered_pdf(_pdf(tmp_path), CLEAN_HTML, intended_pages=1)
    assert errors == ["rendered PDF has 3 pages; expected 1"]


def test_an_unreadable_page_count_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pdfinfo(monkeypatch, pages=None)
    errors = validate_rendered_pdf(_pdf(tmp_path), CLEAN_HTML)
    assert errors == ["rendered PDF has no readable page count"]


def test_a_pdfinfo_that_cannot_read_the_file_fails_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pdfinfo(monkeypatch, returncode=1)
    errors = validate_rendered_pdf(_pdf(tmp_path), CLEAN_HTML)
    assert errors == ["pdfinfo could not read rendered PDF"]


def test_a_missing_pdfinfo_is_reported_rather_than_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not having the tool must not read as the document being fine. A gate
    that cannot run has not passed."""

    def explode(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("pdfinfo")

    monkeypatch.setattr(pdf_module.subprocess, "run", explode)
    errors = validate_rendered_pdf(_pdf(tmp_path), CLEAN_HTML)
    assert errors == ["could not inspect PDF page count with pdfinfo"]


def test_a_clean_render_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The counterpart that keeps every case above from passing for the wrong
    reason: a gate that always fails proves nothing."""
    _stub_pdfinfo(monkeypatch)
    assert validate_rendered_pdf(_pdf(tmp_path), CLEAN_HTML) == []


# --- what the gate leaves at the final path (spec 067) -----------------------


def _write_pdf(html_text: str, path: Path) -> None:
    path.write_bytes(b"%PDF-1.7\n%stub\n")


def _passes(path: Path, html_text: str) -> list[str]:
    return []


def _fails(path: Path, html_text: str) -> list[str]:
    return ["rendered PDF has 2 pages; expected 1"]


def test_a_pdf_that_fails_the_gate_is_never_moved_into_place(tmp_path: Path) -> None:
    """The artifact endpoint serves whatever is at the final path, so a PDF
    the gate refused must not be there, and nor may the earlier one."""
    final = tmp_path / "letter.pdf"
    final.write_bytes(b"%PDF-1.7\n%earlier run\n")

    errors = render_validated_pdf(CLEAN_HTML, final, _write_pdf, _fails)

    assert errors == ["rendered PDF has 2 pages; expected 1"]
    assert not final.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_final_path_is_empty_while_the_gate_runs(tmp_path: Path) -> None:
    """Validation can take seconds (pdfinfo). A reader in that window, or a
    process killed in it, must never find an unjudged PDF at the final path."""
    final = tmp_path / "letter.pdf"
    final.write_bytes(b"%PDF-1.7\n%earlier run\n")
    seen: list[tuple[Path, bool]] = []

    def watching(path: Path, html_text: str) -> list[str]:
        seen.append((path, final.exists()))
        return []

    render_validated_pdf(CLEAN_HTML, final, _write_pdf, watching)

    [(validated, final_existed)] = seen
    assert validated != final
    assert validated.parent == final.parent
    assert validated.name.startswith(".")
    assert final_existed is False


def test_a_render_that_raises_leaves_no_pdf_and_no_temporary_file(tmp_path: Path) -> None:
    final = tmp_path / "letter.pdf"
    final.write_bytes(b"%PDF-1.7\n%earlier run\n")

    def crashing(html_text: str, path: Path) -> None:
        path.write_bytes(b"%PDF-1.7\n%half")
        raise RuntimeError("Playwright could not render the PDF")

    with pytest.raises(RuntimeError, match="could not render"):
        render_validated_pdf(CLEAN_HTML, final, crashing, _passes)

    assert list(tmp_path.iterdir()) == []


def test_a_passing_render_is_moved_into_place(tmp_path: Path) -> None:
    final = tmp_path / "letter.pdf"
    final.write_bytes(b"%PDF-1.7\n%earlier run\n")

    assert render_validated_pdf(CLEAN_HTML, final, _write_pdf, _passes) == []

    assert final.read_bytes() == b"%PDF-1.7\n%stub\n"
    assert list(tmp_path.iterdir()) == [final]
