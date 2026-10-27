"""Cover letters (spec 014 port of openai_cover_letters.py).

The kind's shape, banned phrasing validated, PDF or failure. The HTML and
PDF contain only the full letter: no internal section labels ever reach the
recruiter-facing artifact. On the industry and focused kinds the letter
leads with a result and closes with an offer, and the linter holds it
(spec 158).
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import cast

from harrier.apply.answers import extract_json_object, slugify
from harrier.apply.brief import (
    EMPTY_BRIEF,
    Brief,
    Limits,
    brief_instructions,
    limit_violations,
    never_name_hits,
    with_operator_evidence,
)
from harrier.apply.claims import (
    Claim,
    ClaimCheckError,
    ClaimContext,
    NeedsInputError,
    banned_hits,
    check_claims,
    find_placeholders,
    parse_claims,
    retry_payload,
)
from harrier.apply.letterlint import AskCoverage, LintContext, lint_letter
from harrier.apply.postingasks import (
    Analysis,
    analyse_posting,
    ask_text,
    asks_document,
    experience_line,
    fact_catalogue,
    link_line,
    posting_company,
)
from harrier.apply.profile import (
    ApplicationProfileError,
    load_candidate_document,
    load_profile_json,
    load_profile_markdown,
    load_story_bank,
    profile_text,
)
from harrier.apply.review import Review, render_review
from harrier.connection import Connection
from harrier.filestore import artifact_path, files, files_in, join_key, local_file, save_text
from harrier.llm import LLMClientError, generate_text
from harrier.llm.jsonparse import loads_tolerant
from harrier.paths import repo_root
from harrier.resume.content import (
    ResumeBundle,
    ResumeBundleError,
    TruthSources,
    forbidden_hits,
    load_forbidden_phrases,
    load_skill_vocabulary,
    load_truth_sources,
    names_unsafe_scheme,
)
from harrier.resume.facts import ResumeContentError, professional_experience_years
from harrier.resume.markdown import normalize_visible_role_title, normalize_visible_url_text
from harrier.resume.pdf import render_pdf, render_validated_pdf, validate_rendered_pdf
from harrier.tracks import Scope, rules_for
from harrier.voices import assemble

logger = logging.getLogger(__name__)

# Anchored to the repository, not the working directory: a hosted run starts
# in its owner's run directory (spec 106), where `templates/` does not exist.
TEMPLATE_DIR = repo_root() / "templates"
HTML_TEMPLATE = "cover-letter-template.html"
CSS_TEMPLATE = "cover-letter-template.css"

BANNED_PHRASES = [
    "fit:",
    "tailored for",
    "i am thrilled",
    "i am passionate about",
    "dream role",
    "amazing opportunity",
    "cutting-edge",
    "world-class",
    "dynamic environment",
    "innovative solutions",
    "leverage",
    "spearheaded",
    "drove transformation",
    "excited to join",
    "fast-paced environment",
    "i would be honored",
    "i can send those on request",
    "most relevant to this role",
    "practically",
    "that aligns with the kind of",
    "add immediate value",
    "short technical conversation",
    "current priorities",
    "i bring",
    "i offer",
    "i come with",
]

# The rules every kind's letter prompt carries, whatever its voice (spec 101):
# the hard constraints the checks below enforce on every letter, the
# no-invention rules, the banned phrasing, the claims rules and the return
# format. The kind supplies the voice, the structure and the length
# (`harrier.voices`), around these three segments.
LETTER_SHARED: tuple[str, ...] = (
    """Hard constraints:
- No "Fit:"
- No bullet-list voice
- No internal or debug wording
- No "I can send those on request"
- No fake enthusiasm
- No flattering praise
- No corporate filler
- No invented experience
- Use the application profile for positioning and safe framing only. Every
  fact about the candidate must come from resume_truth_source_md,
  latest_project_achievements_md, or candidate_json's candidate.location
  and candidate.eu_entity_status.
- story_bank is context only: it shows how a result was achieved, but it
  was written by a model and is never evidence. A fact you take from it
  must also be stated in the truth sources, and you cite that line.
""",
    """Banned phrasing:
"""
    + "\n".join(f"- {phrase}" for phrase in BANNED_PHRASES)
    + """

Claims and evidence:
- Declare every factual sentence you write as a claim: the sentence exactly
  as it appears in your letter, whether it is about the candidate or the
  employer, and one or more evidence fragments.
- Evidence is quoted verbatim. Candidate evidence comes only from
  resume_truth_source_md, latest_project_achievements_md, or candidate_json's
  candidate.location or candidate.eu_entity_status. Employer evidence comes
  only from job_description_text.
- Never cite job_description_text or the application profile as candidate
  evidence. A requirement the posting lists is something the employer wants,
  not something the candidate did. If a sentence about the candidate has no
  evidence in the truth sources, leave it out.
- Keep every number exactly as the evidence states it. A total over a period
  is never a rate, and a percentage keeps its sign.
- If the evidence describes a demo or synthetic data, say so in the sentence.
- Name a technology only if the truth sources show the candidate used it.
- If the letter needs a concrete example and none exists in the material,
  write [[TODO: what is needed]] instead of inventing one.
- Say "convention" or "warning" rather than "enforced" unless the material
  shows something enforcing it.
- Do not state model names, versions or other time-sensitive tool details
  unless the material supplies them.
""",
    """
