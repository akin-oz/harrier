"""The resume and cover letter templates are found from any working
directory (spec 107, PR 3's hosted runs).

A hosted run starts in its owner's run directory (spec 106), not the
repository root. On staging, the first hosted `tailor` run failed with
`FileNotFoundError: 'templates/resume-template.html'` because the template
directory was relative to the working directory. Both tests run from an
empty temporary directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harrier.apply import letters
from harrier.resume import htmlrender
from harrier.tracks import TRACK_KINDS, rules_for


def test_every_resume_template_is_found_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    for kind in TRACK_KINDS:
        assert htmlrender._read_template(htmlrender.TEMPLATE_DIR, rules_for(kind).cv_template)  # pyright: ignore[reportPrivateUsage]
    assert htmlrender._read_template(htmlrender.TEMPLATE_DIR, htmlrender.CSS_TEMPLATE)  # pyright: ignore[reportPrivateUsage]


def test_the_cover_letter_templates_are_found_from_another_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    names = {letters.HTML_TEMPLATE, letters.CSS_TEMPLATE}
    names |= {rules_for(kind).letter_template for kind in TRACK_KINDS}
    for name in sorted(names):
        assert (letters.TEMPLATE_DIR / name).read_text(encoding="utf-8")
