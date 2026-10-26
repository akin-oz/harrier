"""The cover letter linter (spec 158, rules L1 to L11; spec 160, L12 to L17;
spec 165, L18 to L22).

Deterministic: no model call and no network. It reads the letter, the
evidence the letter may cite, the posting, and the record's professional
years, and returns one message per violation. The industry and focused
kinds run it through `cover_letter_violations`; the academic kind does not.

The rules name no fact about the candidate. Every value they compare
against comes from the context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import pairwise

from harrier.apply.claims import number_tokens
from harrier.resume.content import year_counts
from harrier.resume.dashes import dash_marks, describe

# L2. "may" is not here: it is also a month.
HEDGES: tuple[str, ...] = (
    "hopefully",
    "perhaps",
    "possibly",
    "somewhat",
    "fairly",
    "relatively",
    "arguably",
    "might",
    "I believe",
    "I think",
    "I feel",
    "happy to",
    "would love to",
    "I hope",
)

# L4: the hook never begins with one of these.
BANNED_OPENERS: tuple[str, ...] = (
    "I am writing",
    "I'm writing",
    "I am applying",
    "I'm applying",
    "I am excited",
    "I'm excited",
    "The role",
    "This role",
    "Your team",
)

# L5: refused anywhere in the letter.
BANNED_ANYWHERE: tuple[str, ...] = (
    "I am writing to apply",
    "excited",
    "let me know",
    "looking forward",
)

# L7: a shared run of this many words is a restatement (spec 160: was 8).
RESTATEMENT_WORDS = 6

# L3: the word range (spec 160: 120 to 220).
MIN_WORDS = 120
MAX_WORDS = 220

# L13: words that qualify a number.
QUALIFIERS: tuple[str, ...] = ("~", "approximately", "about", "around", "roughly", "in total")

# L15: what a sentence that opens on the company may not go on to say.
COMPANY_SAYS = (
    "describes",
    "described",
    "says",
    "said",
    "offers",
    "offered",
    "believes",
    "believed",
    "claims",
    "claimed",
    "promises",
    "promised",
)

# L10: legal entity types a letter never calls "my" or "our" (spec 159).
# Spelled as letter groups: a dot and whitespace may sit between two groups,
# so "B.V." and "sp. z o. o." are the same types. A dot after the last group
# is not part of the match, since it may end the sentence.
ENTITY_TYPES: tuple[tuple[str, ...], ...] = (
    ("O", "Ü"),
    ("G", "m", "b", "H"),
    ("Ltd",),
    ("L", "L", "C"),
    ("B", "V"),
    ("S", "A", "S"),
    ("S", "R", "L"),
    ("sp", "z", "o", "o"),
)

# L11: the role line's length.
ROLE_LINE_MAX_WORDS = 15

# L18 (spec 165): negative framing, refused anywhere.
NEGATIVE_FRAMING: tuple[str, ...] = (
    "had to",
    "was required to",
    "were required to",
    "although I have never",
    "although I've never",
    "I have never done",
    "I've never done",
    "I don't have",
    "I do not have",
    "I lack",
    "despite not having",
)

# L19: a passive account of the candidate's own growth.
_PASSIVE_GROWTH = re.compile(
    r"(?<!\w)(?:was|were|has been|have been)\s+"
    r"(?:developed|strengthened|improved|honed|gained|built)\s+(?:by|through)(?!\w)",
    re.IGNORECASE,
)

# L20: the words a sentence in the first person starts with.
I_OPENERS: tuple[str, ...] = ("i", "i'm", "i've", "i'd", "i'll", "my")

# L21: a shared run of this many words with one fact is a copied fact.
COPIED_FACT_WORDS = 10

# L10: an owner word, at most two words, then an entity type, across any
# whitespace. A word is a letter or digit followed by letters, digits,
# apostrophes or hyphens, so a sentence end stops the match (spec 159).
_ENTITY_TYPE = "|".join(
    r"\.?\s*".join(re.escape(group) for group in groups) for groups in ENTITY_TYPES
)
_OWNED_ENTITY = re.compile(
    rf"\b(?:my|our)\s+(?:[^\W_][\w'\u2019-]*\s+){{0,2}}(?:{_ENTITY_TYPE})(?!\w)",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_APOSTROPHE = "\u2019"
_WORD = re.compile(rf"[^\W_]+(?:['{_APOSTROPHE}][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class AskCoverage:
    """One of the posting's top asks and the facts that meet it (spec 160)."""

    rank: int
    ask: str
    fact_texts: tuple[str, ...]
    listed_as_gap: bool


