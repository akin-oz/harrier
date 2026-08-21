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

import harrier.llm.providers as llm_providers
from harrier.demo import repo_root
from harrier.llm import LLMConfig
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


def dockerfile_instructions() -> str:
    """The Dockerfile with its comments removed.

    Every assertion below is a substring match, and this file is heavily
    commented, so a comment mentioning a command would satisfy a check that the
    command exists. Deleting a `RUN` while leaving the paragraph explaining it
    is exactly how a guard comes to pass for the wrong reason, which is the
    shape this repository keeps finding (review of PR #58).
    """
    return "\n".join(
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


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
    assert f"--group {group}" in dockerfile_instructions(), (
        f"dependency group {group!r} is a feature the UI can reach, but the image never installs it"
    )


def test_the_dev_group_stays_out_of_the_runtime_image() -> None:
    assert "--no-dev" in dockerfile_instructions()


def test_the_browser_path_is_pinned_rather_than_left_to_home() -> None:
    """The container runs as a uid with no passwd entry, so HOME resolves to
    `/` and Playwright looked under `/.cache/ms-playwright`, which is not where
    the download went. Pinning the path makes the lookup uid-independent.

    Asserted against the instructions rather than the file, so deleting the
    command while leaving the paragraph that explains it fails (review of
    PR #58).
    """
    text = dockerfile_instructions()
    assert "ENV PLAYWRIGHT_BROWSERS_PATH=" in text
    assert "playwright install --with-deps chromium" in text
    # The page-count gate shells out to pdfinfo; without poppler every render
    # validates as "could not inspect PDF page count", so the install command
    # itself is what is asserted.
    assert "apt-get install" in text and "poppler-utils" in text


# --- the provider seam the container has to be able to run -------------------
#
# Round four of the missing-runtime-dependency defect, and the first one that
# was neither a dependency group nor a repository directory: `.env` carries
# AI_PROVIDER=claude-cli into the container through `env_file`, and the image
# had no `claude` binary. All six call sites of `harrier.llm.generate_text`
# failed identically.

_CLI_PATH = re.compile(r"^ENV\s+CLAUDE_CLI_PATH=(\S+)\s*$", re.MULTILINE)
_CLI_VERSION = re.compile(r"^ARG\s+CLAUDE_CLI_VERSION=(\S+)\s*$", re.MULTILINE)
# The installer writes under $HOME, so the HOME this command sets is what
# decides where the binary actually lands.
_CLI_INSTALL = re.compile(r"HOME=(\S+)\s+bash\s+\S*install[-\w]*\.sh")
# A download piped straight into a shell, which is the failure below.
_PIPED_INTO_SHELL = re.compile(r"curl[^\n]*\|\s*(?:ba)?sh\b")


def test_the_image_installs_the_cli_the_provider_seam_resolves() -> None:
    """Asserted against the instructions rather than the file, so deleting the
    command while leaving the paragraph explaining it fails (review of PR #58)."""
    text = dockerfile_instructions()
    assert _CLI_INSTALL.search(text), "the image never installs the `claude` CLI"
    assert _CLI_PATH.search(text), "the image installs the CLI without telling the runtime where"


def test_a_failed_download_cannot_produce_a_green_image() -> None:
    """The build must not be able to report success without the CLI.

    `curl ... | bash` yields the shell's exit status, not curl's, so a failed
    download feeds an empty script to a shell that exits 0. Reproduced on the
    first version of this block (review of PR #59): the layer reported DONE,
    the image built, and `/opt/claude/.local/bin/claude` was not in it. The
    provider then failed at runtime in front of the operator, which is the
    failure the whole change exists to remove.

    So the download goes to a file and the layer verifies what it produced.
    This is asserted here rather than by building an image because the Python
    CI job has no container runtime; the executable check lives in the build
    itself, where it runs on every build including CI's, and this pins that it
    is still there.
    """
    text = dockerfile_instructions()
    assert not _PIPED_INTO_SHELL.search(text), (
        "a download is piped into a shell, which hides its failure from the layer"
    )
    assert 'test -x "${CLAUDE_CLI_PATH}"' in text, (
        "the layer never checks that the binary it advertises exists"
    )
    assert '--version | cut -d\' \' -f1)" = "${CLAUDE_CLI_VERSION}"' in text, (
        "the layer never checks that the installed CLI is the pinned version"
    )


def test_the_cli_lands_where_the_image_says_it_did() -> None:
    """The Playwright bug, one binary over.

    `PLAYWRIGHT_BROWSERS_PATH` pointed somewhere the download had not gone, and
    nothing failed until a run needed the browser. `CLAUDE_CLI_PATH` is the
    override `find_binary` reads before `PATH`, so the same drift would leave
    the image carrying 279 MB of CLI that the provider cannot find.
    """
    text = dockerfile_instructions()
    advertised = _CLI_PATH.search(text)
    installed = _CLI_INSTALL.search(text)
    assert advertised and installed
    prefix = installed.group(1).rstrip("/")
    assert advertised.group(1).startswith(f"{prefix}/"), (
        f"the image advertises {advertised.group(1)} but installs under {prefix}"
    )


def test_the_cli_version_is_pinned_rather_than_floating() -> None:
    """`stable` would mean the image's behavior changes without the tree
    changing, which is this spec's staleness failure inverted: not a container
    running old code, but two containers built from one commit running
    different code."""
    text = dockerfile_instructions()
    version = _CLI_VERSION.search(text)
    assert version, "the CLI version is not declared"
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.group(1)), (
        f"CLAUDE_CLI_VERSION={version.group(1)!r} is not an exact version"
    )
    install_line = next(
        (line for line in text.splitlines() if _CLI_INSTALL.search(line)),
        "",
    )
    assert "${CLAUDE_CLI_VERSION}" in install_line, (
        f"the pinned version is declared but the installer is not told it: {install_line.strip()!r}"
    )