Return strict JSON only with this shape:
{
  "short_version": "string",
  "full_version": "string",
  "claims": [
    {"sentence": "string", "about": "candidate or employer", "evidence": ["string"]}
  ],
  "gaps": [
    {"requirement": "string", "posting_evidence": "string"}
  ]
}

List in gaps each thing the posting asks for that the candidate evidence
cannot back, with posting_evidence quoted verbatim from job_description_text.
Leave gaps empty when there is none. A gap never appears in the letter.

FORMATTING: Never use an em dash, an en dash, a double hyphen, or a hyphen with a space on both sides as punctuation. Use a comma, a colon, or a new sentence instead. A hyphen inside a compound word is fine.
""",
)


def letter_prompt(kind: str) -> str:
    """The cover letter prompt for a track of this kind (spec 101)."""
    return assemble(rules_for(kind).letter_voice, LETTER_SHARED)


COVER_LETTERS = "cover-letters"


def cover_letter_keys_for(company: str, role: str, prefix: str = COVER_LETTERS) -> dict[str, str]:
    """Where a cover letter run puts its files, by artifact kind.

    Shared by the writer below and by the reader that serves an artifact back,
    so the two cannot disagree about where a letter is (spec 047).
    """
    slug = slugify(f"{company}-{role}")
    return {
        "markdown": join_key(prefix, f"{slug}.md"),
        "html": join_key(prefix, f"{slug}.html"),
        "pdf": join_key(prefix, f"{slug}.pdf"),
    }


def cover_letter_asks_key_for(company: str, role: str, prefix: str = COVER_LETTERS) -> str:
    """The asks file beside the letter (spec 160). Kept out of
    `cover_letter_keys_for`, whose kinds the artifact endpoint serves."""
    return join_key(prefix, f"{slugify(f'{company}-{role}')}.asks.json")


def cover_letter_paths_for(
    company: str, role: str, output_dir: Path | None = None
) -> dict[str, Path]:
    """The local files behind `cover_letter_keys_for`, or in `output_dir`."""
    with files_in(output_dir, COVER_LETTERS) as prefix:
        keys = cover_letter_keys_for(company, role, prefix)
        return {artifact: local_file(key) for artifact, key in keys.items()}


def candidate_contact(conn: Connection) -> dict[str, str]:
    candidate = load_candidate_document(conn)
    block_raw = candidate.get("candidate")
    block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else {}
    sources = load_truth_sources(conn)
    email_match = re.search(r"- Email: (.+)", sources.truth_text)
    linkedin_match = re.search(r"- LinkedIn: (.+)", sources.truth_text)
    linkedin_url = str(block.get("linkedin") or "") or (
        linkedin_match.group(1).strip() if linkedin_match else ""
    )
    return {
        "name": str(block.get("name", "")),
        "location": str(block.get("location", "")),
        "email": str(block.get("email") or "")
        or (email_match.group(1).strip() if email_match else ""),
        "linkedin_url": linkedin_url,
        "linkedin_label": normalize_visible_url_text(linkedin_url),
    }


def build_cover_letter_payload(
    conn: Connection,
    company: str,
    role: str,
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    extra_notes: str | None = None,
    brief: Brief = EMPTY_BRIEF,
    *,
    scope: Scope,
) -> dict[str, object]:
    tracker_metadata = None
    if tracker_row:
        tracker_metadata = {
            "company": tracker_row.get("company", ""),
            "title": tracker_row.get("title", ""),
            "location": tracker_row.get("location", ""),
            "url": tracker_row.get("url", ""),
            "source": tracker_row.get("source", ""),
            "fit_score": tracker_row.get("fit_score", ""),
            "status": tracker_row.get("status", ""),
            "next_action": tracker_row.get("next_action", ""),
            "notes": tracker_row.get("notes", ""),
        }
    candidate = load_candidate_document(conn)
    block_raw = candidate.get("candidate")
    block = cast("dict[str, object]", block_raw) if isinstance(block_raw, dict) else {}
    sources = load_truth_sources(conn)
    links: dict[str, object] = {}
    if rules_for(scope.track.kind).header_links:
        # The closing names the demo and the repository (spec 140): the
        # facts' links, which the operator writes and vouches for.
        links = {"candidate_links": _candidate_links(conn, scope)}
    return {
        **links,
        "candidate_name": str(block.get("name", "")),
        "company": company,
        "role": role,
        "job_url": job_url or "",
        "tracker_metadata": tracker_metadata,
        "job_description_text": jd_text or "",
        "extra_notes": extra_notes or "",
        # The application brief (spec 066). Empty values when there is none.
        "never_name": list(brief.never_name),
        "employer_guidance": brief.employer_guidance,
        "guidance_url": brief.guidance_url,
        "operator_evidence": list(brief.evidence),
        "truth_sources": {
            "resume_truth_source_md": sources.truth_text,
            "latest_project_achievements_md": sources.achievements_text,
            "candidate_json": candidate,
            # The four STAR strings of each story, as context only: the
            # offer evaluator's model wrote them, so a letter never cites
            # them (spec 158, amendment 15).
            "story_bank": load_story_bank(conn),
            "application_profile_md": load_profile_markdown(conn, scope),
            "application_profile_json": load_profile_json(conn, scope),
        },
    }


def _candidate_links(conn: Connection, scope: Scope) -> dict[str, object]:
    from harrier.resume.content import ResumeBundleError, load_bundle

    try:
        bundle = load_bundle(conn, scope)
    except ResumeBundleError:
        return {"github": "", "links": []}
    return {
        "github": bundle.github,
        "links": [{"label": label, "url": url} for label, url in bundle.links],
    }


def parse_cover_letter_response(text: str) -> dict[str, str]:
    payload_raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    short_version = str(payload.get("short_version", "")).strip()
    full_version = str(payload.get("full_version", "")).strip()
    if not short_version or not full_version:
        raise ValueError("AI response was missing cover letter fields")
    return {"short_version": short_version, "full_version": full_version}


@dataclass(frozen=True)
class Gap:
    """Something the posting asks for that the evidence cannot back (spec 158)."""

    requirement: str
    posting_evidence: str


@dataclass(frozen=True)
class FactUsed:
    """One evidence fragment a candidate claim rests on, and the document code
    found it in (spec 158)."""

    sentence: str
    evidence: str
    source: str


def parse_cover_letter_gaps(text: str) -> list[Gap]:
    """The gaps the model listed. An absent key is an empty list."""
    payload_raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    raw = payload.get("gaps")
    if not isinstance(raw, list):
        return []
    gaps: list[Gap] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        entry = cast("dict[str, object]", item)
        # A JSON null or any other non-string is empty, never the text "None".
        raw_requirement = entry.get("requirement")
        raw_evidence = entry.get("posting_evidence")
        requirement = raw_requirement.strip() if isinstance(raw_requirement, str) else ""
        evidence = raw_evidence.strip() if isinstance(raw_evidence, str) else ""
        if requirement:
            gaps.append(Gap(requirement=requirement, posting_evidence=evidence))
    return gaps


def parse_cover_letter_claims(text: str) -> list[Claim]:
    """The claims the model declared alongside the letter (spec 065)."""
    payload_raw: object = loads_tolerant(extract_json_object(text))
    payload = cast("dict[str, object]", payload_raw) if isinstance(payload_raw, dict) else {}
    return parse_claims(payload.get("claims"))


def strip_banned_phrases(text: str) -> str:
    value = text
    for banned in BANNED_PHRASES:
        value = re.sub(re.escape(banned), "", value, flags=re.IGNORECASE)
    return value


# The block range and word cap are the kind's (`KindRules`); the floor on a
# paragraph holds for every kind, except a linted kind's role line.
MIN_PARAGRAPH_WORDS = 8

# The kinds whose letter leads with a result and closes with an offer, and
# which the linter holds (spec 158).
LINTED_KINDS = frozenset({"industry", "focused"})


def normalize_cover_letter_text(text: str, *, is_full: bool) -> str:
    """Formatting only: whitespace, leading bullet markers, and `Paragraph N:`
    labels (spec 065, rule N1).

    This used to delete banned phrases as substrings, drop short paragraphs,
    keep the first three, and trim to 240 words. Each of those changed what
    the letter said without saying so, and the first one cut words in half.
    They are now checks that refuse in `cover_letter_violations`.
    """
    value = (text or "").replace("\r\n", "\n")
    value = re.sub(r"^[\-\*•]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*paragraph\s*\d+\s*:\s*", "", value, flags=re.IGNORECASE | re.MULTILINE)
    if is_full:
        blocks = [
            re.sub(r"\s+", " ", block).strip()
            for block in re.split(r"\n\s*\n", value)
            if block.strip()
        ]
        value = "\n\n".join(blocks)
    else:
        value = re.sub(r"\s+", " ", value).strip()
    return value.strip()


def cover_letter_violations(
    letter: dict[str, str],
    limits: Limits | None = None,
    *,
    kind: str,
    lint: LintContext | None = None,
    added: tuple[str, ...] = (),
) -> list[str]:
    """Every shape and phrasing rule the letter breaks (spec 065, rule N1).

    A brief's stated limits replace the kind's defaults and apply to the
    full letter, the version that is sent (spec 066, B2). The industry kind
    holds five to eight blocks and the focused six to eight, each at most
    220 words (spec 158); the academic kind has neither, and its length is
    the page gate's (spec 101). With `lint`, the linter's rules join them on
    the industry and focused kinds.
    """
    rules = rules_for(kind)
    stated = limits or Limits()
    linted = kind in LINTED_KINDS
    violations = [
        f"banned phrase: {phrase}"
        for phrase in banned_hits(
            BANNED_PHRASES, f"{letter['short_version']}\n{letter['full_version']}"
        )
    ]
    full = letter["full_version"]
    if "\n- " in full or full.lstrip().startswith("- "):
        violations.append("cover letter should not use bullet-list voice")
    paragraphs = [block for block in re.split(r"\n\s*\n", full) if block.strip()]
    if stated.paragraphs is None:
        expected = rules.letter_blocks
        if expected is not None and not expected[0] <= len(paragraphs) <= expected[1]:
            low, high = expected
            violations.append(
                f"cover letter should contain {low} to {high} blocks, role line and close"
                f" included: it has {len(paragraphs)}"
            )
    else:
        if len(paragraphs) < stated.paragraphs:
            violations.append(
                f"cover letter should contain {stated.paragraphs} paragraphs (letter.paragraphs)"
            )
        if len(paragraphs) > stated.paragraphs:
            violations.append(
                f"over the stated limit: letter.paragraphs {stated.paragraphs},"
                f" full_version has {len(paragraphs)}"
            )
    # A linted kind's first block is its role line, short by design, and the
    # link and experience lines code adds are short too (spec 160).
    floor_from = 1 if linted else 0
    for paragraph in paragraphs[floor_from:]:
        if paragraph.strip() in added:
            continue
        if len(paragraph.split()) < MIN_PARAGRAPH_WORDS:
            violations.append(f"stub paragraph: {paragraph}")
    words = len(full.split())
    cap = rules.letter_max_words
    if stated.max_words is None and cap is not None and words > cap:
        violations.append(f"over the word limit: {words} words, at most {cap}")
    violations.extend(limit_violations(full, stated, "letter", "full_version"))
    if linted and lint is not None:
        # L3 is the kind's cap above, which a brief's max_words replaces.
        # L8 is dropped because a number in a declared employer sentence
        # comes from the posting, which L8 does not read. A candidate
        # number passes C5 only from verified evidence (spec 065), and the
        # hook's number must be candidate evidence (L4).
        # A brief that states any letter limit replaces the word range too
        # (spec 160), so the floor goes with the cap.
        dropped: tuple[str, ...] = ("over the word limit", "number not in the evidence")
        if stated.paragraphs is not None or stated.max_words is not None:
            dropped = (*dropped, "under the word floor")
        violations.extend(
            violation
            for violation in lint_letter(full, lint, letter["short_version"])
            if not violation.startswith(dropped)
        )
    return list(dict.fromkeys(violations))


@dataclass(frozen=True)
class LetterDraft:
    """A letter that passed every check, with the claims it rests on, which
    the draft's "To verify" section lists (spec 066, B9)."""

    short_version: str
    full_version: str
    claims: tuple[Claim, ...] = ()
    # What the posting asks for that the evidence cannot back, and the
    # document each candidate fact came from (spec 158).
    gaps: tuple[Gap, ...] = ()
    facts_used: tuple[FactUsed, ...] = ()
    # The posting analysis behind the letter, written beside it, and the
    # company as the posting writes it (spec 160). None and "" on the
    # academic kind.
    asks: dict[str, object] | None = None
    company: str = ""


