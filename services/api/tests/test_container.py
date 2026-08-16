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


def dockerignore_rules() -> list[str]:
    """The rules in file order.

    Order is the whole semantics: docker applies every rule and the last one
    that matches decides. A set loses that, and the earlier version of this
    helper returned one (review of PR #56).
    """
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def rule_matches(stem: str, pattern: str) -> bool:
    """Whether one rule covers one classified path.

    Two syntaxes have to agree. `data/**` in the classification and `data/` in
    the ignore file are the same instruction, so the comparison is on the path
    prefix rather than the literal string. And a rule may be a glob covering
    several classified paths: `.env.*` covers `.env.local`, which a string
    comparison reads as uncovered.
    """
    bare = pattern.rstrip("/")
    return bare in {stem, f"{stem}/**", f"{stem}/*"} or fnmatch(stem, bare)


def excludes(pattern: str, rules: list[str]) -> bool:
    """Whether `.dockerignore` excludes a classified path, last match winning.

    A negation re-includes a path, so `data/` followed by `!data/` leaves the
    directory in the build context. Skipping negations entirely, which the
    first version did, reports that path as excluded while the image carries
    it: the check would have said the personal data was out while it was in,
    which is the worst direction for a privacy guard to be wrong in.
    """
    stem = pattern.removesuffix("/**").removesuffix("/*").rstrip("/")
    verdict = False
    for rule in rules:
        negated = rule.startswith("!")
        if rule_matches(stem, rule[1:] if negated else rule):
            verdict = not negated
    return verdict


@pytest.mark.parametrize("pattern", never_in_git_patterns())
def test_every_never_in_git_path_is_kept_out_of_the_image(pattern: str) -> None:
    """A layer outlives the deletion of the directory it copied.

    `docker image save` carries every layer, so a personal path that reaches
    one has left the machine in a way removing the file does not undo. This is
    the same class ADR-008 removed from git, applied to the other artifact this
    repository now produces.
    """
    assert excludes(pattern, dockerignore_rules()), (
        f"{pattern} is never-in-git but .dockerignore does not exclude it"
    )


def test_a_negation_that_reintroduces_a_classified_path_is_caught() -> None:
    """The regression case for the bug this check itself had.

    `data/` then `!data/` leaves the directory in the build context. A checker
    that skips negations calls that excluded, so the guard would report the
    personal data was out of the image while it was in it.
    """
    assert excludes("data/**", ["data/"])
    assert not excludes("data/**", ["data/", "!data/"])
    # Order decides, so re-excluding after the negation restores the verdict.
    assert excludes("data/**", ["data/", "!data/", "data/"])
    # A negation of something else does not re-include this one.
    assert excludes("data/**", ["data/", "!logs/"])


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


# A repository-root directory the runtime resolves, in either of the two forms
# the source uses: `Path("templates")` against the working directory, and
# `repo_root() / "fixtures"` against the tree.
_ASSET_ROOT = re.compile(r'(?:Path\(|repo_root\(\)\s*/\s*)"([a-z_]+)"')

# Personal, and mounted rather than copied (ADR-008). Their absence from the
# image is the requirement, not an oversight, so they are excluded here and
# asserted absent below.
MOUNTED_NOT_COPIED = {"data", "secrets"}

SOURCE_DIRS = (
    ROOT / "services" / "api" / "src" / "harrier",
    ROOT / "services" / "api" / "src" / "harrier_api",
    ROOT / "services" / "api" / "src" / "harrier_cli",
)


def runtime_asset_roots() -> set[str]:
    """Repository directories the runtime opens, read out of the source.

    Derived rather than listed. A new module reading a new directory adds
    itself here, so the image is told about it by a failing test rather than by
    a traceback from a running container.
    """
    found: set[str] = set()
    for directory in SOURCE_DIRS:
        for path in directory.rglob("*.py"):
            for name in _ASSET_ROOT.findall(path.read_text(encoding="utf-8")):
                if (ROOT / name).is_dir():
                    found.add(name)
    assert found, "no asset roots discovered; the scan did not run"
    return found - MOUNTED_NOT_COPIED


