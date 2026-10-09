"""The academic search entry: one per track, written once (spec 097).

The `academic_searches` configuration kind holds one JSON object keyed by
track slug. Each value is that track's search entry. It compiles into two
things that must agree: the source's input (what the actor is asked for) and
the academic gates (what harrier keeps). Writing the search once is the
point: an opaque actor input beside separate title lists would reject
postings the source found by full text, and seen state would keep those
rejections.

No field names the actor or any of its input or output fields. Those are the
source module's constants (`harrier.sources.apify_academic`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from harrier.screening.rules import ACADEMIC_MATCHER_RULES, AcademicTerm
from harrier.sources import apify_academic as source
from harrier.tracks import InvalidSlugError, validate_slug


class SearchError(ValueError):
    """An entry is not the shape its readers need. The message names the field."""


ENTRY_FIELDS = frozenset(
    {
        "areas",
        "require_area_match",
        "position",
        "exclude",
        "countries",
        "window_days",
        "portals",
        "flag_phrases",
        "funding_flag_values",
        "ceilings",
    }
)
REQUIRED_FIELDS = ("areas", "countries", "window_days", "ceilings")

POSITION_FIELDS = ("title", "position_type", "description")
POSITION_DEFAULT = ("title", "position_type")
EXCLUDE_FIELDS = ("title", "organisation", "position_type")
EXCLUDE_DEFAULT = ("title",)
AREA_FIELDS = ("title", "description", "subject")

# The deadline rule, digested into the policy version: a deadline more than
# one day before the run's date is passed, and a passed decision is judged
# again on every run (spec 097, "The deadline").
DEADLINE_RULE = "passed-when-more-than-1-day-before-run-date;never-skipped-by-seen"


@dataclass(frozen=True)
class Area:
    label: str
    terms: tuple[AcademicTerm, ...]


@dataclass(frozen=True)
class FieldTerms:
    terms: tuple[AcademicTerm, ...]
    fields: tuple[str, ...]


@dataclass(frozen=True)
class SearchEntry:
    """One track's search, parsed and bounded."""

    areas: tuple[Area, ...]
    require_area_match: bool
    position: FieldTerms | None
    exclude: tuple[FieldTerms, ...]
    countries: tuple[str, ...]
    window_days: int
    portals: tuple[str, ...]
    flag_phrases: dict[str, tuple[str, ...]]
    funding_flag_values: tuple[str, ...]
    max_results: int
    max_charge_usd: float
    # Ceilings above their hard limits, clamped where the entry was read
    # (spec 035's rule); the summary says so.
    clamped: tuple[str, ...] = field(default=())
    raw: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def keywords(self) -> list[str]:
        """Every area term, once, in order: the source's search terms."""
        seen: list[str] = []
        for area in self.areas:
            for term in area.terms:
                if term.text not in seen:
                    seen.append(term.text)
        return seen


