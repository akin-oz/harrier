"""What a job's generated artifacts are, and where they are (spec 047).

The Apply page shows what an operation produced and serves it back. Both
questions resolve here, through the same `*_keys_for` helpers the writers
use, so the reader cannot drift from the writer. Whether an artifact exists
is asked of the storage seam (spec 107).

The kind is a closed set. A caller names an artifact, never a path, which is
why there is no traversal to defend against: an unknown kind is refused
before anything touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass

from harrier.apply.answers import answers_key_for
from harrier.apply.letters import cover_letter_keys_for
from harrier.connection import Connection
from harrier.filestore import files, key_name
from harrier.offers.evaluate import report_key_for
from harrier.resume.content import ResumeBundleError, load_bundle
from harrier.resume.tailor import resume_keys_for
from harrier.tracker import get_job
from harrier.tracks import Scope


class UnknownArtifactKind(KeyError):
    """The caller named an artifact this system does not produce."""


@dataclass(frozen=True)
class Artifact:
    kind: str
    # None when no name can be built: a resume without a bundle.
    key: str | None
    exists: bool
    produced_by: str
    media_type: str

    @property
    def filename(self) -> str:
        """The name a person sees: the key's last segment."""
        return key_name(self.key) if self.key is not None else ""


# kind -> the operation that produces it. The Apply page turns a missing
# artifact into "run this", so the operation is part of the record rather
# than something the page hardcodes.
PRODUCED_BY: dict[str, str] = {
    "resume-pdf": "resume",
    "resume-markdown": "resume",
    "resume-evaluation": "resume",
    "cover-letter-pdf": "cover-letter",
    "cover-letter-markdown": "cover-letter",
    "answers": "answers",
    "evaluation": "evaluate",
}

ARTIFACT_KINDS: tuple[str, ...] = tuple(PRODUCED_BY)

_PDF = "application/pdf"
_MARKDOWN = "text/markdown; charset=utf-8"

_MEDIA_TYPES: dict[str, str] = {
    "resume-pdf": _PDF,
    "resume-markdown": _MARKDOWN,
    "resume-evaluation": _MARKDOWN,
    "cover-letter-pdf": _PDF,
    "cover-letter-markdown": _MARKDOWN,
    "answers": _MARKDOWN,
    "evaluation": _MARKDOWN,
}


def _resume_keys(conn: Connection, scope: Scope, company: str, role: str) -> dict[str, str] | None:
    """None when the resume bundle is not configured.

    The resume filename carries the candidate's name, so without a bundle
    there is no name and therefore no key to report. That is a real state on
    a fresh install, and reporting the artifact as simply absent would be a
    guess: nothing can be produced until the bundle exists.
    """
    try:
        bundle = load_bundle(conn, scope)
    except ResumeBundleError:
        return None
    return resume_keys_for(bundle.name, company, role)


def _keys_by_kind(conn: Connection, scope: Scope, job_id: int) -> dict[str, str]:
    row = get_job(conn, scope, job_id)
    company = row.get("company", "")
    role = row.get("title", "")

    keys: dict[str, str] = {}
    resume = _resume_keys(conn, scope, company, role)
    if resume is not None:
        keys["resume-pdf"] = resume["pdf"]
        keys["resume-markdown"] = resume["markdown"]
        keys["resume-evaluation"] = resume["evaluation"]

    letter = cover_letter_keys_for(company, role)
    keys["cover-letter-pdf"] = letter["pdf"]
    keys["cover-letter-markdown"] = letter["markdown"]
    keys["answers"] = answers_key_for(company, role)
    keys["evaluation"] = report_key_for(company, role)
    return keys


def _artifact(kind: str, key: str | None) -> Artifact:
    return Artifact(
        kind=kind,
        key=key,
        exists=key is not None and files().stat(key) is not None,
        produced_by=PRODUCED_BY[kind],
        media_type=_MEDIA_TYPES[kind],
    )


def artifacts_for_job(conn: Connection, scope: Scope, job_id: int) -> list[Artifact]:
    """Every artifact kind for this job, present or not.

    Absent kinds are listed rather than omitted: the page's job is to say
    what exists and what would produce the rest, and an omitted row cannot
    say the second thing.
    """
    keys = _keys_by_kind(conn, scope, job_id)
    return [_artifact(kind, keys.get(kind)) for kind in ARTIFACT_KINDS]


def artifact_for_job(conn: Connection, scope: Scope, job_id: int, kind: str) -> Artifact:
    """One artifact by kind.

    Raises `UnknownArtifactKind` for anything outside the closed set, which is
    what a path-shaped kind is. The check happens before any key is built.
    """
    if kind not in PRODUCED_BY:
        raise UnknownArtifactKind(kind)
    return _artifact(kind, _keys_by_kind(conn, scope, job_id).get(kind))