def copied_paths() -> set[str]:
    """The first path of every `COPY` in the runtime stage.

    `COPY --from=web ...` is the SPA arriving from the build stage, which is
    how `apps` gets there, so its destination is what counts.
    """
    copied: set[str] = set()
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        parts = [word for word in stripped.split()[1:] if not word.startswith("--")]
        if not parts:
            continue
        destination = parts[-1].strip("./")
        copied.add(destination.split("/")[0])
        copied.add(parts[0].split("/")[0])
    return copied


@pytest.mark.parametrize("asset", sorted(runtime_asset_roots()))
def test_the_image_copies_every_repository_asset_the_runtime_reads(asset: str) -> None:
    """The regression case for a container that had the code and not the files.

    `harrier tailor` failed inside a running container with a missing
    `templates/resume-template.html`. Nothing was wrong with the code; the
    image simply never copied the directory, and no check existed that could
    notice. The same hole covered `fixtures/` and the parity documents.
    """
    assert asset in copied_paths(), f"the runtime reads {asset}/ but the Dockerfile never copies it"


@pytest.mark.parametrize("personal", sorted(MOUNTED_NOT_COPIED))
def test_personal_directories_are_mounted_rather_than_copied(personal: str) -> None:
    """The other half of the same rule, so widening the COPY list cannot
    quietly pull personal data into a layer while making the test above pass."""
    assert personal not in copied_paths(), f"{personal}/ is copied into the image"


PYPROJECT = ROOT / "services" / "api" / "pyproject.toml"

# Tooling, not a feature. The image is a runtime, so this one stays out and its
# absence is asserted rather than assumed.
BUILD_ONLY_GROUPS = {"dev"}


def dependency_groups() -> set[str]:
    """The group names declared in `pyproject.toml`, read from the file.

    Derived so a group added tomorrow is either installed or explicitly
    build-only, instead of becoming a lazy import that fails inside a running
    container.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    section = text.split("[dependency-groups]", 1)
    assert len(section) == 2, "pyproject declares no dependency groups"
    body = re.split(r"^\[", section[1], maxsplit=1, flags=re.MULTILINE)[0]
    groups = set(re.findall(r"^([a-z][a-z0-9_-]*)\s*=\s*\[", body, flags=re.MULTILINE))
    assert groups, "no dependency groups parsed; the scan did not run"
    return groups


@pytest.mark.parametrize("group", sorted(dependency_groups() - BUILD_ONLY_GROUPS))
def test_the_image_installs_every_feature_dependency_group(group: str) -> None:
    """Optional on a laptop is not optional in an image.

    On a laptop an optional group means "install it when you want that
    feature". In an image it means the feature is absent and the operator has
    no way to add it: the run just fails with a message telling them to run an
    install command. `pdf` was missing and `harrier tailor` died on Chromium;
    `gmail` was missing behind it and `POST /mail/watch` would have died the
    same way.
    """
    assert f"--group {group}" in DOCKERFILE.read_text(encoding="utf-8"), (
        f"dependency group {group!r} is a feature the UI can reach, but the image never installs it"
    )


def test_the_dev_group_stays_out_of_the_runtime_image() -> None:
    assert "--no-dev" in DOCKERFILE.read_text(encoding="utf-8")


def test_the_browser_path_is_pinned_rather_than_left_to_home() -> None:
    """The container runs as a uid with no passwd entry, so HOME resolves to
    `/` and Playwright looked under `/.cache/ms-playwright`, which is not where
    the download went. Pinning the path makes the lookup uid-independent."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "PLAYWRIGHT_BROWSERS_PATH=" in text
    assert "playwright install" in text
    # The page-count gate shells out to pdfinfo; without poppler every render
    # validates as "could not inspect PDF page count".
    assert "poppler-utils" in text


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
