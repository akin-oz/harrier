"""Can this repository legally be what it says it is (spec 038).

The repository was public with no LICENSE, which means default copyright
applied and nobody could use, modify, or redistribute any of it. The README
meanwhile said a license would land before it went public, which was false at
the moment it was written.

These tests exist so the three statements of the licence cannot drift apart
again, and so the smaller claim-versus-reality gaps the readiness board found
alongside it stay closed.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from harrier.paths import repo_root

ROOT = repo_root()
LICENSE_PATH = ROOT / "LICENSE"


def test_a_license_file_exists() -> None:
    """Without one, publishing produces a public repository that is not open
    source. This is the finding the readiness lens was written to catch."""
    assert LICENSE_PATH.is_file()


def test_the_license_is_mit() -> None:
    text = LICENSE_PATH.read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    # The grant itself, not just the title: a file headed MIT that omits the
    # permission clause grants nothing.
    assert "Permission is hereby granted, free of charge" in text
    assert "without restriction" in text


def test_the_license_names_a_copyright_holder_and_year() -> None:
    line = next(
        line
        for line in LICENSE_PATH.read_text(encoding="utf-8").splitlines()
        if line.startswith("Copyright")
    )
    assert len(line.split()) >= 4, "copyright line needs a year and a holder"


def test_the_readme_and_the_package_metadata_name_the_same_license() -> None:
    """The gap this closes is the one that produced the defect: a document
    describing a licence state the repository did not have."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## License" in readme
    assert "MIT" in readme.split("## License", 1)[1]

    with (ROOT / "services" / "api" / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    assert pyproject["project"]["license"] == "MIT"


def test_the_readme_no_longer_says_a_license_is_coming() -> None:
    """It said so while the repository was already public."""
    section = (ROOT / "README.md").read_text(encoding="utf-8").split("## License", 1)[1]
    assert "Not yet chosen" not in section


# --- the agents say what they are ------------------------------------------

AGENTS = ROOT / ".ai" / "agents"
COMPILED_AGENTS = ROOT / ".claude" / "agents"

# Every agent that audits rather than implements. Spec 028 covered the ten
# board members and left these four, so the repository stated the correct
# distinction for one set and the incorrect one for the other.
GUARDIANS = (
    "contract-guardian",
    "privacy-reviewer",
    "fsd-reviewer",
    "data-integrity-reviewer",
)


def frontmatter(directory: Path, name: str) -> str:
    return (directory / f"{name}.md").read_text(encoding="utf-8").split("---")[1]


@pytest.mark.parametrize("name", GUARDIANS)
@pytest.mark.parametrize("directory", [AGENTS, COMPILED_AGENTS], ids=["source", "compiled"])
def test_a_guardian_cannot_write(directory: Path, name: str) -> None:
    front = frontmatter(directory, name)
    assert "Write" not in front, f"{name} in {directory.name} can write"
    assert "Edit" not in front, f"{name} in {directory.name} can edit"


@pytest.mark.parametrize("name", GUARDIANS)
def test_a_guardian_holding_bash_states_its_execution_limits(name: str) -> None:
    """`Bash` is not read-only. An agent that holds it and says nothing about
    what it may not do is the claim-versus-reality gap one level up."""
    body = (AGENTS / f"{name}.md").read_text(encoding="utf-8")
    if "Bash" in body.split("---")[1]:
        assert "## Execution limits" in body, f"{name} holds Bash but states no limits"


def test_no_agent_claims_to_be_read_only_while_holding_bash() -> None:
    """The exact defect: one guardian's prose said "You are read-only" in a
    file whose frontmatter granted a shell."""
    for path in sorted(AGENTS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        front, body = text.split("---")[1], text.split("---", 2)[2]
        if "Bash" not in front:
            continue
        assert "You are\nread-only" not in body and "You are read-only" not in body, (
            f"{path.name} claims to be read-only while holding Bash"
        )


def test_the_contract_guardian_does_not_regenerate_what_it_audits() -> None:
    """It was told to run the generation command, which writes the artifact
    the source-of-truth guard protects. An auditor that repairs the evidence
    cannot report on it."""
    body = (AGENTS / "contract-guardian.md").read_text(encoding="utf-8")
    instruction = body.split("Check, in order:", 1)[1]
    assert "Do not run `just contract`" in instruction
    assert "compare" in instruction


# --- one definition of where the repository is ------------------------------


def test_repo_root_has_exactly_one_definition() -> None:
    """There were two, agreeing only because both files sat at the same
    depth. Moving either would have pointed the scheduler at a directory
    without the code, silently, with every job still reporting success."""
    package = ROOT / "services" / "api" / "src" / "harrier"
    definitions = [
        path.relative_to(ROOT)
        for path in package.rglob("*.py")
        if "def repo_root(" in path.read_text(encoding="utf-8")
    ]
    assert definitions == [Path("services/api/src/harrier/paths.py")], definitions


def test_repo_root_resolves_to_a_directory_holding_the_repository() -> None:
    root = repo_root()
    assert (root / "justfile").is_file()
    assert (root / "specs").is_dir()
    assert (root / ".ai").is_dir()


# --- dependency licences ----------------------------------------------------


def test_every_runtime_dependency_permits_redistribution() -> None:
    """Answered rather than assumed. Shipping MIT over a copyleft runtime
    dependency is a licence claim the project cannot honour.

    Honest limitation: this reads the installed distributions' declared
    metadata. A package that declares its licence incorrectly is not caught
    here, and no automated check would catch it.
    """
    from importlib.metadata import distributions

    permissive = ("mit", "bsd", "apache", "isc", "python software foundation", "psf", "unlicense")
    offenders: list[str] = []
    for dist in distributions():
        declared = (dist.metadata.get("License") or "").strip().lower()
        classifiers = " ".join(dist.metadata.get_all("Classifier") or []).lower()
        haystack = f"{declared} {classifiers}"
        if not haystack.strip():
            continue
        if "gpl" in haystack and "lgpl" not in haystack:
            offenders.append(f"{dist.metadata['Name']}: {declared or classifiers[:60]}")
            continue
        if any(token in haystack for token in permissive):
            continue
    assert not offenders, f"copyleft runtime dependencies: {offenders}"


def test_the_license_file_is_tracked_by_git() -> None:
    """A LICENSE that exists only on this machine grants nothing to anyone
    who clones the repository, which is the entire point of adding it."""
    tracked = subprocess.run(
        ["git", "ls-files", "LICENSE"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert tracked.stdout.strip() == "LICENSE"


# --- fixture provenance -----------------------------------------------------

FIXTURES = ROOT / "fixtures"
PROVENANCE = FIXTURES / "PROVENANCE.md"

# A fixture may legitimately name a real provider host: the importers route
# on hostname, so `jobs.ashbyhq.com` is what makes the fixture exercise the
# real path, and the host is a protocol fact rather than provider content.
# What would reveal a recording is the *board slug*, which names an actual
# company and its actual postings.
INVENTED_SLUGS = frozenset({"exampleco", "example-eu-co", "northwind"})

# Structural parts of the providers' API paths, which say nothing about whose
# board it is.
API_SEGMENTS = frozenset(
    {"v0", "v1", "boards", "jobs", "postings", "posting-api", "job-board", "embed"}
)

PROVIDER_PATH = re.compile(
    r"https://(?:[a-z0-9.-]*\.)?(?:greenhouse\.io|ashbyhq\.com|lever\.co)/([^\"\s?]+)"
)


def identifying_segments(text: str) -> set[str]:
    """The parts of a provider URL that name a company rather than an API."""
    found: set[str] = set()
    for path in PROVIDER_PATH.findall(text):
        for segment in path.split("/"):
            if not segment or segment in API_SEGMENTS:
                continue
            # Opaque ids: numeric, or a UUID-shaped posting reference.
            if segment.isdigit() or re.fullmatch(r"[0-9a-f-]{16,}", segment):
                continue
            found.add(segment)
    return found


def test_every_fixture_is_listed_with_its_provenance() -> None:
    listed = PROVENANCE.read_text(encoding="utf-8")
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file() or path == PROVENANCE:
            continue
        relative = path.relative_to(FIXTURES).as_posix()
        assert relative in listed, f"{relative} has no provenance entry"


def test_no_fixture_names_a_real_company() -> None:
    """Authored, not captured. A recorded board response is that provider's
    content and, worse, somebody's real posting (ADR-008).

    The host may be real because routing depends on it. The slug may not:
    an unrecognised one means a fixture was taken from a live board.
    """
    for path in sorted(FIXTURES.rglob("*")):
        if not path.is_file() or path.suffix not in (".json", ".txt", ".md"):
            continue
        unknown = sorted(identifying_segments(path.read_text(encoding="utf-8")) - INVENTED_SLUGS)
        assert not unknown, f"{path.name} names board slugs that are not invented: {unknown}"
