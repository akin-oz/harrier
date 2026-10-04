"""Dashes used as punctuation, which the resume never carries (spec 071 O1).

An em dash, an en dash, a double hyphen, or a hyphen with a space on both
sides. A hyphen inside a word ("Full-Stack") is not punctuation, and a
markdown list marker at the start of a line is syntax, not text.

This module imports nothing from `harrier.resume`, so the bundle parser and
the markdown validator can both use it.
"""

from __future__ import annotations

import re

_DASH_PUNCTUATION = re.compile(r"\u2014|\u2013|--|(?<=\s)-(?=\s)")
_LIST_MARKER = re.compile(r"^(\s*)- ")


def dash_marks(text: str) -> list[str]:
    """Each dash used as punctuation in `text`, in order, list markers aside."""
    found: list[str] = []
    for line in text.splitlines() or [text]:
        text_part = _LIST_MARKER.sub(r"\1", line)
        found.extend(match.group() for match in _DASH_PUNCTUATION.finditer(text_part))
    return found


def describe(marks: list[str]) -> str:
    names = {
        "\u2014": "em dash",
        "\u2013": "en dash",
        "--": "double hyphen",
        "-": "spaced hyphen",
    }
    return ", ".join(dict.fromkeys(names[mark] for mark in marks))
