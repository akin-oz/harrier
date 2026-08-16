"""What a job's generated artifacts are, and where they are (spec 047).

The Apply page shows what an operation produced and serves it back. Both
questions resolve here, through the same `*_paths_for` helpers the writers
use, so the reader cannot drift from the writer.

The kind is a closed set. A caller names an artifact, never a path, which is
why there is no traversal to defend against: an unknown kind is refused
before anything touches the filesystem.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from harrier.apply.answers import answers_path_for
from harrier.apply.letters import cover_letter_paths_for
from harrier.offers.evaluate import report_path_for
from harrier.resume.content import ResumeBundleError, load_bundle
from harrier.resume.tailor import resume_paths_for
from harrier.tracker import get_job


class UnknownArtifactKind(KeyError):
    """The caller named an artifact this system does not produce."""


@dataclass(frozen=True)
class Artifact:
    kind: str
    path: Path
    exists: bool
    produced_by: str
    media_type: str


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


def _resume_paths(conn: sqlite3.Connection, company: str, role: str) -> dict[str, Path] | None:
    """None when the resume bundle is not configured.

    The resume filename carries the candidate's name, so without a bundle
    there is no name and therefore no path to report. That is a real state on
    a fresh install, and reporting the artifact as simply absent would be a
    guess: nothing can be produced until the bundle exists.
    """
    try:
        bundle = load_bundle(conn)
    except ResumeBundleError:
        return None
    return resume_paths_for(bundle.name, company, role)


def _paths_by_kind(conn: sqlite3.Connection, job_id: int) -> dict[str, Path]:
    row = get_job(conn, job_id)
    company = row.get("company", "")
    role = row.get("title", "")

    paths: dict[str, Path] = {}
    resume = _resume_paths(conn, company, role)
    if resume is not None:
        paths["resume-pdf"] = resume["pdf"]
        paths["resume-markdown"] = resume["markdown"]
        paths["resume-evaluation"] = resume["evaluation"]

    letter = cover_letter_paths_for(company, role)
    paths["cover-letter-pdf"] = letter["pdf"]
    paths["cover-letter-markdown"] = letter["markdown"]
    paths["answers"] = answers_path_for(company, role)
    paths["evaluation"] = report_path_for(company, role)
    return paths


def artifacts_for_job(conn: sqlite3.Connection, job_id: int) -> list[Artifact]:
    """Every artifact kind for this job, present or not.

    Absent kinds are listed rather than omitted: the page's job is to say
    what exists and what would produce the rest, and an omitted row cannot
    say the second thing.
    """
    paths = _paths_by_kind(conn, job_id)
    found: list[Artifact] = []
    for kind in ARTIFACT_KINDS:
        path = paths.get(kind)
        found.append(
            Artifact(
                kind=kind,
                path=path if path is not None else Path(),
                exists=path is not None and path.is_file(),
                produced_by=PRODUCED_BY[kind],
                media_type=_MEDIA_TYPES[kind],
            )
        )
    return found


def artifact_for_job(conn: sqlite3.Connection, job_id: int, kind: str) -> Artifact:
    """One artifact by kind.

    Raises `UnknownArtifactKind` for anything outside the closed set, which is
    what a path-shaped kind is. The check happens before any path is built.
    """
    if kind not in PRODUCED_BY:
        raise UnknownArtifactKind(kind)
    path = _paths_by_kind(conn, job_id).get(kind)
    return Artifact(
        kind=kind,
        path=path if path is not None else Path(),
        exists=path is not None and path.is_file(),
        produced_by=PRODUCED_BY[kind],
        media_type=_MEDIA_TYPES[kind],
    )
