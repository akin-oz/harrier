"""What a posting asks for, and which profile facts meet each ask (spec 160).

A letter answers the posting's first asks in the posting's order. Code
splits the posting into sections and keeps the responsibilities and
requirements; a model call names the first five asks, quotes each, and
matches facts to it by id; code checks every quote and id. The writer then
sees the asks and their facts, never the posting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast

from harrier.apply.answers import extract_json_object
from harrier.apply.claims import ClaimCheckError, retry_payload
from harrier.llm import LLMClientError, generate_text
from harrier.llm.jsonparse import loads_tolerant
from harrier.resume.content import ResumeBundle, TruthSources

MAX_ASKS = 5

# --- 1. posting sections ---------------------------------------------------------

ASK_HEADINGS: tuple[str, ...] = (
    "what you'll do",
    "what you will do",
    "responsibilities",
    "your role",
    "the role",
    "requirements",
    "what we look for",
    "what we're looking for",
    "what you bring",
    "you have",
    "qualifications",
    "must have",
    "nice to have",
    "about you",
)

DROPPED_HEADINGS: tuple[str, ...] = (
    "what we offer",
    "benefits",
    "perks",
    "why join",
    "why us",
    "our culture",
    "life at",
    "about us",
    "our story",
    "who we are",
    "compensation",
)

_HEADING_MAX_WORDS = 8


@dataclass(frozen=True)
class Section:
    heading: str
    text: str
    kind: str  # "ask", "dropped" or "other"


def _heading_kind(line: str) -> str | None:
    lowered = line.strip().rstrip(":").strip().lower().replace("\u2019", "'")
    if any(lowered.startswith(heading) for heading in ASK_HEADINGS):
        return "ask"
    if any(lowered.startswith(heading) for heading in DROPPED_HEADINGS):
        return "dropped"
    return None


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.endswith(":"):
        return True
    return len(stripped.split()) <= _HEADING_MAX_WORDS and stripped[-1] not in ".!?;,"


def split_sections(posting: str) -> list[Section]:
    """The posting cut at heading lines. A heading that names neither asks
    nor offer keeps the kind of the section before it."""
    sections: list[Section] = []
    heading = ""
    kind = "other"
    lines: list[str] = []
    for line in posting.splitlines():
        named = _heading_kind(line) if _is_heading(line) else None
        if named is not None:
            sections.append(Section(heading, "\n".join(lines).strip(), kind))
            heading, kind, lines = line.strip(), named, []
        else:
            lines.append(line)
    sections.append(Section(heading, "\n".join(lines).strip(), kind))
    return [section for section in sections if section.text or section.heading]


def ask_text(posting: str) -> tuple[str, list[str]]:
    """The text the analysis reads, and the headings of the dropped sections.

    With an ask heading, the ask sections only. Without one, the whole
    posting minus the dropped sections."""
    sections = split_sections(posting)
    dropped = [section.heading for section in sections if section.kind == "dropped"]
    if any(section.kind == "ask" for section in sections):
        kept = [section for section in sections if section.kind == "ask"]
    else:
        kept = [section for section in sections if section.kind != "dropped"]
    text = "\n".join(
        part for section in kept for part in (section.heading, section.text) if part
    ).strip()
    return text, dropped


# --- 2. the facts and the analysis call ------------------------------------------


def fact_catalogue(sources: TruthSources, bundle: ResumeBundle | None) -> dict[str, str]:
    """Every fact the analysis may match, by id: the track's bullet pool by
    its own ids, then the truth document's asserted lines as t1, t2, ... and
    the achievements document's as a1, a2, ..."""
    facts: dict[str, str] = dict(bundle.bullet_pool) if bundle is not None else {}
    truth_lines = TruthSources(sources.truth_text, "").asserted_lines()
    achievement_lines = TruthSources("", sources.achievements_text).asserted_lines()
    for number, line in enumerate(truth_lines, start=1):
        facts[f"t{number}"] = line.strip()
    for number, line in enumerate(achievement_lines, start=1):
        facts[f"a{number}"] = line.strip()
    return {key: value for key, value in facts.items() if value}


