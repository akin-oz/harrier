"""The agent teams stay wired to real agents (spec 028).

aie 0.2.0 compiles `.ai/agents/` into `.claude/agents/` but has no notion of
agent teams, so `.claude/agent-teams/` is a copy that nothing else keeps
honest. These tests are that something else: without them a renamed agent,
an orphaned one, or an edited copy is discovered when a board is convened
and half of it fails to spawn.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harrier.demo import repo_root

ROOT = repo_root()
SOURCE_TEAMS = ROOT / ".ai" / "agent-teams"
COMPILED_TEAMS = ROOT / ".claude" / "agent-teams"
AGENTS = ROOT / ".ai" / "agents"
COMPILED_AGENTS = ROOT / ".claude" / "agents"

MEMBER_RE = re.compile(r"`((?:review|readiness)-[a-z-]+)`")
TEAM_PREFIX = {"principal-review": "review-", "open-source-readiness": "readiness-"}

# Membership comes from the roster table only. A launch document also names
# the *other* team's agents when handing a lens off, and reading those as
# members made this suite fail on its first run, which is the whole point of
# scoping it.
ROSTER = {
    "principal-review": ("## Members", "## Scope discipline"),
    "open-source-readiness": ("## Why these five lenses", "All five are"),
}


def team_names() -> list[str]:
    return sorted(path.name for path in SOURCE_TEAMS.iterdir() if path.is_dir())


def members_of(team: str) -> set[str]:
    launch = (SOURCE_TEAMS / team / "launch.md").read_text(encoding="utf-8")
    start, end = ROSTER[team]
    roster = launch[launch.index(start) : launch.index(end)]
    return set(MEMBER_RE.findall(roster))


def agent_files() -> set[str]:
    return {path.stem for path in AGENTS.glob("*.md")}


def test_every_team_has_a_launch_and_a_tasks_document() -> None:
    assert team_names() == ["open-source-readiness", "principal-review"]
    for team in team_names():
        assert (SOURCE_TEAMS / team / "launch.md").is_file()
        assert (SOURCE_TEAMS / team / "tasks.md").is_file()


def test_every_member_named_in_a_launch_document_exists() -> None:
    """A renamed agent otherwise surfaces as a board that half spawns."""
    known = agent_files()
    for team in team_names():
        missing = sorted(members_of(team) - known)
        assert not missing, f"{team} names agents that do not exist: {missing}"


def test_every_team_agent_is_claimed_by_exactly_one_team() -> None:
    claimed: dict[str, list[str]] = {}
    for team in team_names():
        for member in members_of(team):
            claimed.setdefault(member, []).append(team)
    orphans = sorted(
        name
        for name in agent_files()
        if name.startswith(("review-", "readiness-")) and name not in claimed
    )
    assert not orphans, f"agents belonging to no team: {orphans}"
    shared = {name: teams for name, teams in claimed.items() if len(teams) > 1}
    assert not shared, f"agents claimed by more than one team: {shared}"


def test_members_use_the_prefix_of_their_team() -> None:
    for team, prefix in TEAM_PREFIX.items():
        wrong = sorted(name for name in members_of(team) if not name.startswith(prefix))
        assert not wrong, f"{team} members must start with {prefix}: {wrong}"


@pytest.mark.parametrize("team", ["principal-review", "open-source-readiness"])
def test_each_team_fields_five_members(team: str) -> None:
    assert len(members_of(team)) == 5


def frontmatter_of(directory: Path, member: str) -> str:
    return (directory / f"{member}.md").read_text(encoding="utf-8").split("---")[1]


@pytest.mark.parametrize("directory", [AGENTS, COMPILED_AGENTS], ids=["source", "compiled"])
def test_no_team_member_can_write(directory: Path) -> None:
    """A board that can write is not a review board.

    Checked against the compiled copy as well as the source, because
    `.claude/agents/` is what Claude Code loads: an earlier version of this
    test read only `.ai/`, so a compiled file that gained `Write` would have
    passed it (review finding on PR #29).
    """
    for team in team_names():
        for member in sorted(members_of(team)):
            frontmatter = frontmatter_of(directory, member)
            assert "Write" not in frontmatter, f"{member} in {directory.name} can write"
            assert "Edit" not in frontmatter, f"{member} in {directory.name} can edit"


@pytest.mark.parametrize("directory", [AGENTS, COMPILED_AGENTS], ids=["source", "compiled"])
def test_review_board_members_cannot_execute_anything(directory: Path) -> None:
    """`Bash` is not read-only, so the board that claims to be does not get it.

    The first version of spec 028 gave all ten members `Bash` and called them
    all read-only. A shell writes, installs and reaches the network, so the
    claim was false for every one of them. The five investigators on
    `open-source-readiness` genuinely need it (they clone, build and run the
    suite) and their launch document now says they are not read-only. The
    five reviewers here execute nothing, so the grant is simply removed and
    the property becomes enforceable.
    """
    for member in sorted(members_of("principal-review")):
        assert "Bash" not in frontmatter_of(directory, member), (
            f"{member} holds Bash, so principal-review's read-only claim is false"
        )


def test_every_team_member_compiles_into_the_claude_directory() -> None:
    """`.ai/agents/` is the source; the board spawns from `.claude/agents/`."""
    for team in team_names():
        for member in sorted(members_of(team)):
            assert (COMPILED_AGENTS / f"{member}.md").is_file(), (
                f"{member} has no compiled agent; run `aie sync`"
            )


def test_the_investigators_are_told_they_are_not_read_only() -> None:
    """The honest half of the tool split, in the file the agent actually reads.

    Without this the correction lives only in a launch document, and an agent
    that holds `Bash` reads nothing telling it to leave the checkout alone.
    """
    for member in sorted(members_of("open-source-readiness")):
        body = (AGENTS / f"{member}.md").read_text(encoding="utf-8")
        assert "## Execution limits" in body, f"{member} holds Bash but states no limits"


def test_the_compiled_copy_matches_the_source() -> None:
    """aie does not compile agent teams, so nothing but this keeps the copy
    Claude Code actually reads in step with the source of truth."""
    for team in team_names():
        for name in ("launch.md", "tasks.md"):
            source = (SOURCE_TEAMS / team / name).read_text(encoding="utf-8")
            compiled_path = COMPILED_TEAMS / team / name
            assert compiled_path.is_file(), f"{compiled_path} is missing; copy .ai/agent-teams"
            assert compiled_path.read_text(encoding="utf-8") == source, (
                f"{compiled_path} has drifted from .ai/agent-teams/{team}/{name}"
            )


def test_every_readiness_lens_cites_the_failure_that_motivated_it() -> None:
    """The teams' organising rule: an investigator exists because this
    repository has already failed that way."""
    launch = (SOURCE_TEAMS / "open-source-readiness" / "launch.md").read_text(encoding="utf-8")
    table = launch[launch.index("| Investigator") : launch.index("All five are")]
    for member in sorted(members_of("open-source-readiness")):
        row = next((line for line in table.splitlines() if f"`{member}`" in line), "")
        assert row, f"{member} has no row in the why-these-lenses table"
        # The last cell is the reason; a placeholder would be much shorter.
        assert len(row.split("|")[-2].strip()) > 60, f"{member} cites no real failure"


def test_a_handoff_reference_is_not_read_as_membership() -> None:
    """principal-review hands document-versus-code findings to the other
    team by name. That mention must not make it a member."""
    launch = (SOURCE_TEAMS / "principal-review" / "launch.md").read_text(encoding="utf-8")
    assert "readiness-claim-auditor" in launch
    assert "readiness-claim-auditor" not in members_of("principal-review")
