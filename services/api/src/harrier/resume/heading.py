"""The resume role heading: the one definition of its format (spec 063).

A role is one line of the resume markdown: the organization, a separator,
the title, and the employment type in parentheses when there is one. The
writer (`markdown.build_markdown`) builds that line, the HTML renderer
(`htmlrender._parse_experience`) splits it back, and the bundle validator
(`content`, spec 062 Rule 3) has to know what the other two do, because an
organization that splits early hands its tail to the title.

Each used to know the format on its own, the validator by re-enacting the
other two by hand. With the writer and the parser moved to another
separator together, every test stayed green and the defect spec 062 closed
came back. So the format lives here and all three call it.

This module imports nothing from `harrier.resume`, so all three can import
it. The functions read `TITLE_SEPARATOR` when called, not when imported, so
changing it moves every caller at once.
"""

from __future__ import annotations

TITLE_SEPARATOR = " \u2014 "


def role_heading(organization: str, title: str, employment_type: str) -> str:
    """The heading text for one role, without the markdown entry marker."""
    suffix = f" ({employment_type})" if employment_type else ""
    return f"{organization}{TITLE_SEPARATOR}{title}{suffix}"


def split_role_heading(heading: str, position: int) -> tuple[str, str]:
    """The organization and the rest, split on the first separator.

    The rest is the title with its employment type suffix, which is how the
    resume shows it. `position` is the heading's 1-based place among the
    roles and is all the error names: the text is an employer's name, and an
    error message is the kind of text that gets pasted elsewhere.
    """
    organization, separator, rest = heading.partition(TITLE_SEPARATOR)
    if not separator:
        raise ValueError(f"role heading {position} has no title separator")
    return organization, rest