def _object(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SearchError(f"{where} must be an object, got {type(value).__name__}")
    return cast("dict[str, object]", value)


def _unknown(entry: dict[str, object], allowed: frozenset[str] | set[str], where: str) -> None:
    unknown = sorted(str(key) for key in entry if key not in allowed)
    if unknown:
        raise SearchError(f"{where} has unknown fields {unknown}; expected only {sorted(allowed)}")


def _strings(value: object, where: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise SearchError(f"{where} must be a list")
    items: list[str] = []
    for item in cast("list[object]", value):
        if not isinstance(item, str) or not item.strip():
            raise SearchError(f"{where} entries must be non-blank strings")
        items.append(item.strip())
    if not items and not allow_empty:
        raise SearchError(f"{where} must not be empty")
    return items


def _terms(value: object, where: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not value:
        raise SearchError(f"{where} must be a non-empty list of terms")
    terms: list[dict[str, object]] = []
    for index, item in enumerate(cast("list[object]", value)):
        term = _object(item, f"{where}[{index}]")
        _unknown(term, {"text", "prefix"}, f"{where}[{index}]")
        text = term.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SearchError(f"{where}[{index}].text must be a non-blank string")
        prefix = term.get("prefix", False)
        if not isinstance(prefix, bool):
            raise SearchError(f"{where}[{index}].prefix must be true or false")
        try:
            AcademicTerm(text.strip(), prefix=prefix)
        except ValueError as exc:
            raise SearchError(f"{where}[{index}].text has no word to match") from exc
        normalized: dict[str, object] = {"text": text.strip()}
        if prefix:
            normalized["prefix"] = True
        terms.append(normalized)
    return terms


def _field_terms(
    value: object, where: str, allowed: tuple[str, ...], default: tuple[str, ...]
) -> dict[str, object]:
    entry = _object(value, where)
    _unknown(entry, {"terms", "in"}, where)
    terms = _terms(entry.get("terms"), f"{where}.terms")
    fields = list(default)
    if "in" in entry:
        fields = _strings(entry["in"], f"{where}.in", allow_empty=False)
        bad = [name for name in fields if name not in allowed]
        if bad:
            raise SearchError(f"{where}.in names {bad}; expected a subset of {list(allowed)}")
    return {"terms": terms, "in": fields}


def _validate_entry(slug: str, value: object, *, enforce_limits: bool) -> dict[str, object]:
    where = f"academic_searches[{slug!r}]"
    entry = _object(value, where)
    _unknown(entry, ENTRY_FIELDS, where)
    missing = [name for name in REQUIRED_FIELDS if name not in entry]
    if missing:
        raise SearchError(f"{where} is missing {missing[0]!r}")

    areas_raw = entry["areas"]
    if not isinstance(areas_raw, list) or not areas_raw:
        raise SearchError(f"{where}.areas must be a non-empty list")
    areas: list[dict[str, object]] = []
    for index, item in enumerate(cast("list[object]", areas_raw)):
        area = _object(item, f"{where}.areas[{index}]")
        _unknown(area, {"label", "terms"}, f"{where}.areas[{index}]")
        label = area.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SearchError(f"{where}.areas[{index}].label must be a non-blank string")
        areas.append(
            {
                "label": label.strip(),
                "terms": _terms(area.get("terms"), f"{where}.areas[{index}].terms"),
            }
        )

    normalized: dict[str, object] = {"areas": areas}

    require = entry.get("require_area_match", False)
    if not isinstance(require, bool):
        raise SearchError(f"{where}.require_area_match must be true or false")
    normalized["require_area_match"] = require

    if "position" in entry:
        normalized["position"] = _field_terms(
            entry["position"], f"{where}.position", POSITION_FIELDS, POSITION_DEFAULT
        )

    if "exclude" in entry:
        raw_exclude = entry["exclude"]
        if not isinstance(raw_exclude, list):
            raise SearchError(f"{where}.exclude must be a list")
        normalized["exclude"] = [
            _field_terms(item, f"{where}.exclude[{index}]", EXCLUDE_FIELDS, EXCLUDE_DEFAULT)
            for index, item in enumerate(cast("list[object]", raw_exclude))
        ]

    countries = [
        code.upper()
        for code in _strings(entry["countries"], f"{where}.countries", allow_empty=False)
    ]
    unsupported = [code for code in countries if code not in source.SUPPORTED_COUNTRIES]
    if unsupported:
        raise SearchError(
            f"{where}.countries names codes the source does not support: {unsupported}"
        )
    normalized["countries"] = countries

    window = entry["window_days"]
    if type(window) is not int or window < 1:
        raise SearchError(f"{where}.window_days must be a positive integer")
    normalized["window_days"] = window

    if "portals" in entry:
        portals = _strings(entry["portals"], f"{where}.portals", allow_empty=False)
        known = source.known_portals()
        if not known:
            raise SearchError(
                f"{where}.portals is set, and {source.PORTALS_ENV} names no known portal"
            )
        unknown_portals = [portal for portal in portals if portal not in known]
        if unknown_portals:
            raise SearchError(f"{where}.portals names unknown portals: {unknown_portals}")
        normalized["portals"] = portals

    if "flag_phrases" in entry:
        flags = _object(entry["flag_phrases"], f"{where}.flag_phrases")
        normalized_flags: dict[str, list[str]] = {}
        for name, phrases in flags.items():
            if not name or not name.replace("_", "").isalnum() or name != name.lower():
                raise SearchError(f"{where}.flag_phrases has a malformed flag name {name!r}")
            checked = _strings(phrases, f"{where}.flag_phrases.{name}")
            # The matcher's own rule, as for every other term: a phrase with
            # no word to match would fail inside screening, after the run was
            # billed (review of PR #187).
            for phrase in checked:
                try:
                    AcademicTerm(phrase)
                except ValueError as exc:
                    raise SearchError(
                        f"{where}.flag_phrases.{name} has a phrase with no word to match: "
                        f"{phrase!r}"
                    ) from exc
            normalized_flags[name] = checked
        normalized["flag_phrases"] = normalized_flags

    if "funding_flag_values" in entry:
        normalized["funding_flag_values"] = _strings(
            entry["funding_flag_values"], f"{where}.funding_flag_values"
        )

    ceilings = _object(entry["ceilings"], f"{where}.ceilings")
    _unknown(ceilings, {"max_results", "max_charge_usd"}, f"{where}.ceilings")
    if "max_results" not in ceilings:
        raise SearchError(f"{where}.ceilings is missing 'max_results'")
    if "max_charge_usd" not in ceilings:
        raise SearchError(f"{where}.ceilings is missing 'max_charge_usd'")
    max_results = ceilings["max_results"]
    if type(max_results) is not int or max_results < 1:
        raise SearchError(f"{where}.ceilings.max_results must be a positive integer")
    max_charge = ceilings["max_charge_usd"]
    if not isinstance(max_charge, int | float) or isinstance(max_charge, bool) or max_charge <= 0:
        raise SearchError(f"{where}.ceilings.max_charge_usd must be a number above 0")
    if enforce_limits:
        if max_results > source.ACADEMIC_MAX_RESULTS:
            raise SearchError(
                f"{where}.ceilings.max_results {max_results} is above its hard limit "
                f"{source.ACADEMIC_MAX_RESULTS}"
            )
        if max_charge > source.ACADEMIC_MAX_CHARGE_USD:
            raise SearchError(
                f"{where}.ceilings.max_charge_usd {max_charge} is above its hard limit "
                f"{source.ACADEMIC_MAX_CHARGE_USD}"
            )
        worst = source.worst_case_usd(max_results)
        if worst > max_charge:
            raise SearchError(
                f"{where}.ceilings: max_results {max_results} can cost {worst:.2f} USD, "
                f"above max_charge_usd {max_charge}"
            )
    normalized["ceilings"] = {"max_results": max_results, "max_charge_usd": float(max_charge)}
    return normalized


def validate_searches(value: object, *, enforce_limits: bool = True) -> dict[str, object]:
    """The whole kind's value, checked and normalized.

    `enforce_limits` is true on a write: a ceiling above its hard limit, or a
    result cap that can cost more than the charge ceiling, is refused there.
    A read checks the shape only, and the ceilings are clamped where the
    entry is used (`parse_entry`), as spec 035 does for the discovery count.
    Whether a slug names a live academic track needs a connection, so it is
    checked when a run starts, never here.
    """
    if not isinstance(value, dict):
        raise SearchError(f"academic_searches must be an object, got {type(value).__name__}")
    normalized: dict[str, object] = {}
    for key, entry in cast("dict[object, object]", value).items():
        if not isinstance(key, str):
            raise SearchError("academic_searches keys must be track slugs")
        try:
            validate_slug(key)
        except InvalidSlugError as exc:
            raise SearchError(f"academic_searches key {key!r} is not a track slug: {exc}") from exc
        normalized[key] = _validate_entry(key, entry, enforce_limits=enforce_limits)
    return normalized


def _term_objects(raw: object) -> tuple[AcademicTerm, ...]:
    return tuple(
        AcademicTerm(str(term["text"]), prefix=bool(term.get("prefix", False)))
        for term in cast("list[dict[str, Any]]", raw)
    )


def parse_entry(slug: str, raw: object) -> SearchEntry:
    """One entry, validated for shape, with its ceilings clamped."""
    checked = cast("dict[str, Any]", _validate_entry(slug, raw, enforce_limits=False))
    clamped: list[str] = []
    max_results = int(checked["ceilings"]["max_results"])
    if max_results > source.ACADEMIC_MAX_RESULTS:
        max_results = source.ACADEMIC_MAX_RESULTS
        clamped.append("max_results")
    max_charge = float(checked["ceilings"]["max_charge_usd"])
    if max_charge > source.ACADEMIC_MAX_CHARGE_USD:
        max_charge = source.ACADEMIC_MAX_CHARGE_USD
        clamped.append("max_charge_usd")
    position: FieldTerms | None = None
    if "position" in checked:
        position_raw = cast("dict[str, Any]", checked["position"])
        position = FieldTerms(
            _term_objects(position_raw["terms"]), tuple(cast("list[str]", position_raw["in"]))
        )
    return SearchEntry(
        areas=tuple(
            Area(str(area["label"]), _term_objects(area["terms"]))
            for area in cast("list[dict[str, Any]]", checked["areas"])
        ),
        require_area_match=bool(checked["require_area_match"]),
        position=position,
        exclude=tuple(
            FieldTerms(_term_objects(item["terms"]), tuple(item["in"]))
            for item in cast("list[dict[str, Any]]", checked.get("exclude", []))
        ),
        countries=tuple(checked["countries"]),
        window_days=int(checked["window_days"]),
        portals=tuple(checked.get("portals", [])),
        flag_phrases={
            name: tuple(phrases)
            for name, phrases in cast(
                "dict[str, list[str]]", checked.get("flag_phrases", {})
            ).items()
        },
        funding_flag_values=tuple(checked.get("funding_flag_values", [])),
        max_results=max_results,
        max_charge_usd=max_charge,
        clamped=tuple(clamped),
        raw=checked,
    )


def refusal_before_run(entry: SearchEntry) -> str | None:
    """Why this entry may not start a run, or None. Checked on every run,
    whatever wrote the entry, so a row written around `config set` cannot
    start a run whose worst case exceeds its own charge ceiling."""
    worst = source.worst_case_usd(entry.max_results)
    if worst > entry.max_charge_usd:
        return (
            f"max_results {entry.max_results} can cost {worst:.2f} USD, "
            f"above max_charge_usd {entry.max_charge_usd}"
        )
    return None


def compile_input(entry: SearchEntry) -> dict[str, object]:
    """The source's input. Position terms are not sent: the source combines
    its keywords with OR, so a position word would widen the search."""
    return source.compile_input(
        keywords=entry.keywords,
        countries=list(entry.countries),
        window_days=entry.window_days,
        max_results=entry.max_results,
        portals=list(entry.portals) or None,
    )


def worst_case_usd(entry: SearchEntry) -> float:
    return source.worst_case_usd(entry.max_results)


def policy_fingerprint(entry: SearchEntry) -> dict[str, object]:
    """What an academic decision depends on, and nothing else: the fields
    that reject, the source fields those rules read, the deadline rule and
    the matcher's rules. Labels, flags, countries and ceilings decide no
    rejection, so they are left out and editing them reopens nothing."""
    raw = entry.raw
    read_fields = set(EXCLUDE_FIELDS) | set(POSITION_FIELDS) | {"deadline"}
    if entry.require_area_match:
        read_fields |= set(AREA_FIELDS)
    deciding: dict[str, object] = {
        "position": raw.get("position"),
        "exclude": raw.get("exclude", []),
        "require_area_match": entry.require_area_match,
        "areas": (
            [area["terms"] for area in cast("list[dict[str, Any]]", raw["areas"])]
            if entry.require_area_match
            else None
        ),
    }
    return {
        "deciding": deciding,
        "field_map": {name: source.FIELD_MAP.get(name) for name in sorted(read_fields)},
        "deadline_rule": DEADLINE_RULE,
        "matcher": list(ACADEMIC_MATCHER_RULES),
    }
