"""What to check before sending, and one next action (spec 066, B9).

Written at the end of the markdown drafts only. The letter HTML and PDF are
built from the letter text alone, so neither section nor any flag can reach
a recruiter (B10).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from harrier.apply.claims import Claim, find_placeholders, number_tokens
from harrier.apply.requirements import Flag


@dataclass(frozen=True)
class Review:
    claims: tuple[Claim, ...] = ()
    flags: tuple[Flag, ...] = ()


def next_action(texts: Sequence[str], review: Review, path: Path) -> str:
    """Exactly one line, in priority order: a placeholder only the operator
    can fill, then a requirement only they can answer, then a read-through."""
    placeholders = find_placeholders("\n".join(texts))
    if placeholders:
        return f'Replace "{placeholders[0]}" in {path}.'
    if review.flags:
        flag = review.flags[0]
        return f'Decide your answer to the {flag.kind} requirement: "{flag.sentence}".'
    return f"Read {path} once against the posting."


def render_review(texts: Sequence[str], review: Review, path: Path) -> str:
    joined = "\n".join(texts)
    items = [f"- Placeholder: {placeholder}" for placeholder in find_placeholders(joined)]
    items.extend(f'- Flag ({flag.kind}): "{flag.sentence}"' for flag in review.flags)
    items.extend(
        f'- Claim: "{claim.sentence}" rests on: '
        + "; ".join(f'"{fragment}"' for fragment in claim.evidence)
        for claim in review.claims
    )
    numbers = dict.fromkeys(token.raw for token in number_tokens(joined))
    items.extend(f"- Number: {number}" for number in numbers)
    if not items:
        items = ["- Nothing flagged. Read it once anyway."]
    lines = ["## To verify", "", *items, "", "## Next action", "", next_action(texts, review, path)]
    return "\n".join(lines) + "\n"