@dataclass(frozen=True)
class LintContext:
    """What the linter reads besides the letter."""

    # Every document the letter may cite, as one text (spec 158).
    evidence: str
    posting: str
    company: str
    role: str
    # The record's completed professional years; None when the record
    # cannot be read, and then L9 checks only for "decade".
    professional_years: int | None = None
    # Spec 160: the company as the posting writes it (L14, L15), the top
    # asks (L16), the evidence the letter's claims cite (L16) and the link
    # line code added (L17). Empty values switch those rules off.
    company_form: str = ""
    asks: tuple[AskCoverage, ...] = ()
    cited: tuple[str, ...] = ()
    link_line: str | None = None
    # Spec 165: each fact the writer received under `asks`, as (id, text),
    # for L21; and the lines code added (the link and experience lines),
    # which are not body sentences. Empty switches L21 off.
    fact_texts: tuple[tuple[str, str], ...] = ()
    added_lines: tuple[str, ...] = ()


def _phrase(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _sentences(block: str) -> list[str]:
    return [part for part in _SENTENCE_END.split(block.strip()) if part.strip()]


def _words(text: str) -> list[str]:
    return [word.lower().replace(_APOSTROPHE, "'") for word in _WORD.findall(text)]


def _windows(text: str, size: int = RESTATEMENT_WORDS) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[at : at + size]) for at in range(len(words) - size + 1)}


def _shared_runs(text: str, posting: str, evidence: str) -> list[str]:
    """Each run of `RESTATEMENT_WORDS` words in `text` that the posting also
    has, case and punctuation aside. A run the evidence also has is the
    candidate's own fact in the posting's words, not a restatement."""
    windows = _windows(posting) - _windows(evidence)
    words = _words(text)
    runs: list[str] = []
    at = 0
    while at <= len(words) - RESTATEMENT_WORDS:
        window = tuple(words[at : at + RESTATEMENT_WORDS])
        if window in windows:
            runs.append(" ".join(window))
            at += RESTATEMENT_WORDS
        else:
            at += 1
    return runs


def _starts_with(text: str, prefix: str) -> bool:
    return bool(prefix.strip()) and text.lower().startswith(prefix.strip().lower())