def compose_assigns(name: str) -> bool:
    """Whether the compose file carries an active assignment of `name`.

    Any assignment counts, an empty value included: `environment:` outranks
    `env_file`, so even `NAME: ""` would silently override the operator's
    value from the local environment file. A mention inside a comment does
    not count; the guard is about assignments, not vocabulary (review of
    PR #61: the first version required a non-empty value, so an empty
    assignment would have passed as absent).
    """
    pattern = rf"^(?!\s*#)\s+{re.escape(name)}:"
    return re.search(pattern, COMPOSE.read_text(encoding="utf-8"), re.MULTILINE) is not None


def test_the_compose_file_does_not_force_api_key_billing() -> None:
    """Spec 051 set `CLAUDE_CLI_USE_API_KEY: "1"` here on the premise that no
    subscription credential could cross into a Linux container. The premise
    was incomplete: `claude setup-token` mints CLAUDE_CODE_OAUTH_TOKEN, a
    plain environment value that crosses through env_file and bills the
    subscription. Forcing the switch made every container AI run bill the
    API key even when the cheap path was configured (spec 054). Billing the
    key is now an explicit opt-in from .env, never a compose default, and no
    compose assignment may shadow the operator's value either way.
    """
    assert not compose_assigns("CLAUDE_CLI_USE_API_KEY"), (
        "the compose file assigns CLAUDE_CLI_USE_API_KEY, forcing or shadowing the billing choice"
    )


def test_the_compose_file_wires_the_env_file_the_credential_rides_in() -> None:
    """The subscription token reaches the container only through the
    `env_file: .env` line. The provider test below injects the token
    directly, so it cannot notice this line disappearing; this can (review
    of PR #61)."""
    text = COMPOSE.read_text(encoding="utf-8")
    env_file_block = re.search(
        r"^\s+env_file:\s*\n(?:\s*-.*\n)*?\s*-\s*(?:path:\s*)?\.env\s*$",
        text,
        re.MULTILINE,
    )
    assert env_file_block, "the compose file no longer loads .env; the CLI credential cannot arrive"


