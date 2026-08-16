"""The container keeps the promises spec 051 made for it.

Three properties, and none of them is visible by reading the running system:
the image must carry no personal path, the published port must stay on
loopback, and a container built from an old tree must be distinguishable from
a current one.

The `.dockerignore` check derives its expectations from
`config/data-classification.json` rather than listing them, so a path
classified never-in-git tomorrow fails this test until the image is taught to
exclude it. A hand-listed copy would agree with the classification exactly
until someone changed one of them.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatch
from typing import cast

import pytest

from harrier.demo import repo_root
from harrier_api.app import BUILD_UNKNOWN, build_revision, build_timestamp

ROOT = repo_root()
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
CLASSIFICATION = ROOT / "config" / "data-classification.json"


def never_in_git_patterns() -> list[str]:
    raw: object = json.loads(CLASSIFICATION.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    section = cast("dict[str, object]", raw)["never_in_git"]
    assert isinstance(section, dict)
    patterns = cast("dict[str, object]", section)["patterns"]
    assert isinstance(patterns, list), "the classification carries no pattern list"
    # An empty set would make every case below vacuous, which is the
    # "reports success while doing nothing" shape this repository keeps
    # finding (spec 045).
    assert patterns, "the classification carries no patterns"
    return [str(pattern) for pattern in cast("list[object]", patterns)]


def dockerignore_lines() -> set[str]:
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def excludes(pattern: str, lines: set[str]) -> bool:
    """Whether `.dockerignore` excludes a classified path.

    Two syntaxes have to agree here. `data/**` in the classification and
    `data/` in the ignore file are the same instruction, so the comparison is
    on the path prefix rather than the literal string. And an ignore line may
    be a glob covering several classified paths: `.env.*` covers `.env.local`,
    which a string comparison reads as uncovered. The first version of this
    check did exactly that and failed on a path that was already excluded,
    which is the right direction for a check to fail in but still wrong.
    """
    stem = pattern.removesuffix("/**").removesuffix("/*").rstrip("/")
    for candidate in lines:
        if candidate.startswith("!"):
            # A negation re-includes a path; it never excludes one.
            continue
        bare = candidate.rstrip("/")
        if bare in {stem, f"{stem}/**", f"{stem}/*"} or fnmatch(stem, bare):
            return True
    return False


@pytest.mark.parametrize("pattern", never_in_git_patterns())
def test_every_never_in_git_path_is_kept_out_of_the_image(pattern: str) -> None:
    """A layer outlives the deletion of the directory it copied.

    `docker image save` carries every layer, so a personal path that reaches
    one has left the machine in a way removing the file does not undo. This is
    the same class ADR-008 removed from git, applied to the other artifact this
    repository now produces.
    """
    assert excludes(pattern, dockerignore_lines()), (
        f"{pattern} is never-in-git but .dockerignore does not exclude it"
    )


# A published mapping, host side first. Matched from the compose file rather
# than from a running container, because the property has to hold on a machine
# where nothing is running.
_PUBLISH = re.compile(r'^\s*-\s*"([^"]+:)?(\d+):(\d+)"\s*$')


def test_the_published_port_never_leaves_loopback() -> None:
    """Spec 035 established that this API is not an open door.

    A published container port is the easiest way to undo that without
    noticing: `"8000:8000"` binds every interface, and the difference from
    `"127.0.0.1:8000:8000"` is ten characters in a file nobody rereads.
    """
    offenders: list[str] = []
    for number, line in enumerate(COMPOSE.read_text(encoding="utf-8").splitlines(), 1):
        match = _PUBLISH.match(line)
        if not match:
            continue
        host = (match.group(1) or "").rstrip(":")
        if host not in {"127.0.0.1", "[::1]"}:
            offenders.append(f"{number}: {line.strip()}")
    assert offenders == [], "a port mapping is not bound to loopback: " + "; ".join(offenders)


def test_the_container_comes_back_when_docker_starts() -> None:
    assert "restart: unless-stopped" in COMPOSE.read_text(encoding="utf-8")


def test_the_image_is_told_what_revision_it_is() -> None:
    """Without this the container reproduces the failure it exists to remove.

    An image is stale by construction until rebuilt, so a container answering
    correctly while running old code is easier to arrive at here than with a
    host process, not harder.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG HARRIER_REVISION" in text
    assert "ARG HARRIER_BUILT_AT" in text


def test_an_unstamped_process_says_unknown_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`just dev` is not built from an image and has no revision to report.

    Reporting "unknown" is the honest answer. Reporting a revision read from
    the working tree would be a claim about the running process that the
    running process cannot make, which is the confusion this field exists to
    end.
    """
    monkeypatch.delenv("HARRIER_REVISION", raising=False)
    monkeypatch.delenv("HARRIER_BUILT_AT", raising=False)
    assert build_revision() == BUILD_UNKNOWN
    assert build_timestamp() == BUILD_UNKNOWN


def test_a_stamped_process_reports_the_revision_it_was_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARRIER_REVISION", "abc1234-dirty")
    monkeypatch.setenv("HARRIER_BUILT_AT", "2026-08-16T12:00:00Z")
    assert build_revision() == "abc1234-dirty"
    assert build_timestamp() == "2026-08-16T12:00:00Z"


def test_blank_stamps_are_treated_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A build arg left unset arrives as an empty string, not as absent.

    Without the strip-and-fall-back the health path would report a revision of
    "", which reads as a value rather than as the absence of one.
    """
    monkeypatch.setenv("HARRIER_REVISION", "   ")
    monkeypatch.setenv("HARRIER_BUILT_AT", "")
    assert build_revision() == BUILD_UNKNOWN
    assert build_timestamp() == BUILD_UNKNOWN