def validate_cover_letter(letter: dict[str, str], *, kind: str) -> None:
    violations = cover_letter_violations(letter, kind=kind)
    if violations:
        raise ClaimCheckError(violations)


def generate_cover_letter(
    conn: Connection,
    company: str,
    role: str,
    job_url: str | None = None,
    tracker_row: dict[str, str] | None = None,
    jd_text: str | None = None,
    extra_notes: str | None = None,
    brief: Brief = EMPTY_BRIEF,
    *,
    scope: Scope,
) -> LetterDraft:
    """The letter for a job on the scope's track: its kind's prompt, its own
    application profile, and every check (specs 065, 101). The industry and
    focused kinds answer the posting's asks first (spec 160)."""
    kind = scope.track.kind
    if kind in LINTED_KINDS:
        return _letter_from_asks(conn, company, role, jd_text, extra_notes, brief, scope=scope)
    payload = build_cover_letter_payload(
        conn,
        company,
        role,
        job_url=job_url,
        tracker_row=tracker_row,
        jd_text=jd_text,
        extra_notes=extra_notes,
        brief=brief,
        scope=scope,
    )
    system_prompt = letter_prompt(kind) + brief_instructions(brief, "letter")
    output_text = _request_letter(system_prompt, payload)
    try:
        return _checked_letter(conn, output_text, company, role, jd_text, brief, scope)
    except ClaimCheckError as refusal:
        # One retry that says what failed (spec 085). Parse and transport
        # failures are not refusals and are not caught here.
        logger.warning(
            "cover letter refused on attempt 1, retrying once: %s", "; ".join(refusal.violations)
        )
        output_text = _request_letter(system_prompt, retry_payload(payload, refusal, output_text))
    return _checked_letter(conn, output_text, company, role, jd_text, brief, scope)