def test_the_container_supplies_the_credential_the_cli_authenticates_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container credential path, end to end at the provider seam.

    Under spec 051 the credential was the API key, forced by a compose
    switch. Under spec 054 it is the subscription token: with no opt-in
    switch, CLAUDE_CODE_OAUTH_TOKEN survives into the CLI's child
    environment and ANTHROPIC_API_KEY is stripped, so the run bills the
    subscription. Same property, new credential, so the test keeps the name
    spec 051 cites.
    """
    captured: dict[str, str] = {}

    class FakeProc:
        returncode = 0
        stdout = '{"is_error": false, "result": "ok"}'
        stderr = ""

    def fake_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
        return "/opt/claude/.local/bin/claude"

    def fake_run(*args: object, **kwargs: object) -> FakeProc:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured.update(cast("dict[str, str]", env))
        return FakeProc()

    monkeypatch.setattr(llm_providers, "find_binary", fake_binary)
    monkeypatch.setattr(llm_providers.subprocess, "run", fake_run)
    monkeypatch.delenv("CLAUDE_CLI_USE_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-probe")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-probe")

    config = LLMConfig(provider="claude-cli", model="sonnet", api_key="claude-cli")
    assert llm_providers.generate_with_config("system", "user", config, 30) == "ok"
    assert captured.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat-probe", (
        "the subscription token does not reach the CLI"
    )
    assert "ANTHROPIC_API_KEY" not in captured


def test_the_opt_in_switch_still_hands_the_cli_the_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch spec 054 keeps: CLAUDE_CLI_USE_API_KEY=1 from .env
    restores per-token billing, explicitly and only then."""
    captured: dict[str, str] = {}

    class FakeProc:
        returncode = 0
        stdout = '{"is_error": false, "result": "ok"}'
        stderr = ""

    def fake_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
        return "/opt/claude/.local/bin/claude"

    def fake_run(*args: object, **kwargs: object) -> FakeProc:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured.update(cast("dict[str, str]", env))
        return FakeProc()

    monkeypatch.setattr(llm_providers, "find_binary", fake_binary)
    monkeypatch.setattr(llm_providers.subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDE_CLI_USE_API_KEY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-probe")

    config = LLMConfig(provider="claude-cli", model="sonnet", api_key="claude-cli")
    assert llm_providers.generate_with_config("system", "user", config, 30) == "ok"
    assert captured.get("ANTHROPIC_API_KEY") == "sk-ant-probe", (
        "the explicit opt-in no longer reaches the CLI as a credential"
    )


def test_without_the_switch_the_key_is_still_withheld(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half, so the test above cannot pass by the provider having
    stopped stripping the key at all. On the host the CLI runs on a
    subscription and an inherited API key would bill silently."""
    captured: dict[str, str] = {}

    class FakeProc:
        returncode = 0
        stdout = '{"is_error": false, "result": "ok"}'
        stderr = ""

    def fake_binary(name: str, path_env: str, fallbacks: tuple[str, ...]) -> str | None:
        return "/opt/claude/.local/bin/claude"

    def fake_run(*args: object, **kwargs: object) -> FakeProc:
        env = kwargs.get("env")
        assert isinstance(env, dict)
        captured.update(cast("dict[str, str]", env))
        return FakeProc()

    monkeypatch.setattr(llm_providers, "find_binary", fake_binary)
    monkeypatch.setattr(llm_providers.subprocess, "run", fake_run)
    monkeypatch.delenv("CLAUDE_CLI_USE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-probe")

    config = LLMConfig(provider="claude-cli", model="sonnet", api_key="claude-cli")
    llm_providers.generate_with_config("system", "user", config, 30)
    assert "ANTHROPIC_API_KEY" not in captured


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
