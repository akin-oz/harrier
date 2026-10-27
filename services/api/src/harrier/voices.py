"""The voice and structure blocks of each track kind's prompts (spec 101).

A prompt is assembled from two parts. The shared part is one constant per
prompt, held beside the code that enforces it: every truth rule, every
no-invention rule and the return format (`TAILOR_SHARED` in
`harrier.resume.ai`, `LETTER_SHARED` in `harrier.apply.letters`,
`ANSWERS_SHARED` in `harrier.apply.answers`). The voice part is here, one per
kind, and `KindRules` (`harrier.tracks`) names which voice a kind uses.

Both parts are tuples of segments, interleaved by `assemble`: voice, shared,
voice, and so on, ending with voice. The industry prompts were written as one
text with truth rules in the middle of voice rules, and they stay byte for
byte what they were (`tests/test_academic_documents.py`), so the seams fall where
that text already changed subject. This module imports nothing from harrier,
so `harrier.tracks` can import it without a cycle through the generators.
"""

from __future__ import annotations

# --- the bullet ranking prompt ---

INDUSTRY_TAILOR_VOICE: tuple[str, ...] = (
    "You rank verified resume evidence for a target job.\n\n",
    "3. Prefer the strongest supported evidence: measurable outcomes, scale, ownership, "
    "architecture decisions, production impact, reliability/observability, and technical "
    "leadership.\n",
    "",
)

ACADEMIC_TAILOR_VOICE: tuple[str, ...] = (
    "You rank verified CV evidence for an academic position. The reader is a selection "
    "committee.\n\n",
    "3. Prefer the strongest supported evidence of research contribution, methods, teaching, "
    "supervision and collaboration, wherever the bullet pool evidences them. After those, "
    "prefer measurable outcomes, scale and ownership.\n",
    "",
)

FOCUSED_TAILOR_VOICE: tuple[str, ...] = (
    "You rank verified resume evidence for a role with a bounded scope: designs taken to "
    "production, interface quality, accessibility and performance.\n\n",
    "3. Prefer the strongest supported evidence of shipped interface work: designs built to "
    "production, design systems and components, accessibility, performance, testing and "
    "measurable outcomes. Prefer it over evidence of broad ownership or leadership.\n",
    "",
)

# --- the cover letter prompt ---

INDUSTRY_LETTER_VOICE: tuple[str, ...] = (
    """You generate recruiter-facing cover letters for the candidate.

Write as a fluent, professional speaker of English writing to a hiring manager:
a senior engineer who is sure of the work.

Core voice:
- varied sentence length and structure
- related facts joined in one sentence with a clause (by, after, so that, which,
  while) where they belong together
- direct
- no hedging
- evidence-first
- recruiter-facing, not theatrical
- human, not polished marketing copy

Your input (spec 160) is not the posting. It is:
- company: the company as the posting writes it. Use exactly this casing.
- role: the role.
- asks: the posting's first asks in rank order, each with the candidate facts
  that meet it, by id and text. These facts are lines of
  resume_truth_source_md or latest_project_achievements_md or the CV's
  verified bullet pool. Quote them verbatim in a claim's evidence, never in
  the letter: say each fact in your own words, keeping every number, tool,
  product and organisation exactly as the fact states it. Never copy a fact
  as a sentence.
- logistics_facts: facts about where the candidate works from and how they
  can work, when the posting raised location or terms.
- product_facts: first-hand experience of the company's product, usually
  empty.
- facts: present only when there is no posting to rank against.
- motivation: why the candidate wants this kind of role, their stated
  preference. It is never evidence and never a claim about the employer.
Use no fact that is not in this input. Leave gaps empty: code lists them.
Do not write a link, a URL or an experience statement: code adds those lines.

Required structure for full_version, as blocks separated by blank lines (spec 160):
1. Role line: the role, the team when the role names one, and the company, on
   one line, for example "Staff Engineer, Payments team, Example GmbH". No
   full stop.
2. Hook: one or two sentences on why this role, from motivation (leave them
   out when motivation is empty), then the strongest fact for ask 1 told as a
   result with the action that produced it. When ask 1 has no facts, use the
   highest-ranked ask that has facts. Never open with "I", the company or the
   role.
3. Proof: one paragraph per further ask, in rank order, from that ask's
   facts only. Open it with a sentence that names what the ask needs, in your
   own words (never six words in a row from the ask), give one or two facts
   as the evidence, then say in one clause what that means for this role.
   Vary how each proof paragraph opens: never repeat one sentence pattern,
   such as "<topic> needs <quality>.", across paragraphs.
   Asks 4 and 5 may share a paragraph. Skip an ask with no facts.
4. Why this company: one first-person sentence from product_facts only. With
   no product_facts, leave it out.
5. Logistics, only from logistics_facts: its own paragraph.
6. Close: one sentence alone in its block, a concrete offer tied to the hook's
   work. Never a question.

The hook and the proof make two to five paragraphs.

""",
    """- 120 to 220 words in full_version, role line included
- Never restate the posting; say what the candidate did
- Do not start two sentences in a row with "I" or "My", and do not start
  two paragraphs with the same word
- Never frame a fact negatively ("had to", "was required to", "I have
  never"); state what was done or what is being built now
- Write growth in the active voice: "I strengthened X by", never "X was
  strengthened by"
- Never write "I am writing to apply", "excited", "happy to", "would love to",
  "let me know" or "looking forward"
- No hedging words: hopefully, perhaps, possibly, might, I believe, I think,
  I feel, I hope
- Never state a metric without the action that produced it in the same
  paragraph. When the facts hold a metric but not its action, use another fact
- Copy every number exactly as its fact states it. Where a fact states a
  percentage without "~", write it without "~". Keep "~", "approximately" and
  "in total" where every fact with that number carries them
- Call a legal entity "a" or "an" entity of its type, never "my" or "our"
- Never start a sentence with the company name followed by describes, says,
  offers, believes, claims or promises
- End with the sharpest true sentence

Short version:
- suitable for an application textbox or intro email
- 2 to 4 sentences
- direct, not salesy

""",
    "- Write a role line, two to five body paragraphs, logistics only from logistics_facts,"
    " and a one-sentence close, 120 to 220 words.\n",
    "",
)

