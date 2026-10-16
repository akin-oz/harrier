"""A job's stored description, read, written and required (spec 149).

The store is the URL-keyed description cache that discovery fills
(`harrier.screening.descriptions`). Nothing here adds a store: the operator
can see what discovery captured, and paste the text once when it captured
none, and every application operation reads that one entry.
"""

from __future__ import annotations

from harrier.screening.descriptions import load_cached_description, save_description_cache

# A choice, not a measured need: far above any posting seen, and small
# enough that a mistaken paste of a whole file is refused (spec 149).
MAX_DESCRIPTION_BYTES = 65_536


class DescriptionError(ValueError):
    """A description the store refuses, in words that never quote it."""


class JobHasNoUrlError(DescriptionError):
    """The job has no URL, and a description is stored by URL."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"job {job_id} has no URL; a description is stored by URL")


class NoDescriptionError(ValueError):
    """An operation that needs a description found none, given or stored.

    Its message is the CLI's; the browser route words its own refusal.
    """

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        super().__init__(
            f"no job description is stored for job {job_id}; "
            f"store one with harrier description set {job_id} --file PATH"
        )


def stored_description(url: str) -> str:
    """The job's stored description, or "" when none is stored or the entry
    is damaged (the cache already reads damage as missing)."""
    return load_cached_description(url.strip()).strip() if url.strip() else ""


def store_description(job_id: int, url: str, text: str) -> str:
    """Store `text` as the job's description, replacing any entry, and
    return what was stored. Refuses before anything is written."""
    if not url.strip():
        raise JobHasNoUrlError(job_id)
    stripped = text.strip()
    if not stripped:
        raise DescriptionError("the description is empty")
    if "\x00" in stripped:
        raise DescriptionError("the description holds a NUL character")
    try:
        encoded = stripped.encode("utf-8")
    except UnicodeEncodeError as error:
        raise DescriptionError("the description is not text UTF-8 can encode") from error
    if len(encoded) > MAX_DESCRIPTION_BYTES:
        raise DescriptionError(
            f"the description is longer than {MAX_DESCRIPTION_BYTES} bytes in UTF-8"
        )
    save_description_cache(url.strip(), stripped)
    return stripped


def require_description(job_id: int, url: str, given: str | None = None) -> str:
    """The text to use: what the caller was given, else the stored one.

    Raises NoDescriptionError when there is neither. Tailor, cover letter
    and answers call this before generating, from the CLI and the browser
    alike (spec 149).
    """
    if given and given.strip():
        return given
    stored = stored_description(url)
    if stored:
        return stored
    raise NoDescriptionError(job_id)