def lint_letter(full_version: str, context: LintContext, short_version: str = "") -> list[str]:
    """Every rule of L1 to L22 the letter breaks. Empty when it passes.

    L11's block count is the kind's and the brief's, so
    `cover_letter_violations` holds it; this checks the role line."""
    violations: list[str] = []
    blocks = _blocks(full_version)

    # L1: dashes, in both versions.
    for name, text in (("full_version", full_version), ("short_version", short_version)):
        marks = dash_marks(text)
        if marks:
            violations.append(f"dash as punctuation in {name}: {describe(marks)}")

    # L2, L5, L9 and L10 read both versions: the short one goes into an
    # application textbox (review of #405).
    both = f"{full_version}\n{short_version}"

    # L2: hedging.
    hedges = [hedge for hedge in HEDGES if _phrase(hedge).search(both)]
    if hedges:
        violations.append(f"hedging: {', '.join(hedges)}")

    # L3: length.
    words = len(full_version.split())
    if words > MAX_WORDS:
        violations.append(f"over the word limit: {words} words, at most {MAX_WORDS}")
    if words < MIN_WORDS:
        violations.append(f"under the word floor: {words} words, at least {MIN_WORDS}")

    # L11 (role line), then L4 on the block after it.
    role_line = blocks[0] if blocks else ""
    if (
        not role_line
        or "\n" in role_line
        or len(role_line.split()) > ROLE_LINE_MAX_WORDS
        or role_line.rstrip()[-1:] in (".", "!", "?")
    ):
        violations.append(
            "missing role line: the first block names the role and company in one line"
        )
    hook = blocks[1] if len(blocks) > 1 else ""
    openers = [*BANNED_OPENERS, context.company, context.role]
    opener = next((prefix for prefix in openers if _starts_with(hook, prefix)), None)
    if opener is not None:
        violations.append(f"banned opener: the hook begins with {opener!r}")
    # Spec 160: the hook answers the posting's first ask, which may have no
    # metric, so its first sentence no longer needs a number.
    known = {token.value for token in number_tokens(context.evidence)}

    # L5: banned anywhere.
    banned = [phrase for phrase in BANNED_ANYWHERE if _phrase(phrase).search(both)]
    if banned:
        violations.append(f"banned phrase: {', '.join(banned)}")

    # L6: the close.
    close = blocks[-1] if blocks else ""
    if len(_sentences(close)) > 1:
        violations.append("the close is more than one sentence")
    if "?" in close:
        violations.append("the close asks a question")

    # L7: restatement, role line aside.
    body = "\n\n".join(blocks[1:])
    for run in _shared_runs(body, context.posting, context.evidence):
        violations.append(f"restates the posting: {run}")

    # L8: numbers, with the company's and role's own numbers exempt as in C5.
    exempt = {token.value for token in number_tokens(f"{context.company} {context.role}")}
    unbacked = [
        token.raw
        for token in number_tokens(full_version)
        if token.value not in known and token.value not in exempt
    ]
    if unbacked:
        violations.append(f"number not in the evidence: {', '.join(dict.fromkeys(unbacked))}")

    # L9: experience.
    years = context.professional_years
    if years is not None:
        larger = [count for count in year_counts(both) if count > years]
        if larger:
            violations.append(
                f"states {max(larger)} years of experience; the record holds"
                f" {context.professional_years}"
            )
    if re.search(r"\bdecades?\b", both, re.IGNORECASE):
        violations.append("counts experience in decades")

    # L10: never "my" or "our" entity, whatever the evidence says. Each
    # version alone, so a match cannot span the two (spec 159).
    owned = [
        " ".join(match.group(0).split())
        for text in (full_version, short_version)
        for match in _OWNED_ENTITY.finditer(text)
    ]
    for found in dict.fromkeys(owned):
        violations.append(
            f'calls a legal entity \'{found}\': write "a" or "an" before the entity type'
        )

    violations.extend(_prose_rules(both, blocks, context))
    violations.extend(_asks_rules(full_version, blocks, context))
    return violations


def _body_blocks(blocks: list[str], context: LintContext) -> list[str]:
    """Every block after the role line except the close and the lines code
    added (spec 165)."""
    added = {line.strip() for line in (*context.added_lines, context.link_line or "") if line}
    return [block for block in blocks[1:-1] if block.strip() not in added]


def _prose_rules(both: str, blocks: list[str], context: LintContext) -> list[str]:
    """L18 to L22 (spec 165): how the letter reads."""
    violations: list[str] = []
    plain = both.replace(_APOSTROPHE, "'")

    # L18: negative framing.
    negative = [phrase for phrase in NEGATIVE_FRAMING if _phrase(phrase).search(plain)]
    if negative:
        violations.append(f"negative framing: {', '.join(negative)}")

    # L19: passive growth.
    passive = [" ".join(match.group(0).split()) for match in _PASSIVE_GROWTH.finditer(plain)]
    if passive:
        violations.append(f"passive growth: {', '.join(dict.fromkeys(passive))}")

    body = _body_blocks(blocks, context)
    sentences = [sentence for block in body for sentence in _sentences(block)]

    # L20: sentences that start with "I" or "My".
    starts = [bool(words) and words[0] in I_OPENERS for words in map(_words, sentences)]
    in_a_row = any(first and second for first, second in pairwise(starts))
    count = sum(starts)
    if in_a_row or count * 3 > len(sentences):
        violations.append(
            f'"I" or "My" starts {count} of {len(sentences)} body sentences'
            + (", two in a row" if in_a_row else "")
        )

    # L21: a body sentence that copies a fact the writer was given.
    copied: list[str] = []
    for fact_id, text in context.fact_texts:
        fact_windows = _windows(text, COPIED_FACT_WORDS)
        if fact_windows and any(
            _windows(sentence, COPIED_FACT_WORDS) & fact_windows for sentence in sentences
        ):
            copied.append(fact_id)
    for fact_id in dict.fromkeys(copied):
        violations.append(f"copies fact {fact_id} word for word: say it in other words")

    # L22: two body paragraphs that open with the same word.
    openers = [_WORD.findall(block)[0] for block in body if _WORD.search(block)]
    folded = [word.lower().replace(_APOSTROPHE, "'") for word in openers]
    seen: set[str] = set()
    for word, key in zip(openers, folded, strict=True):
        if folded.count(key) > 1 and key not in seen:
            seen.add(key)
            violations.append(f'two body paragraphs open with "{word}"')
    return violations


