"""Whether a posting can engage someone based in Türkiye (spec 122).

Two dimensions, kept apart. The verdict says where the role permits work
from: confirmed, possible, unknown or excluded. The routes say how the
employer can engage the person: employee, contractor, employer of record,
relocation. Neither is derived from the other, and a confirmed verdict
says nothing about the route.

This module judges and writes nothing. It never rejects a posting, never
touches the fit score and never reorders the queue: the gates and the score
are `harrier.screening.rules`, and the write is `harrier.tracker.store`.
Its version is its own and is kept out of `policy_version`, which
describes screening decisions; this makes none (spec 031).

Every table here is matched against text folded by `fold_with_map`, so
"TÜRKİYE", "Türkiye", "turkiye" and "Turkey" read alike. The excerpt is cut
from the text as written.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from harrier.screening.normalized import NormalizedJob
from harrier.screening.rules import (
    EMPLOYMENT_BLOCKER_PATTERNS,
    EU_PERMIT_PATTERNS,
    US_PAYROLL_REACH_PATTERNS,
    US_SCOPE_PATTERNS,
    location_names_explicit_emea,
)
from harrier.tracker.store import EligibilityAssessment

CONFIRMED = "confirmed"
POSSIBLE = "possible"
UNKNOWN = "unknown"
EXCLUDED = "excluded"

# The fields a rule reads, in the order a tie is broken.
FIELDS: tuple[str, ...] = ("title", "location", "description")

# The longest excerpt kept. A sentence longer than this is cut around the
# phrase that decided.
EXCERPT_LIMIT = 300

# --- the tables (the authority; spec 122 shows their shape) ---

TR_NAMED: tuple[str, ...] = (r"\b(?:turkiye|turkey|istanbul|ankara|izmir|antalya|bursa)\b",)

# Places a restriction can name that are not Türkiye and are not the EU, the
# EEA or Europe. An EU, EEA or Europe requirement is an EU-permit phrase, a
# positive signal and never a filter (product invariant), so it is absent
# here on purpose. The US is spec 078's table, below.
RESTRICTING_PLACES: tuple[str, ...] = (
    "united kingdom",
    "uk",
    "great britain",
    "britain",
    "england",
    "scotland",
    "wales",
    "northern ireland",
    "ireland",
    "germany",
    "france",
    "spain",
    "portugal",
    "italy",
    "netherlands",
    "belgium",
    "luxembourg",
    "austria",
    "switzerland",
    "poland",
    "czech republic",
    "czechia",
    "slovakia",
    "hungary",
    "romania",
    "bulgaria",
    "greece",
    "croatia",
    "slovenia",
    "serbia",
    "ukraine",
    "lithuania",
    "latvia",
    "estonia",
    "finland",
    "sweden",
    "norway",
    "denmark",
    "iceland",
    "israel",
    "egypt",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "south africa",
    "nigeria",
    "kenya",
    "india",
    "brazil",
    "argentina",
    "mexico",
    "colombia",
    "canada",
    "australia",
    "new zealand",
    "singapore",
    "japan",
    "latam",
    "latin america",
    "north america",
    "apac",
)
_PLACE = "(?:" + "|".join(re.escape(place) for place in RESTRICTING_PLACES) + ")"
_THE = r"(?:the\s+)?"

RESTRICTION_PATTERNS: tuple[str, ...] = (
    rf"\b(?:must|should|need to|needs to|required to)\s+(?:be\s+)?"
    rf"(?:based|located|living|resident|residing)\s+(?:in|within)\s+{_THE}{_PLACE}\b",
    rf"\bmust\s+(?:reside|live)\s+in\s+{_THE}{_PLACE}\b",
    rf"\bmust\s+be\s+an?\s+(?:resident|citizen|national)\s+of\s+{_THE}{_PLACE}\b",
    rf"\b(?:right|authori[sz]ed|eligible|permission)\s+to\s+work\s+in\s+{_THE}{_PLACE}\b",
    rf"\b{_PLACE}\s+(?:residents?|citizens?|nationals?|based\s+candidates?)\s+only\b",
    rf"\b(?:residents?|citizens?)\s+of\s+{_THE}{_PLACE}\s+only\b",
    rf"\b(?:only|exclusively)\s+(?:open\s+to|hiring|accepting|considering)\s+"
    rf"(?:candidates\s+|applicants\s+|applications\s+|people\s+|residents\s+)?"
    rf"(?:based\s+|located\s+|living\s+)?(?:in|from)\s+{_THE}{_PLACE}\b",
    rf"\b{_PLACE}[- ]?\s?only\b",
    rf"\bremote\s*\(\s*{_PLACE}\s*\)",
)

REGION_PATTERNS: tuple[str, ...] = (
    r"\b(?:worldwide|global|anywhere|emea|europe|remote[- ]first)\b",
)

# Bare "contract" is absent: "employment contract" and "permanent contract"
# are employment. Bare "B2B" is absent too: "a B2B SaaS company" describes
# the business, not the engagement.
ROUTE_CONTRACTOR_PATTERNS: tuple[str, ...] = (
    r"\b(?:independent\s+)?contractors?\b",
    r"\bcontract\s+(?:role|position|basis|engagement|work)\b",
    r"\bb2b\s+(?:contract|agreement|basis|engagement|cooperation)\b",
    r"\b(?:on\s+a|via|through|as)\s+b2b\b",
    r"\bfreelance(?:rs?)?\b",
)
# "Via" or "through" a named provider, so a posting from Deel, Remote.com or
# Oyster is not an employer of record by its company name.
ROUTE_EOR_PATTERNS: tuple[str, ...] = (
    r"\bemployer\s+of\s+record\b",
    r"\beor\b",
    r"\b(?:via|through|using)\s+(?:deel|remote\.com|oyster)\b",
)
ROUTE_RELOCATION_PATTERNS: tuple[str, ...] = (
    r"\brelocation\s+(?:support|package|assistance|bonus)\b",
    r"\b(?:we|will)\s+(?:sponsor|support)\s+(?:your\s+)?(?:visa|relocation)\b",
    r"\bvisa\s+sponsorship\s+(?:and|&)\s+relocation\b",
)
ROUTE_EMPLOYEE_PATTERNS: tuple[str, ...] = (
    r"\b(?:full[- ]time|permanent)\s+employees?\b",
    r"\bemployment\s+contract\b",
    r"\bpermanent\s+(?:position|role|employment)\b",
)

# A route offered in the negative ("no relocation support", "not a contract
# role") names no route.
_NEGATION_BEFORE = re.compile(r"(?:\b(?:no|not|without|unable to|cannot)\b|n't\b)[^.;\n]{0,20}$")

# An alternative that keeps a restriction from restricting: "must be based
# in the UK or Türkiye", "UK or EU". Spec 078's clause rule
# (`rules._offers_emea`), with the folded Türkiye forms its table cannot see.
_ALTERNATIVE = r"(?:turkiye|turkey|emea|europe|european union|eu|eea|middle east)"
_JOIN = r"\.?\s*(?:,\s*(?:or|and)\b|,|/|\bor\b|\band\b)\s*(?:in\s+)?(?:the\s+)?"
_LISTED = r"[a-z][a-z.\-]*(?:\s+[a-z][a-z.\-]*)?"
_CLAUSE_END = r"\s*(?:$|[.,;:!?)\n]|time\s*zones?\b|timezones?\b|hours\b)"
_OFFERS_ALTERNATIVE = re.compile(
    rf"(?:{_JOIN}{_LISTED}){{0,3}}?{_JOIN}{_ALTERNATIVE}\b(?:{_JOIN}{_LISTED}){{0,3}}?{_CLAUSE_END}"
)

ROUTE_ORDER: tuple[str, ...] = ("employee", "contractor", "eor", "relocation")
VERDICT_ORDER: tuple[str, ...] = (CONFIRMED, EXCLUDED, POSSIBLE, UNKNOWN)
_ROUTE_TABLES: dict[str, tuple[str, ...]] = {
    "employee": ROUTE_EMPLOYEE_PATTERNS,
    "contractor": ROUTE_CONTRACTOR_PATTERNS,
    "eor": ROUTE_EOR_PATTERNS,
    "relocation": ROUTE_RELOCATION_PATTERNS,
}
# The fields each route reads. An employer of record, relocation and an
# employment contract are said in the description; a contract can be in the
# title or the location too ("Frontend Engineer (Contract)").
_ROUTE_FIELDS: dict[str, tuple[str, ...]] = {
    "employee": ("description",),
    "contractor": FIELDS,
    "eor": ("description",),
    "relocation": ("description",),
}

_FOLD = "nfkc-casefold-strip-combining-dotless-i"


# --- folding ---


def fold_with_map(text: str) -> tuple[str, list[int]]:
    """Text folded for matching, and for each folded character the index of
    the character it came from, so a match can be cut from the original.

    Per character: NFKC, case-folded, combining marks dropped, and the
    dotless i (U+0131) read as i. "İ" folds to "i" with a combining dot,
    which is dropped; "ü" loses its diaeresis.
    """
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text or ""):
        composed = unicodedata.normalize("NFKC", char).casefold().replace("\u0131", "i")
        for piece in unicodedata.normalize("NFD", composed):
            if unicodedata.combining(piece):
                continue
            folded.append(piece)
            origin.append(index)
    return "".join(folded), origin


def fold(text: str) -> str:
    return fold_with_map(text)[0]


# --- the version ---


def eligibility_version() -> str:
    """A 12-hex digest of every table this module reads, the fold and the
    verdict order. Not part of `policy_version` (spec 122)."""
    parts = [
        _FOLD,
        "|".join(VERDICT_ORDER),
        "|".join(ROUTE_ORDER),
        *TR_NAMED,
        *RESTRICTION_PATTERNS,
        *US_SCOPE_PATTERNS,
        *EMPLOYMENT_BLOCKER_PATTERNS,
        *EU_PERMIT_PATTERNS,
        *US_PAYROLL_REACH_PATTERNS,
        *REGION_PATTERNS,
        *(pattern for route in ROUTE_ORDER for pattern in _ROUTE_TABLES[route]),
        _OFFERS_ALTERNATIVE.pattern,
        _NEGATION_BEFORE.pattern,
        str(EXCERPT_LIMIT),
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


# --- matching ---


@dataclass(frozen=True)
class _Hit:
    kind: str
    field: str
    start: int
    end: int
    phrase: str


class _Text:
    """One field, folded, with the way back to the original."""

    def __init__(self, field: str, original: str) -> None:
        self.field = field
        self.original = original
        self.folded, self.origin = fold_with_map(original)

    def hits(
        self,
        kind: str,
        patterns: tuple[str, ...],
        *,
        skip_negated: bool = False,
        skip_alternative: bool = False,
    ) -> list[_Hit]:
        found: list[_Hit] = []
        for pattern in patterns:
            for match in re.finditer(pattern, self.folded):
                if skip_negated and _NEGATION_BEFORE.search(self.folded[: match.start()]):
                    continue
                if skip_alternative and _OFFERS_ALTERNATIVE.match(self.folded[match.end() :]):
                    continue
                found.append(_Hit(kind, self.field, match.start(), match.end(), match.group(0)))
        return sorted(found, key=lambda hit: hit.start)

    def excerpt(self, hit: _Hit) -> str:
        """The sentence the hit sits in, as written, at most EXCERPT_LIMIT."""
        if not self.origin:
            return ""
        start = self.origin[hit.start]
        end = self.origin[hit.end - 1] + 1
        text = self.original
        left = max(text.rfind(mark, 0, start) for mark in ".!?\n")
        right_candidates = [i for i in (text.find(mark, end) for mark in ".!?\n") if i != -1]
        right = min(right_candidates) + 1 if right_candidates else len(text)
        left += 1
        if right - left > EXCERPT_LIMIT:
            room = max(0, EXCERPT_LIMIT - (end - start))
            left = max(left, start - room // 2)
            right = min(right, left + EXCERPT_LIMIT)
        return " ".join(text[left:right].split())


def _texts(job: NormalizedJob) -> dict[str, _Text]:
    values = {
        "title": job["title"],
        "location": job["location"],
        "description": job["description"],
    }
    return {name: _Text(name, values[name] or "") for name in FIELDS}


def _description_sha256(job: NormalizedJob) -> str:
    text = job["description"] or ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else ""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def assess_eligibility(job: NormalizedJob) -> EligibilityAssessment:
    """The rule assessment of one posting. Pure apart from the clock: no
    network, no LLM, no configuration beyond this module's tables."""
    texts = _texts(job)

    def collect(
        kind: str,
        patterns: tuple[str, ...],
        fields: tuple[str, ...],
        *,
        skip_negated: bool = False,
        skip_alternative: bool = False,
    ) -> list[_Hit]:
        return [
            hit
            for name in fields
            for hit in texts[name].hits(
                kind, patterns, skip_negated=skip_negated, skip_alternative=skip_alternative
            )
        ]

    tr_named = collect("tr_named", TR_NAMED, ("location", "description"))
    restriction = collect("restriction", RESTRICTION_PATTERNS, FIELDS, skip_alternative=True)
    # Spec 078's US tables, with its location rule: a location naming an EMEA
    # region gives way to no US scope phrase, and nothing overrides W-2.
    us_restriction: list[_Hit] = []
    if not location_names_explicit_emea(job["location"] or ""):
        us_restriction += collect(
            "us_restriction", US_SCOPE_PATTERNS, FIELDS, skip_alternative=True
        )
    us_restriction += collect("us_restriction", EMPLOYMENT_BLOCKER_PATTERNS, FIELDS)
    eu_permit = collect("eu_permit", EU_PERMIT_PATTERNS, ("location", "description"))
    reach = collect("reach", US_PAYROLL_REACH_PATTERNS, ("location", "description"))
    region = collect("region", REGION_PATTERNS, ("location", "description"))
    routes_hit = {
        route: collect(
            f"route_{route}", _ROUTE_TABLES[route], _ROUTE_FIELDS[route], skip_negated=True
        )
        for route in ROUTE_ORDER
    }

    restricted = restriction + us_restriction
    tr_in_location = [hit for hit in tr_named if hit.field == "location"]
    tr_in_description = [hit for hit in tr_named if hit.field == "description"]
    gets_around = routes_hit["eor"] + routes_hit["relocation"]

    deciding: _Hit | None
    if tr_in_location and not restricted:
        verdict, deciding = CONFIRMED, tr_in_location[0]
    elif restricted and not tr_named and not gets_around:
        verdict, deciding = EXCLUDED, restricted[0]
    else:
        reasons: list[list[_Hit]] = [
            tr_in_description if not tr_in_location else [],
            eu_permit,
            reach,
            routes_hit["eor"],
            routes_hit["relocation"],
            routes_hit["contractor"] if region else [],
            tr_named if restricted else [],
        ]
        deciding = next((hits[0] for hits in reasons if hits), None)
        verdict = POSSIBLE if deciding is not None else UNKNOWN

    every = [
        *tr_named,
        *restricted,
        *eu_permit,
        *reach,
        *region,
        *(hit for route in ROUTE_ORDER for hit in routes_hit[route]),
    ]
    matched: list[str] = []
    for hit in every:
        entry = f"{hit.kind}:{hit.phrase}"
        if entry not in matched:
            matched.append(entry)

    return EligibilityAssessment(
        verdict=verdict,
        routes=tuple(route for route in ROUTE_ORDER if routes_hit[route]),
        # Where the rule read: the posting itself, whatever the verdict.
        evidence_url=job["url"] or "",
        excerpt=texts[deciding.field].excerpt(deciding) if deciding is not None else "",
        field=deciding.field if deciding is not None else "",
        matched="|".join(matched),
        checked_at=_now(),
        origin="rule",
        version=eligibility_version(),
        description_sha256=_description_sha256(job),
    )
