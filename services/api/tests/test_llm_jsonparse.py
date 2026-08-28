"""Pins for tolerant AI-response JSON parsing (spec 058).

The tolerance is exactly one malformation: trailing commas before a
closing brace or bracket, outside string literals. Everything else must
fail exactly as strict json.loads fails, and well-formed input must never
be altered.
"""

import json

import pytest

from harrier.llm.jsonparse import loads_tolerant


def test_trailing_comma_before_closing_brace_parses() -> None:
    text = (
        '{\n  "short_version": "a synthetic short letter",\n'
        '  "full_version": "a synthetic full letter",\n}'
    )
    assert loads_tolerant(text) == {
        "short_version": "a synthetic short letter",
        "full_version": "a synthetic full letter",
    }


def test_trailing_comma_before_closing_bracket_in_nested_array_parses() -> None:
    text = '{"answers": [{"notes": ["one", "two",],},],}'
    assert loads_tolerant(text) == {"answers": [{"notes": ["one", "two"]}]}


def test_string_value_containing_comma_brace_survives_byte_for_byte() -> None:
    value = 'closing thought: ", }" and "],", verbatim'
    text = json.dumps({"full_version": value})
    with_trailing = text[:-1] + ",}"
    parsed = loads_tolerant(with_trailing)
    assert parsed == {"full_version": value}


def test_valid_json_is_never_altered() -> None:
    text = json.dumps({"a": [1, 2], "b": {"c": "text with , } inside"}})
    assert loads_tolerant(text) == json.loads(text)


def test_truncated_json_raises_the_strict_error() -> None:
    truncated = '{"short_version": "a synthetic short letter"'
    with pytest.raises(json.JSONDecodeError) as tolerant_error:
        loads_tolerant(truncated)
    with pytest.raises(json.JSONDecodeError) as strict_error:
        json.loads(truncated)
    assert str(tolerant_error.value) == str(strict_error.value)


def test_other_malformations_still_fail() -> None:
    for bad in ("{'single': 'quotes'}", "not json at all", '{"a": 1 // comment\n}'):
        with pytest.raises(json.JSONDecodeError):
            loads_tolerant(bad)