@dataclass(frozen=True)
class AskPlan:
    """What the writer is given and what code adds around it (spec 160)."""

    analysis: Analysis
    facts: dict[str, str]
    company: str
    link: str | None = None
    experience: str | None = None
    # The casing L14 holds the letter to: the posting's form of the name,
    # "" when the posting never writes it (amendment 2).
    company_form: str = ""
    # Whether the letter has a logistics block before its close: when the
    # posting raised location or terms, and always on the focused kind
    # (spec 140), so the link line goes before it (review of #410).
    logistics_block: bool = False


def _bundle_or_none(conn: Connection, scope: Scope) -> ResumeBundle | None:
    from harrier.resume.content import load_bundle

    try:
        return load_bundle(conn, scope)
    except ResumeBundleError:
        return None


def plan_letter(
    conn: Connection, company: str, role: str, jd_text: str | None, *, scope: Scope
) -> AskPlan:
    """The posting analysis, the facts it matched, and the lines code adds.

    Raises `ClaimCheckError` when the posting has asks but none matches a
    fact (spec 160, failure modes)."""
    sources = load_truth_sources(conn)
    bundle = _bundle_or_none(conn, scope)
    facts = fact_catalogue(sources, bundle)
    posting = jd_text or ""
    display = posting_company(company, posting)
    form = display if re.search(rf"(?<!\w){re.escape(display)}(?!\w)", posting) else ""
    if not posting.strip():
        unset = () if track_motivation(conn, scope) else (MOTIVATION_GAP,)
        return AskPlan(analysis=Analysis(extra_gaps=unset), facts=facts, company=display)
    analysis = analyse_posting(posting, facts, display, role)
    if analysis.asks and all(ask.gap for ask in analysis.asks):
        asks = "; ".join(f"{ask.rank}. {ask.ask}" for ask in analysis.asks)
        raise ClaimCheckError([f"no posting ask matches a profile fact: {asks}"])
    text, _ = ask_text(posting)
    link, link_gap = link_line(text, bundle)
    experience, experience_gap = experience_line(role, text, bundle)
    gaps = [gap for gap in (link_gap, experience_gap) if gap]
    if not analysis.product_fact_ids:
        gaps.append(f"first-hand experience of {display}'s product")
    if not track_motivation(conn, scope):
        gaps.append(MOTIVATION_GAP)
    return AskPlan(
        analysis=replace(analysis, extra_gaps=tuple(gaps)),
        facts=facts,
        company=display,
        link=link,
        experience=experience,
        company_form=form,
        logistics_block=scope.track.kind == "focused"
        or bool(analysis.logistics_quote or analysis.logistics_fact_ids),
    )