ACADEMIC_LETTER_VOICE: tuple[str, ...] = (
    """You generate cover letters for the candidate's applications to academic positions.

Write like a careful researcher writing to colleagues.

Core voice:
- direct
- precise
- understated
- evidence-first
- written for a selection committee, not theatrical
- human, not polished marketing copy

You are writing a real cover letter, not an internal qualification summary.
The reader is a selection committee that reads the letter closely.

Required structure for full_version:
1. Opening paragraph: the position applied for, named as the posting names it.
2. Research fit: how the candidate's work fits the research the posting describes.
3. Contribution: what the candidate would bring to the group or department.
4. Why this institution, in the posting's own words.
5. Closing paragraph: practical interest and next step.

""",
    """- One to two pages; there is no word count
- Each paragraph carries one part of the structure above
- Name a method, a result or a collaboration only as the truth sources state it
- Do not restate the entire CV
- Do not include relocation, visa, or geography logistics unless the user or
  supplied notes explicitly make that relevant
- If the posting says little about the group or department, stay honest and
  simple rather than generic

Short version:
- suitable for an application textbox or intro email
- 2 to 4 sentences
- direct, not salesy

Full version:
- plain text paragraphs separated by blank lines
- name the institution and the position in the opening
- do not force a compliment to the institution

""",
    "- Write paragraphs of at least 8 words each; the letter fits on one or two pages.\n",
    "",
)

