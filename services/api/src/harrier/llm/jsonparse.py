"""Tolerant JSON parsing for AI responses (spec 058).

Every AI call site extracts a JSON object from the model's text and parses
it here. Models occasionally emit a trailing comma before a closing brace
or bracket, which is one character of malformation wrapped around otherwise
valid content; strict json.loads throws the whole response away. This
helper parses strictly first, so a well-formed response is never altered,
and only on failure retries once with trailing commas removed. Anything
still malformed after that raises the original strict error, keeping every
caller's error surface unchanged.
"""

from __future__ import annotations

import json

_WHITESPACE = " \t\r\n"


def _strip_trailing_commas(text: str) -> str:
    """Remove commas that directly precede a closing } or ], outside
    string literals. A character-level scan rather than a regex because a
    comma inside a string value (a letter containing ", }") must survive
    byte for byte."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch in "}]":
            index = len(out) - 1
            while index >= 0 and out[index] in _WHITESPACE:
                index -= 1
            if index >= 0 and out[index] == ",":
                del out[index]
        out.append(ch)
    return "".join(out)


def loads_tolerant(text: str) -> object:
    """json.loads that tolerates trailing commas and nothing else."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as strict_error:
        stripped = _strip_trailing_commas(text)
        if stripped != text:
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                pass
        raise strict_error from None