ANALYSIS_PROMPT = """You read a job posting's responsibilities and requirements and match them
to the candidate's verified facts.

Input: ask_text (the posting's responsibilities and requirements), facts (id to
text), company, role.

Return strict JSON only with this shape:
{
  "asks": [{"ask": "string", "quote": "string", "fact_ids": ["string"]}],
  "logistics": {"quote": "string", "fact_ids": ["string"]} or null,
  "product_fact_ids": ["string"]
}

Rules:
- asks: the five asks that matter most for this role, across every section of
  ask_text, listed in the order ask_text gives them. Judge what matters by the
  role's title and the craft it names: the core skills, tools and evidence a
  hiring manager screens for (for a design role, its design process, design
  tools and portfolio) rank above generic duties such as code review,
  collaboration, communication or keeping up with technology. One ask per
  responsibility or requirement; merge two lines that ask for the same thing.
- ask: a short plain name for what is asked, in at most twelve words.
- quote: copied exactly from ask_text, the shortest span that names the ask.
- fact_ids: ids from facts whose text directly shows the candidate meets the
  ask. Do not stretch: a fact that only touches the topic does not match. An
  empty list marks a gap. One fact may serve several asks.
- logistics: only when ask_text states location, remote region, time zone,
  eligibility or contract terms. Quote that text. fact_ids are facts about where
  the candidate works from or how they can work or contract. Otherwise null.
- product_fact_ids: facts that show the candidate used the company's own
  product first hand. Usually an empty list.
- Use only ids that appear in facts. Never invent an id or a quote.
"""


@dataclass(frozen=True)
class Ask:
    rank: int
    ask: str
    quote: str
    fact_ids: tuple[str, ...]

    @property
    def gap(self) -> bool:
        return not self.fact_ids


@dataclass(frozen=True)
class Analysis:
    asks: tuple[Ask, ...] = ()
    logistics_quote: str = ""
    logistics_fact_ids: tuple[str, ...] = ()
    product_fact_ids: tuple[str, ...] = ()
    dropped_sections: tuple[str, ...] = ()
    # Gaps code adds besides the asks with no facts: a link, an experience
    # statement, product experience (spec 160, sections 3 and 5).
    extra_gaps: tuple[str, ...] = field(default=())

    @property
    def gaps(self) -> list[str]:
        return [ask.ask for ask in self.asks if ask.gap] + list(self.extra_gaps)

    def facts_for(self, fact_ids: Sequence[str], facts: Mapping[str, str]) -> list[dict[str, str]]:
        return [{"id": fact_id, "text": facts[fact_id]} for fact_id in fact_ids if fact_id in facts]


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in cast("list[object]", value) if isinstance(item, str))


def parse_analysis(text: str) -> Analysis:
    raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", raw) if isinstance(raw, dict) else {}
    asks: list[Ask] = []
    raw_asks = payload.get("asks")
    for index, item in enumerate(
        cast("list[object]", raw_asks) if isinstance(raw_asks, list) else []
    ):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        ask = entry.get("ask")
        quote = entry.get("quote")
        asks.append(
            Ask(
                rank=index + 1,
                ask=ask.strip() if isinstance(ask, str) else "",
                quote=quote.strip() if isinstance(quote, str) else "",
                fact_ids=_strings(entry.get("fact_ids")),
            )
        )
    logistics_raw = payload.get("logistics")
    logistics = cast("dict[str, object]", logistics_raw) if isinstance(logistics_raw, dict) else {}
    quote = logistics.get("quote")
    return Analysis(
        asks=tuple(asks),
        logistics_quote=quote.strip() if isinstance(quote, str) else "",
        logistics_fact_ids=_strings(logistics.get("fact_ids")),
        product_fact_ids=_strings(payload.get("product_fact_ids")),
    )


def _comparable(text: str) -> str:
    return " ".join(text.replace("\u2019", "'").lower().split())


def analysis_violations(analysis: Analysis, text: str, facts: Mapping[str, str]) -> list[str]:
    """Every way the analysis breaks section 2's checks. Empty when it passes."""
    violations: list[str] = []
    if len(analysis.asks) > MAX_ASKS:
        violations.append(f"more than {MAX_ASKS} asks: {len(analysis.asks)}")
    haystack = _comparable(text)
    last = -1
    for ask in analysis.asks:
        if not ask.ask:
            violations.append(f"ask {ask.rank} has no name")
        position = haystack.find(_comparable(ask.quote)) if ask.quote else -1
        if position < 0:
            violations.append(f"ask {ask.rank} quote not in the posting: {ask.quote}")
        elif position < last:
            violations.append(f"ask {ask.rank} is out of posting order: {ask.quote}")
        else:
            last = position
        violations.extend(
            f"ask {ask.rank} names an unknown fact id: {fact_id}"
            for fact_id in ask.fact_ids
            if fact_id not in facts
        )
    if analysis.logistics_quote and _comparable(analysis.logistics_quote) not in haystack:
        violations.append(f"logistics quote not in the posting: {analysis.logistics_quote}")
    violations.extend(
        f"unknown fact id: {fact_id}"
        for fact_id in (*analysis.logistics_fact_ids, *analysis.product_fact_ids)
        if fact_id not in facts
    )
    return violations


Generate = Callable[[str, str], str]


def _request(generate: Generate, payload: dict[str, object]) -> str:
    try:
        output = generate(ANALYSIS_PROMPT, json.dumps(payload, ensure_ascii=False, indent=2))
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output.strip():
        raise RuntimeError("AI backend returned an empty response")
    return output