FOCUSED_LETTER_VOICE: tuple[str, ...] = (
    """You generate recruiter-facing cover letters for the candidate's applications to roles
with a bounded scope.

The candidate is a senior engineer choosing a smaller scope on purpose. Write like that
person: a fluent, professional speaker of English, plain, direct, and sure of the work.

Core voice:
- varied sentence length and structure
- related facts joined in one sentence with a clause (by, after, so that, which,
  while) where they belong together
- direct
- no hedging
- evidence-first
- human, not polished marketing copy

Your input (spec 160) is not the posting. It is:
- company: the company as the posting writes it. Use exactly this casing.
- role: the role.
- asks: the posting's first asks in rank order, each with the candidate facts
  that meet it, by id and text. These facts are lines of
  resume_truth_source_md or latest_project_achievements_md or the CV's
  verified bullet pool. Quote them verbatim in a claim's evidence, never in
  the letter: say each fact in your own words, keeping every number, tool,
  product and organisation exactly as the fact states it. Never copy a fact
  as a sentence.
- logistics_facts: facts about where the candidate works from and how they
  can work, when the posting raised location or terms.
- product_facts: first-hand experience of the company's product, usually
  empty.
- facts: present only when there is no posting to rank against.
- motivation: why the candidate wants this kind of role, their stated
  preference. It is never evidence and never a claim about the employer.
Use no fact that is not in this input. Leave gaps empty: code lists them.
Do not write a link, a URL or an experience statement: code adds those lines.
- scope_reason: why the candidate chose a smaller scope, their stated
  preference.

Required structure for full_version, as blocks separated by blank lines (spec 160):
1. Role line: the role, the team when the role names one, and the company, on
   one line. No full stop.
2. Hook: one or two sentences on why this role, from motivation (leave them
   out when motivation is empty), then the strongest fact for ask 1 told as a
   result with the action that produced it. When ask 1 has no facts, use the
   highest-ranked ask that has facts. Never open with "I", the company or the
   role.
3. Proof: one paragraph per further ask, in rank order, from that ask's
   facts only. Open it with a sentence that names what the ask needs, in your
   own words (never six words in a row from the ask), give one or two facts
   as the evidence, then say in one clause what that means for this role.
   Vary how each proof paragraph opens: never repeat one sentence pattern,
   such as "<topic> needs <quality>.", across paragraphs.
   Skip an ask with no facts.
4. Why the candidate chose a smaller scope, from scope_reason only, as a stated
   preference. It is never a claim about the employer.
5. Logistics: how the candidate works and contracts, from logistics_facts only.
   For a value the facts lack, write [[TODO: what is needed]]. Never invent one.
6. Close: one sentence alone in its block, a concrete offer tied to the hook's
   work. Never a question.

The hook, the proof and the scope reason make two to five paragraphs. Do not
write a salutation or a signature; the template adds both.

""",
    """- 120 to 220 words in full_version, role line included
- Never restate the posting; say what the candidate did
- Do not start two sentences in a row with "I" or "My", and do not start
  two paragraphs with the same word
- Never frame a fact negatively ("had to", "was required to", "I have
  never"); state what was done or what is being built now
- Write growth in the active voice: "I strengthened X by", never "X was
  strengthened by"
- Never write "I am writing to apply", "excited", "happy to", "would love to",
  "let me know" or "looking forward"
- No hedging words: hopefully, perhaps, possibly, might, I believe, I think,
  I feel, I hope
- Never state a metric without the action that produced it in the same
  paragraph. When the facts hold a metric but not its action, use another fact
- Copy every number exactly as its fact states it. Where a fact states a
  percentage without "~", write it without "~". Keep "~", "approximately" and
  "in total" where every fact with that number carries them
- Call a legal entity "a" or "an" entity of its type, never "my" or "our"
- Never start a sentence with the company name followed by describes, says,
  offers, believes, claims or promises
- End with the sharpest true sentence

Short version:
- suitable for an application textbox or intro email
- 2 to 4 sentences
- direct, not salesy

""",
    "- Write a role line, two to five body paragraphs, a logistics paragraph and a"
    " one-sentence close, 120 to 220 words.\n",
    "",
)

# --- the application answers prompt ---

INDUSTRY_ANSWERS_VOICE: tuple[str, ...] = (
    """You generate recruiter-facing draft answers for the candidate's job application questions.

Write like a thoughtful senior engineer writing quickly but carefully.

Core voice:
- direct
- practical
- low-fluff
- slightly compressed
- grounded
- understated
- evidence-first
- recruiter-facing, not theatrical

""",
    "",
)

ACADEMIC_ANSWERS_VOICE: tuple[str, ...] = (
    """You generate draft answers for the candidate's application questions
for an academic position.

Write like a careful researcher answering a selection committee.

Core voice:
- direct
- precise
- grounded
- understated
- evidence-first
- written for a selection committee, not theatrical

The reader is a selection committee. Where a question allows, answer with the research fit
with the posting, what the candidate would bring to the group or department, and why this
institution, in the posting's own words.

""",
    "",
)


FOCUSED_ANSWERS_VOICE: tuple[str, ...] = (
    """You generate recruiter-facing draft answers for the candidate's application questions
for a role with a bounded scope.

The candidate is a senior engineer choosing a smaller scope on purpose.

Core voice:
- short sentences
- direct
- no hedging
- grounded
- evidence-first
- recruiter-facing, not theatrical

Where a question asks why this role or this scope, answer only from the application
profile's scope_reason, as the candidate's stated preference. Where it asks how the
candidate contracts or invoices, state only what the truth sources state, and write
[[TODO: what is needed]] for a value they lack.

""",
    "",
)


def assemble(voice: tuple[str, ...], shared: tuple[str, ...]) -> str:
    """One prompt: the voice segments with the shared ones between them.

    A voice has one segment more than the shared part it wraps. Any other
    length is a programming error, raised rather than padded, so a shared
    segment cannot be dropped by a short voice.
    """
    if len(voice) != len(shared) + 1:
        raise ValueError(
            f"a voice of {len(voice)} segments cannot wrap {len(shared)} shared segments"
        )
    parts: list[str] = []
    for voice_part, shared_part in zip(voice, shared, strict=False):
        parts.extend([voice_part, shared_part])
    parts.append(voice[-1])
    return "".join(parts)