MOTIVATION_GAP = "a motivation statement"


def track_motivation(conn: Connection, scope: Scope) -> str:
    """The track's `motivation` (spec 165): the operator's stated reason for
    wanting this kind of role. "" when the application profile has none or
    cannot be read."""
    try:
        profile = load_profile_json(conn, scope)
    except ApplicationProfileError:
        return ""
    return str(profile.get("motivation") or "").strip()


def writer_payload(
    conn: Connection,
    plan: AskPlan,
    role: str,
    extra_notes: str | None,
    brief: Brief,
    *,
    scope: Scope,
) -> dict[str, object]:
    """Only the asks, their matched facts and the company (spec 160, section
    4), with the brief's instructions (spec 066). No posting text, story
    bank, application profile or unmatched fact."""
    analysis = plan.analysis
    payload: dict[str, object] = {
        "company": plan.company,
        "role": role,
        "asks": [
            {
                "rank": ask.rank,
                "ask": ask.ask,
                "facts": analysis.facts_for(ask.fact_ids, plan.facts),
            }
            for ask in analysis.asks
        ],
        "logistics_facts": [
            fact["text"] for fact in analysis.facts_for(analysis.logistics_fact_ids, plan.facts)
        ],
        "product_facts": [
            fact["text"] for fact in analysis.facts_for(analysis.product_fact_ids, plan.facts)
        ],
        # The brief's instructions (spec 066): the operator's, not the posting.
        "extra_notes": extra_notes or "",
        "never_name": list(brief.never_name),
        "employer_guidance": brief.employer_guidance,
        "guidance_url": brief.guidance_url,
        "operator_evidence": list(brief.evidence),
        # A stated preference, never evidence (spec 165).
        "motivation": track_motivation(conn, scope),
    }
    if not analysis.asks:
        # No posting to rank against: every fact, unranked (amendment 1).
        payload["facts"] = [{"id": key, "text": text} for key, text in plan.facts.items()]
    if scope.track.kind == "focused":
        profile = load_profile_json(conn, scope)
        payload["scope_reason"] = str(profile.get("scope_reason") or "")
        if not payload["logistics_facts"]:
            # The focused letter always says how the candidate contracts
            # (spec 140), from the two candidate fields.
            try:
                payload["logistics_facts"] = _candidate_facts(load_candidate_document(conn))
            except ApplicationProfileError:
                payload["logistics_facts"] = []
    return payload


def _letter_from_asks(
    conn: Connection,
    company: str,
    role: str,
    jd_text: str | None,
    extra_notes: str | None,
    brief: Brief,
    *,
    scope: Scope,
) -> LetterDraft:
    plan = plan_letter(conn, company, role, jd_text, scope=scope)
    payload = writer_payload(conn, plan, role, extra_notes, brief, scope=scope)
    system_prompt = letter_prompt(scope.track.kind) + brief_instructions(brief, "letter")
    output_text = _request_letter(system_prompt, payload)
    try:
        return _checked_letter(conn, output_text, company, role, jd_text, brief, scope, plan)
    except ClaimCheckError as refusal:
        logger.warning(
            "cover letter refused on attempt 1, retrying once: %s", "; ".join(refusal.violations)
        )
        output_text = _request_letter(system_prompt, retry_payload(payload, refusal, output_text))
    return _checked_letter(conn, output_text, company, role, jd_text, brief, scope, plan)


def _with_added_lines(full: str, plan: AskPlan) -> str:
    """The writer's blocks with the experience line after the hook and the
    link line after the proof, before logistics or the close (section 5)."""
    blocks = [block for block in full.split("\n\n") if block.strip()]
    if plan.experience and plan.experience not in blocks:
        blocks.insert(min(2, len(blocks)), plan.experience)
    if plan.link and plan.link not in blocks:
        before = 2 if plan.logistics_block else 1
        blocks.insert(max(len(blocks) - before, 1), plan.link)
    return "\n\n".join(blocks)