def analyse_posting(
    posting: str,
    facts: Mapping[str, str],
    company: str,
    role: str,
    generate: Generate | None = None,
) -> Analysis:
    """The checked analysis, with one retry that names what failed (as spec
    085 gives a letter). Raises `ClaimCheckError` when both attempts fail."""
    text, dropped = ask_text(posting)
    call = generate if generate is not None else _default_generate
    payload: dict[str, object] = {
        "ask_text": text,
        "facts": dict(facts),
        "company": company,
        "role": role,
    }
    output = _request(call, payload)
    for attempt in (1, 2):
        try:
            analysis = parse_analysis(output)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"failed to parse the posting analysis: {exc}") from exc
        violations = analysis_violations(analysis, text, facts)
        if not violations:
            return Analysis(
                asks=analysis.asks[:MAX_ASKS],
                logistics_quote=analysis.logistics_quote,
                logistics_fact_ids=analysis.logistics_fact_ids,
                product_fact_ids=analysis.product_fact_ids,
                dropped_sections=tuple(dropped),
            )
        refusal = ClaimCheckError([f"posting analysis: {v}" for v in violations])
        if attempt == 2:
            raise refusal
        output = _request(call, retry_payload(payload, refusal, output))
    raise AssertionError("unreachable")


def _default_generate(system_prompt: str, user_input: str) -> str:
    # Looked up at call time, so a test that replaces `generate_text` in this
    # module reaches the analysis without reaching a provider.
    return generate_text(system_prompt, user_input)


# --- 5 and 6. the lines code adds, and the company's casing ---------------------

_PORTFOLIO = re.compile(r"\b(?:portfolio|work samples?|case stud(?:y|ies))\b", re.IGNORECASE)
_GITHUB = re.compile(r"\bgithub\b", re.IGNORECASE)
_SITE_LABEL = re.compile(r"\b(?:portfolio|site|website|home)\b", re.IGNORECASE)
_YEARS = re.compile(r"\b\d{1,2}\s*\+?\s*years?\b", re.IGNORECASE)
_SENIORITY = re.compile(r"\b(?:senior|staff|lead|principal)\b", re.IGNORECASE)


def _visible(url: str) -> str:
    return re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE).rstrip("/")


def link_line(text: str, bundle: ResumeBundle | None) -> tuple[str | None, str | None]:
    """The link line the posting asks for, and the gap when the facts hold no
    URL. (None, None) when the posting asks for no link."""
    portfolio = _PORTFOLIO.search(text)
    github = _GITHUB.search(text)
    if portfolio is None and github is None:
        return None, None
    links = bundle.links if bundle is not None else ()
    github_url = bundle.github if bundle is not None else ""
    if portfolio is None:
        label, url = "GitHub", github_url
    else:
        word = portfolio.group(0).lower()
        label = "Work samples" if word.startswith(("work", "case")) else "Portfolio"
        site = next((u for name, u in links if _SITE_LABEL.search(name)), "")
        url = site or (links[0][1] if links else "") or github_url
    if not url:
        return None, "a portfolio link" if portfolio is not None else "a GitHub link"
    return f"{label}: {_visible(url)}", None


def experience_line(
    title: str, text: str, bundle: ResumeBundle | None
) -> tuple[str | None, str | None]:
    """The facts' experience statement, when the posting states years or
    seniority; the gap when it does and the facts hold none."""
    if not (_YEARS.search(text) or _SENIORITY.search(title) or _SENIORITY.search(text)):
        return None, None
    statement = bundle.experience_statement.strip() if bundle is not None else ""
    if not statement:
        return None, "an experience statement"
    return statement, None


def posting_company(company: str, posting: str) -> str:
    """The company as the posting writes it: its most frequent case form,
    else the tracker's name."""
    name = company.strip()
    if not name:
        return company
    forms = re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", posting, re.IGNORECASE)
    if not forms:
        return name
    counts: dict[str, int] = {}
    for form in forms:
        counts[form] = counts.get(form, 0) + 1
    return max(counts, key=lambda form: (counts[form], form != name.lower()))


def asks_document(analysis: Analysis, company: str) -> dict[str, object]:
    """The asks file beside the letter (spec 160, section 3)."""
    return {
        "company": company,
        "asks": [
            {
                "rank": ask.rank,
                "ask": ask.ask,
                "quote": ask.quote,
                "fact_ids": list(ask.fact_ids),
                "gap": ask.gap,
            }
            for ask in analysis.asks
        ],
        "logistics": (
            {"quote": analysis.logistics_quote, "fact_ids": list(analysis.logistics_fact_ids)}
            if analysis.logistics_quote
            else None
        ),
        "dropped_sections": list(analysis.dropped_sections),
        "gaps": analysis.gaps,
    }