_PERCENT = re.compile(r"(~\s*)?(\d[\d,.]*)\s*%")


def _plain_percentages(evidence: str) -> set[str]:
    """Each percentage value an evidence line states without a "~"."""
    return {
        match.group(2)
        for match in _PERCENT.finditer(evidence)
        if not match.group(1) and evidence[max(match.start() - 1, 0) : match.start()] != "~"
    }


_NUMBER_AT = re.compile(r"(~\s*)?(\d[\d,]*(?:\.\d+)?)\s*([kKmMxX%])?(?!\w)")
_BEFORE_WORDS = 3
_AFTER_WORDS = 5


def _qualified_values(text: str) -> dict[str, bool]:
    """Each number value in `text`, and whether any of its occurrences
    carries a qualifier next to it: "~" attached, "approximately", "about",
    "around" or "roughly" in the three words before, or "in total" in the
    five words after. A qualifier elsewhere on the line qualifies nothing."""
    found: dict[str, bool] = {}
    lowered = text.lower()
    words_before = [q for q in QUALIFIERS if q not in ("~", "in total")]
    for match in _NUMBER_AT.finditer(text):
        suffix = (match.group(3) or "").lower()
        value = match.group(2).replace(",", "") + (suffix if suffix in ("k", "m", "x") else "")
        before = " ".join(lowered[: match.start()].split()[-_BEFORE_WORDS:])
        after = " ".join(lowered[match.end() :].split()[:_AFTER_WORDS])
        qualified = (
            bool(match.group(1))
            or any(re.search(rf"(?<!\w){re.escape(q)}(?!\w)", before) for q in words_before)
            or re.search(r"(?<!\w)in total(?!\w)", after) is not None
        )
        found[value] = found.get(value, False) or qualified
    return found


def _norm(text: str) -> str:
    return " ".join(text.replace("\u2019", "'").lower().split()).strip(" .")


def _asks_rules(full_version: str, blocks: list[str], context: LintContext) -> list[str]:
    """L12 to L17 (spec 160)."""
    violations: list[str] = []

    # L12: no "~" on a percentage the evidence also states exactly.
    exact = _plain_percentages(context.evidence)
    for match in _PERCENT.finditer(full_version):
        if match.group(1) and match.group(2) in exact:
            violations.append(f"a tilde on an exact percentage: ~{match.group(2)}%")

    # L13: a number keeps the qualifier every evidence line gives it, read
    # next to the number itself.
    lines = [_qualified_values(line) for line in context.evidence.splitlines() if line.strip()]
    for block in blocks:
        for sentence in _sentences(block):
            for value, qualified in _qualified_values(sentence).items():
                holding = [line[value] for line in lines if value in line]
                if holding and all(holding) and not qualified:
                    violations.append(f"a number lost its qualifier: {value} (in: {sentence})")

    form = context.company_form.strip()
    if form:
        # L14: the company in the posting's casing.
        for match in re.finditer(rf"(?<!\w){re.escape(form)}(?!\w)", full_version, re.IGNORECASE):
            if match.group(0) != form:
                violations.append(
                    f"company name casing: {match.group(0)!r}, the posting writes {form!r}"
                )
        # L15: never "<Company> describes".
        says = "|".join(COMPANY_SAYS)
        opener = re.compile(rf"^{re.escape(form)}(?:['\u2019]s)?\s+(?:{says})\b", re.IGNORECASE)
        for block in blocks:
            for sentence in _sentences(block):
                if opener.search(sentence.strip()):
                    violations.append(f"a sentence reports what the company says: {sentence}")

    # L16: each top ask with facts is answered by one of them.
    cited = [_norm(fragment) for fragment in context.cited if fragment.strip()]
    for ask in context.asks:
        if not ask.fact_texts:
            if not ask.listed_as_gap:
                violations.append(f"ask {ask.rank} has no facts and is not a gap: {ask.ask}")
            continue
        facts = [_norm(text) for text in ask.fact_texts]
        if not any(c and (c in f or f in c) for c in cited for f in facts):
            violations.append(f"ask {ask.rank} not addressed by its facts: {ask.ask}")

    # L17: the link line code added is still there.
    if context.link_line and context.link_line not in blocks:
        violations.append(f"the link line is missing: {context.link_line}")
    return violations