def _request_letter(system_prompt: str, payload: dict[str, object]) -> str:
    try:
        output_text = generate_text(
            system_prompt, json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except LLMClientError as exc:
        raise RuntimeError(f"AI request failed: {exc}") from exc
    if not output_text.strip():
        raise RuntimeError("AI backend returned an empty response")
    return output_text


def _checked_letter(
    conn: Connection,
    output_text: str,
    company: str,
    role: str,
    jd_text: str | None,
    brief: Brief,
    scope: Scope,
    plan: AskPlan | None = None,
) -> LetterDraft:
    """One response parsed and put through every check. Raises
    `ClaimCheckError` with every violation when it is refused. With a plan,
    code adds its lines first and the asks rules run (spec 160)."""
    try:
        parsed = parse_cover_letter_response(output_text)
        claims = parse_cover_letter_claims(output_text)
        gaps = parse_cover_letter_gaps(output_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to parse AI response: {exc}") from exc
    full = normalize_cover_letter_text(parsed["full_version"], is_full=True)
    if plan is not None:
        full = _with_added_lines(full, plan)
        if plan.experience:
            # The facts' own statement, checked by spec 071 when stored.
            claims = [*claims, Claim(plan.experience, "candidate", (plan.experience,))]
    letter = {
        "short_version": normalize_cover_letter_text(parsed["short_version"], is_full=False),
        "full_version": full,
    }
    texts = [letter["short_version"], letter["full_version"]]
    # The pool joins the evidence only where the writer was given it, so the
    # academic letter's evidence is spec 158's (spec 160).
    documents = letter_evidence(
        conn,
        brief,
        scope=scope if plan is not None else None,
        experience=plan.experience if plan is not None else None,
    )
    sources = _combined(documents)
    posting = "\n".join(part for part in (jd_text or "", brief.employer_guidance) if part)
    coverage: tuple[AskCoverage, ...] = ()
    if plan is not None:
        coverage = tuple(
            AskCoverage(
                rank=ask.rank,
                ask=ask.ask,
                fact_texts=tuple(plan.facts[f] for f in ask.fact_ids if f in plan.facts),
                listed_as_gap=ask.gap,
            )
            for ask in plan.analysis.asks[:3]
        )
    lint = LintContext(
        # Asserted lines only: no denied text, no disclaimer section.
        evidence="\n".join(sources.asserted_lines()),
        posting=jd_text or "",
        company=company,
        role=role,
        professional_years=_professional_years(conn, scope),
        company_form=plan.company_form if plan is not None else "",
        asks=coverage,
        cited=tuple(
            fragment
            for claim in claims
            if claim.about == "candidate"
            for fragment in claim.evidence
        ),
        link_line=plan.link if plan is not None else None,
    )
    added = tuple(line for line in ((plan.link, plan.experience) if plan else ()) if line)
    if plan is not None:
        # L21 reads the facts the writer was given under `asks` (spec 165).
        given = dict.fromkeys(f for ask in plan.analysis.asks for f in ask.fact_ids)
        lint = replace(
            lint,
            fact_texts=tuple((f, plan.facts[f]) for f in given if f in plan.facts),
            added_lines=added,
        )
    if plan is not None:
        # The analysis owns the gaps; the writer was told to leave them
        # empty and never saw the posting to quote (spec 160, section 4).
        gaps = []
    violations = cover_letter_violations(
        letter, brief.letter, kind=scope.track.kind, lint=lint, added=added
    )
    violations.extend(
        f"gap evidence not in posting: {gap.posting_evidence or gap.requirement}"
        for gap in gaps
        if not gap.posting_evidence or _norm(gap.posting_evidence) not in _norm(posting)
    )
    violations.extend(
        f"named a redacted name: {name}"
        for name in never_name_hits(brief.never_name, "\n".join(texts))
    )
    # The candidate's own never-claim list. Spec 034 required it on the
    # letter; only the resume validator was calling it.
    violations.extend(
        f"forbidden phrase: {phrase}"
        for phrase in forbidden_hits(load_forbidden_phrases(conn), "\n".join(texts))
    )
    context = ClaimContext(
        sources=sources,
        posting=posting,
        company=company,
        role=role,
        vocabulary=load_skill_vocabulary(conn),
        profile=profile_text(conn, scope),
    )
    violations.extend(check_claims(texts, claims, context))
    if violations:
        raise ClaimCheckError(violations)
    asks: dict[str, object] | None = None
    if plan is not None:
        asks = asks_document(plan.analysis, plan.company)
        quotes = {ask.ask: ask.quote for ask in plan.analysis.asks}
        gaps = [
            *(Gap(requirement=g, posting_evidence=quotes.get(g, "")) for g in plan.analysis.gaps),
            *gaps,
        ]
    return LetterDraft(
        short_version=letter["short_version"],
        full_version=letter["full_version"],
        claims=tuple(claims),
        gaps=tuple(gaps),
        facts_used=tuple(facts_used(claims, documents)),
        asks=asks,
        company=plan.company if plan is not None else "",
    )


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# The candidate document's fields a letter may cite (spec 158, amendment
# 10): where the candidate works from and how they can contract. The rest of
# the document is search configuration (titles to include or exclude,
# scoring weights, preferences), which states no fact about the candidate.
CANDIDATE_EVIDENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("candidate", "location"),
    ("candidate", "eu_entity_status"),
)


def _candidate_facts(document: dict[str, object]) -> list[str]:
    """The allowlisted string values of the candidate document, one a line."""
    facts: list[str] = []
    for block_name, field_name in CANDIDATE_EVIDENCE_FIELDS:
        block = document.get(block_name)
        if not isinstance(block, dict):
            continue
        value = cast("dict[str, object]", block).get(field_name)
        if isinstance(value, str) and value.strip():
            facts.append(" ".join(value.split()))
    return facts


def letter_evidence(
    conn: Connection,
    brief: Brief = EMPTY_BRIEF,
    *,
    scope: Scope | None = None,
    experience: str | None = None,
) -> dict[str, TruthSources]:
    """Each document a letter's candidate claim may cite, by the name the
    draft's "Facts used" gives it (spec 158). The truth and achievements
    documents as today; the candidate document's allowlisted fields; and
    the brief's evidence (spec 066). The story bank is not one: it is
    unchecked model output, carried in the payload as context only
    (amendment 15)."""
    truth = load_truth_sources(conn)
    try:
        candidate = "\n".join(_candidate_facts(load_candidate_document(conn)))
    except ApplicationProfileError:
        candidate = ""
    operator = with_operator_evidence(TruthSources("", ""), *brief.evidence).achievements_text
    documents = {
        "resume_truth": TruthSources(truth_text=truth.truth_text, achievements_text=""),
        "achievements": TruthSources(truth_text="", achievements_text=truth.achievements_text),
        "candidate": TruthSources(truth_text="", achievements_text=candidate),
        "operator_evidence": TruthSources(truth_text="", achievements_text=operator),
    }
    if scope is not None:
        # The track's bullet pool: the verified lines the CV is built from,
        # which the writer is given by id (spec 160, section 4).
        bundle = _bundle_or_none(conn, scope)
        pool = "\n".join(bundle.bullet_pool.values()) if bundle is not None else ""
        documents["bullet_pool"] = TruthSources(truth_text="", achievements_text=pool)
    if experience:
        documents["experience_statement"] = TruthSources(
            truth_text="", achievements_text=experience
        )
    return documents


def _combined(documents: dict[str, TruthSources]) -> TruthSources:
    """One `TruthSources` over every letter document, each under its own
    heading so one document's closing section cannot swallow the next."""
    extra = "".join(
        f"\n\n## Letter evidence: {name}\n\n{sources.achievements_text}\n"
        for name, sources in documents.items()
        if name not in ("resume_truth", "achievements") and sources.achievements_text.strip()
    )
    return TruthSources(
        truth_text=documents["resume_truth"].truth_text,
        achievements_text=documents["achievements"].achievements_text + extra,
    )


def facts_used(claims: list[Claim], documents: dict[str, TruthSources]) -> list[FactUsed]:
    """Each candidate claim's evidence, with the first document that asserts
    it. Code finds the document; the model does not name it."""
    used: list[FactUsed] = []
    for claim in claims:
        if claim.about != "candidate":
            continue
        for fragment in claim.evidence:
            source = next(
                (name for name, sources in documents.items() if sources.contains(fragment)),
                "not found",
            )
            used.append(FactUsed(sentence=claim.sentence, evidence=fragment, source=source))
    return used


def _professional_years(conn: Connection, scope: Scope) -> int | None:
    """The record's completed professional years, or None when the record
    cannot be read (L9 then checks only for decades)."""
    from harrier.resume.content import load_bundle

    try:
        return professional_experience_years(load_bundle(conn, scope))
    except (ResumeBundleError, ResumeContentError, ValueError):
        return None


def render_cover_letter_markdown(
    company: str,
    role: str,
    job_url: str | None,
    short_version: str,
    full_version: str,
    review_section: str = "",
    gaps: tuple[Gap, ...] | None = None,
    facts: tuple[FactUsed, ...] | None = None,
) -> str:
    """The draft. With `gaps` and `facts` (spec 158), a "Gaps" and a "Facts
    used" section follow the letter; neither reaches the HTML or the PDF."""
    lines = [
        "# Cover Letter",
        "",
        f"- Company: {company}",
        f"- Role: {role}",
    ]
    if job_url:
        lines.append(f"- Job URL: {job_url}")
    lines.extend(["", "## Short Version", short_version, "", "## Full Version", full_version, ""])
    if gaps is not None:
        lines.extend(["## Gaps", ""])
        lines.extend(
            [
                f'- {gap.requirement}: "{gap.posting_evidence}"'
                if gap.posting_evidence
                else f"- {gap.requirement}"
                for gap in gaps
            ]
            or ["None."]
        )
        lines.append("")
    if facts is not None:
        lines.extend(["## Facts used", ""])
        lines.extend(
            [f'- "{fact.sentence}" rests on "{fact.evidence}" ({fact.source})' for fact in facts]
            or ["None."]
        )
        lines.append("")
    markdown = "\n".join(lines).rstrip() + "\n"
    return markdown + ("\n" + review_section if review_section else "")


def markdown_to_html_paragraphs(text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    return "\n".join(f"<p>{html.escape(block)}</p>" for block in blocks)


SALUTATION = "Dear Hiring Manager,"
SIGN_OFF = "Kind regards,"


def letter_frame(kind: str, name: str) -> dict[str, str]:
    """The salutation above the letter and the sign-off below it (spec 165).
    The industry letter has both; the academic letter neither, and the
    focused template writes its own."""
    if kind != "industry":
        return {"salutation_html": "", "sign_off_html": ""}
    return {
        "salutation_html": f'<p class="salutation">{html.escape(SALUTATION)}</p>',
        "sign_off_html": (
            f'<p class="sign-off">{html.escape(SIGN_OFF)}</p>\n'
            f'<p class="signature">{html.escape(name)}</p>'
        ),
    }


def render_cover_letter_html(
    conn: Connection,
    company: str,
    role: str,
    full_version: str,
    template_dir: Path | None = None,
    *,
    kind: str = "industry",
) -> str:
    """The letter's HTML, in the template the kind names (spec 140). The
    focused template has no header: a salutation naming the company above the
    paragraphs, and the candidate's name below them."""
    directory = template_dir if template_dir is not None else TEMPLATE_DIR
    template = (directory / rules_for(kind).letter_template).read_text(encoding="utf-8")
    css = (directory / CSS_TEMPLATE).read_text(encoding="utf-8")
    contact = candidate_contact(conn)
    # The value goes into an href, and escaping does not disable a scheme. A
    # refused value is the operator's to correct, so nothing is rewritten
    # (spec 142).
    if names_unsafe_scheme(contact["linkedin_url"]):
        raise ValueError("the candidate's LinkedIn link must be an http or https URL")
    html_text = template.replace(
        '<link rel="stylesheet" href="./cover-letter-template.css" />',
        f"<style>\n{css}\n</style>",
    )
    replacements = {
        "name": html.escape(contact["name"]),
        # Scrubbed and normalized like the resume header. The raw tracker
        # title was going straight into the letter, so an internal label or a
        # "Tailored for" prefix reached the recruiter (spec 034).
        "headline": html.escape(strip_banned_phrases(normalize_visible_role_title(company, role))),
        "location": html.escape(contact["location"]),
        "email": html.escape(contact["email"]),
        "linkedin_url": html.escape(contact["linkedin_url"]),
        "linkedin_label": html.escape(contact["linkedin_label"]),
        "full_version_html": markdown_to_html_paragraphs(full_version),
        "company": html.escape(company.strip()),
        **letter_frame(kind, contact["name"]),
    }
    for key, value in replacements.items():
        html_text = html_text.replace(f"{{{{{key}}}}}", value)
    unresolved = sorted(set(re.findall(r"{{([a-zA-Z0-9_]+)}}", html_text)))
    if unresolved:
        raise ValueError(f"unresolved cover letter template placeholders: {', '.join(unresolved)}")
    return html_text


def _default_render(html_text: str, pdf_path: Path) -> None:
    render_pdf(html_text, pdf_path, margin_mm=16)


def write_cover_letter_artifacts(
    conn: Connection,
    company: str,
    role: str,
    job_url: str | None,
    short_version: str,
    full_version: str,
    output_dir: Path | None = None,
    template_dir: Path | None = None,
    render: Callable[[str, Path], None] | None = None,
    validate: Callable[[Path, str], list[str]] | None = None,
    review: Review | None = None,
    *,
    kind: str,
    gaps: tuple[Gap, ...] | None = None,
    facts: tuple[FactUsed, ...] | None = None,
    asks: dict[str, object] | None = None,
    display_company: str = "",
) -> dict[str, Path]:
    with files_in(output_dir, COVER_LETTERS) as prefix:
        keys = cover_letter_keys_for(company, role, prefix)
        store = files()
        markdown_path = artifact_path(keys["markdown"])

        # The review sections go in the markdown draft only. The HTML and PDF
        # are built from full_version alone, so no flag reaches a recruiter
        # (B10).
        review_section = render_review(
            [short_version, full_version], review or Review(), markdown_path
        )
        markdown = render_cover_letter_markdown(
            company, role, job_url, short_version, full_version, review_section, gaps, facts
        )
        save_text(keys["markdown"], markdown, atomic=False)
        if asks is not None:
            # Beside the letter, in the never-in-git store (spec 160).
            save_text(
                cover_letter_asks_key_for(company, role, prefix),
                json.dumps(asks, ensure_ascii=False, indent=2) + "\n",
                atomic=False,
            )
        # The keys are per company and role, and the artifact endpoint serves
        # whatever PDF is there, so from here an earlier run's PDF and HTML
        # would sit beside this new draft as if they were current (review of
        # #84). On any failure the draft stays, for the operator to fix
        # (spec 067).
        store.delete(keys["html"])
        store.delete(keys["pdf"])
        # A recruiter-facing artifact never carries a placeholder (spec 065,
        # rule C10).
        placeholders = find_placeholders(f"{short_version}\n{full_version}")
        if placeholders:
            raise NeedsInputError(markdown_path, placeholders)
        try:
            html_text = render_cover_letter_html(
                conn, display_company or company, role, full_version, template_dir, kind=kind
            )
            save_text(keys["html"], html_text, atomic=False)
            # The resume path validated its PDF; this one only checked the
            # file existed, so a zero-byte or four-page letter passed. It is
            # the one recruiter-facing artifact that had neither gate
            # (spec 034).
            pdf_errors = render_validated_pdf(
                html_text,
                keys["pdf"],
                render if render is not None else _default_render,
                validate
                if validate is not None
                # One page on the industry kind, one or two on the academic
                # (spec 101).
                else partial(validate_rendered_pdf, allowed_pages=rules_for(kind).pages),
            )
            if pdf_errors:
                raise RuntimeError("invalid cover letter PDF: " + "; ".join(pdf_errors))
        except BaseException:
            store.delete(keys["html"])
            raise
        return {
            "markdown": markdown_path,
            "html": artifact_path(keys["html"]),
            "pdf": artifact_path(keys["pdf"]),
        }
