"""The files that deploy the hosted service (spec 110).

The hosted Dockerfile target, the two Fly configurations, the Vercel
configuration, the deploy workflow, the compose file's empty store, and the
rule that no error reporter is installed. No runner can execute GitHub's
workflow here and no test builds an image (the Python CI job has no
container runtime for it), so the properties that are the files' own order
and content are read from the files, as the spec's Open decision 7 says. The
two properties that can be executed are: the compose file is run through
`docker compose config` where docker is installed, and the Dockerfile's
hosted target was built and inspected by hand for the change that added it.

Every assertion reads instructions, not comments: a comment that mentions a
command satisfies nothing, which is how a guard comes to pass for the wrong
reason (`tests/test_container.py`).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from test_container import runtime_asset_roots

from harrier.demo import repo_root

ROOT = repo_root()
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
VERCEL = ROOT / "apps" / "web" / "vercel.json"
FLY = {
    "staging": ROOT / "deploy" / "fly.staging.toml",
    "production": ROOT / "deploy" / "fly.production.toml",
}


# --- the Dockerfile's stages ---


@dataclass(frozen=True)
class Stage:
    name: str
    parent: str
    instructions: tuple[str, ...]


def stages() -> dict[str, Stage]:
    """Every stage by name, its instructions without comments, with a line's
    backslash continuations joined."""
    joined: list[str] = []
    pending = ""
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        joined.append((pending + line).strip())
        pending = ""
    found: dict[str, Stage] = {}
    current: list[str] = []
    header: re.Match[str] | None = None
    for instruction in joined:
        match = re.match(r"FROM\s+(\S+)(?:\s+AS\s+(\w+))?", instruction, re.IGNORECASE)
        if match:
            if header is not None:
                found[header.group(2)] = Stage(header.group(2), header.group(1), tuple(current))
            header, current = match, []
        else:
            current.append(instruction)
    assert header is not None, "the Dockerfile has no stage"
    found[header.group(2)] = Stage(header.group(2), header.group(1), tuple(current))
    return found


def chain(name: str) -> list[Stage]:
    """The stage and every stage it builds on, base first."""
    every = stages()
    out: list[Stage] = []
    stage = every[name]
    while True:
        out.insert(0, stage)
        if stage.parent not in every:
            return out
        stage = every[stage.parent]


def lines_of(name: str) -> list[str]:
    return [instruction for stage in chain(name) for instruction in stage.instructions]


def syncs(name: str) -> list[set[str]]:
    """The dependency groups of each `uv sync` in the stage's chain, in order."""
    return [
        set(re.findall(r"--group\s+(\w+)", line)) for line in lines_of(name) if "uv sync" in line
    ]


def groups_installed(name: str) -> set[str]:
    """The groups the image ends with: the last `uv sync` decides, since a
    sync removes what its groups do not name."""
    return syncs(name)[-1]


def test_the_stages_are_read_from_the_file() -> None:
    """The reader finds what the tests below depend on, so none of them is
    vacuous: three stages, the hosted one built on the shared base."""
    every = stages()
    assert list(every) == ["web", "base", "hosted", "runtime"]
    assert every["hosted"].parent == "base"
    assert every["runtime"].parent == "base"
    assert every["base"].parent.startswith("python:")


def test_the_hosted_image_installs_the_store_and_auth_groups() -> None:
    groups = groups_installed("hosted")
    assert groups == {"pdf", "postgres", "hosted"}
    hosted_syncs = [line for line in lines_of("hosted") if "uv sync" in line]
    # Every sync leaves the dev group out, as the local image does.
    assert hosted_syncs and all("--no-dev" in line for line in hosted_syncs)
    # And the project itself is installed: a sync without --no-install-project.
    assert any("--no-install-project" not in line for line in hosted_syncs)


def test_every_sync_of_an_image_names_the_same_groups() -> None:
    """The dependency layer and the final install of a stage agree, so the
    cached layer is the one the image keeps. The shared base installs `pdf`
    alone, and each stage adds its own on top."""
    for name in ("hosted", "runtime"):
        own = [
            set(re.findall(r"--group\s+(\w+)", line))
            for line in stages()[name].instructions
            if "uv sync" in line
        ]
        assert len(own) == 2 and own[0] == own[1], (name, own)


def test_the_hosted_image_installs_no_gmail_group() -> None:
    """Spec 109 offers no hosted Gmail."""
    assert "gmail" not in groups_installed("hosted")
    # The local image still does: the reader is not blind to the group.
    assert "gmail" in groups_installed("runtime")


def test_the_hosted_image_carries_no_cli_provider_and_no_spa() -> None:
    hosted = "\n".join(lines_of("hosted"))
    # No `claude` binary, no variable that finds it, no installer (ADR-013
    # decision 8: the hosted deployment offers the API providers only).
    assert not re.search(r"claude", hosted, re.IGNORECASE)
    # No SPA: nothing from the web stage, and no `apps` directory (ADR-013
    # decision 7: Vercel serves it).
    assert "--from=web" not in hosted
    assert "apps/web" not in hosted
    assert "web" not in {stage.name for stage in chain("hosted")}
    # The local image carries both: the reader is not blind to them.
    local = "\n".join(lines_of("runtime"))
    assert re.search(r"claude", local, re.IGNORECASE)
    assert "--from=web" in local


def test_the_local_image_is_still_the_default_target() -> None:
    """`docker build .` builds the last stage, so the local image stays the
    default and the hosted one is only reached by `--target hosted`."""
    assert list(stages())[-1] == "runtime"
    assert "hosted" not in {stage.name for stage in chain("runtime")}


def test_the_hosted_image_copies_what_the_runtime_reads_and_no_personal_path() -> None:
    copied: set[str] = set()
    for line in lines_of("hosted"):
        if not line.startswith("COPY "):
            continue
        words = [word for word in line.split()[1:] if not word.startswith("--")]
        copied.add(words[0].strip("./").split("/")[0])
    # `apps` is the SPA's directory: the hosted API serves none (ADR-013
    # decision 7), and it serves `apps/web/dist` only when it exists.
    assert runtime_asset_roots() - {"apps"} <= copied | {"services", "LICENSE"}
    assert not copied & {"data", "secrets", "apps", ".env"}


def test_the_hosted_image_serves_the_port_fly_forwards_to() -> None:
    """The command listens on the `internal_port` of both Fly files, on every
    interface, as the API."""
    command = next(line for line in lines_of("hosted") if line.startswith("CMD "))
    assert '"harrier_api.app:app"' in command and '"0.0.0.0"' in command
    ports = {fly(environment)["http_service"]["internal_port"] for environment in FLY}
    assert len(ports) == 1
    assert f'"--port", "{ports.pop()}"' in command
    exposed = [line for line in lines_of("hosted") if line.startswith("EXPOSE ")]
    assert exposed == ["EXPOSE 8000"]


def test_the_hosted_image_says_which_revision_it_is() -> None:
    """The deploy compares `GET /health`'s `revision` with the commit it built
    (spec 110, step 6)."""
    hosted = lines_of("hosted")
    assert "ARG HARRIER_REVISION=unknown" in hosted
    assert any(
        line.startswith("ENV ") and "HARRIER_REVISION=${HARRIER_REVISION}" in line
        for line in hosted
    )


# --- the Fly configurations ---


def fly(environment: str) -> dict[str, Any]:
    return tomllib.loads(FLY[environment].read_text(encoding="utf-8"))


SECRET_SHAPES = (
    re.compile(r"postgres(?:ql)?://", re.IGNORECASE),
    re.compile(r"sb_secret_"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN"),
)
SECRET_NAMES = re.compile(
    r"DATABASE_URL|CREDENTIALS_KEY|SECRET|TOKEN|PASSWORD|SERVICE_ROLE|TELEGRAM_|GMAIL_",
    re.IGNORECASE,
)


def secrets_in(text: str) -> list[str]:
    """Every shape of a secret the text holds: a database URL, a secret key,
    a JWT, a private key, or the name of a variable that carries one."""
    found = [pattern.pattern for pattern in SECRET_SHAPES if pattern.search(text)]
    found += sorted(set(SECRET_NAMES.findall(text)))
    return found


def test_the_secret_scan_finds_each_shape_it_looks_for() -> None:
    """The scan can fail: each secret below is found, and the files' own
    comments, which name the secrets that live elsewhere, are not what it
    reads (it reads parsed values and keys)."""
    for bad in (
        'A = "postgresql://u:p@host/db"',
        'A = "sb_secret_abc"',
        'A = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoi"',
        'HARRIER_DATABASE_URL = "x"',
        'HARRIER_CREDENTIALS_KEY = "x"',
        'TELEGRAM_BOT_TOKEN = "x"',
        'GMAIL_ACCOUNT = "x"',
    ):
        assert secrets_in(bad), bad
    assert secrets_in('HARRIER_AUTH = "supabase"') == []


def flat(value: object) -> list[str]:
    """Every key and every string value of a parsed document."""
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in cast("dict[str, object]", value).items():
            out.append(key)
            out += flat(item)
        return out
    if isinstance(value, list):
        return [part for item in cast("list[object]", value) for part in flat(item)]
    return [value] if isinstance(value, str) else []


@pytest.mark.parametrize("environment", sorted(FLY))
def test_the_fly_files_hold_no_secret_and_name_the_region(environment: str) -> None:
    config = fly(environment)
    assert config["primary_region"] == "ams"
    assert config["app"] == f"harrier-api-{environment}"

    env = config["env"]
    assert env["HARRIER_AUTH"] == "supabase"
    assert "HARRIER_SUPABASE_PUBLISHABLE_KEY" in env
    assert set(env) == {
        "HARRIER_AUTH",
        "HARRIER_SUPABASE_URL",
        "HARRIER_SUPABASE_PUBLISHABLE_KEY",
        "HARRIER_ALLOWED_HOSTS",
        "HARRIER_CORS_ORIGINS",
    }

    # The project's values are placeholders, not a project: a fork deploys
    # its own from the same file, and the repository names none.
    assert env["HARRIER_SUPABASE_URL"] == "__HARRIER_SUPABASE_URL__"
    assert env["HARRIER_SUPABASE_PUBLISHABLE_KEY"] == "__HARRIER_SUPABASE_PUBLISHABLE_KEY__"
    assert ".supabase.co" not in FLY[environment].read_text(encoding="utf-8")

    # An HTTP health check on /health, sending a Host the API accepts: the
    # first name of this file's HARRIER_ALLOWED_HOSTS.
    checks = config["http_service"]["checks"]
    assert len(checks) == 1
    assert (checks[0]["method"], checks[0]["path"]) == ("GET", "/health")
    allowed = [host.strip() for host in env["HARRIER_ALLOWED_HOSTS"].split(",")]
    assert checks[0]["headers"]["Host"] == allowed[0]
    assert allowed[0] == f"harrier-api-{environment}.fly.dev"
    assert env["HARRIER_CORS_ORIGINS"].startswith("https://")

    # No secret, in any key or value, and no notification variable.
    assert secrets_in("\n".join(flat(config))) == []


@pytest.mark.parametrize("environment", sorted(FLY))
def test_the_fly_files_run_one_api_machine_that_stops_when_idle(environment: str) -> None:
    config = fly(environment)
    service = config["http_service"]
    assert service["auto_stop_machines"] == "stop"
    assert service["auto_start_machines"] is True
    assert service["min_machines_running"] == 0
    assert service["internal_port"] == 8000

    # A shared-cpu-1x 1GB machine: one `[[vm]]` entry.
    assert [(vm["size"], vm["memory"]) for vm in config["vm"]] == [("shared-cpu-1x", "1gb")]

    # No volume, no mount, no process group beyond the API, and nothing that
    # allocates a dedicated IPv4 address.
    assert "mounts" not in config
    assert "processes" not in config
    text = "\n".join(flat(config)).lower()
    assert "ipv4" not in text and "dedicated" not in text
    assert "services" not in config  # a [[services]] block could carry an address


def test_staging_has_the_same_settings_as_production() -> None:
    """Hosting budget: `harrier-api-staging` has the settings production has,
    but for the names."""
    staging, production = fly("staging"), fly("production")
    for config in (staging, production):
        config.pop("app")
        config["env"].pop("HARRIER_ALLOWED_HOSTS")
        config["env"].pop("HARRIER_CORS_ORIGINS")
        config["http_service"]["checks"][0]["headers"].pop("Host")
    assert staging == production


# --- the Vercel configuration ---


def test_the_spa_is_deployed_by_the_workflow_only() -> None:
    config = json.loads(VERCEL.read_text(encoding="utf-8"))
    assert config["git"]["deploymentEnabled"] is False
    # The SPA's routes are served by its index, and a path under `/api/` is
    # not one: a call that reached this host by mistake gets a 404, not the
    # index page with a 200.
    (rewrite,) = config["rewrites"]
    assert rewrite["destination"] == "/index.html"
    source = re.compile(rewrite["source"])
    for path in ("/", "/inbox", "/tracker/12", "/apiary", "/assets/app.js"):
        assert source.fullmatch(path), path
    for path in ("/api/health", "/api/jobs", "/api/"):
        assert not source.fullmatch(path), path


# --- the workflow ---


@dataclass(frozen=True)
class Step:
    index: int
    text: str  # the step as written, comments included
    code: str  # its lines without comment lines

    @property
    def name(self) -> str:
        match = re.search(r"^\s*(?:- )?name:\s*(.+)$", self.text, re.MULTILINE)
        return match.group(1).strip() if match else self.text.splitlines()[0].strip()

    @property
    def run(self) -> str:
        """The step's `run` script, without comment lines, or ''."""
        lines = self.text.splitlines()
        for number, line in enumerate(lines):
            if re.match(r"^(\s{8}|\s{6}- )run:", line):
                first = line.split("run:", 1)[1]
                body = ["" if first.strip() in ("|", ">") else first, *lines[number + 1 :]]
                return "\n".join(
                    part for part in body if part.strip() and not part.lstrip().startswith("#")
                )
        return ""


def workflow_jobs() -> dict[str, list[Step]]:
    """Each job's steps, by reading the file's own indentation: a job is a
    two space key under `jobs:`, a step a six space `- `."""
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs_at = text.index("\njobs:\n")
    out: dict[str, list[Step]] = {}
    job = ""
    current: list[str] = []
    steps: list[Step] = []

    def close_step() -> None:
        nonlocal current
        if current:
            body = "\n".join(current)
            code = "\n".join(line for line in current if not line.lstrip().startswith("#"))
            steps.append(Step(len(steps), body, code))
            current = []

    for line in text[jobs_at:].splitlines():
        job_header = re.match(r"^  ([\w-]+):\s*$", line)
        if job_header:
            close_step()
            if job:
                out[job] = list(steps)
            job, steps = job_header.group(1), []
            continue
        if re.match(r"^      - ", line):
            close_step()
        if job and (current or re.match(r"^      - ", line)):
            current.append(line)
    close_step()
    if job:
        out[job] = steps
    return out


def job_header(job: str) -> str:
    """The text of a job before its steps."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"\n  {job}:\n")
    end = text.index("\n    steps:\n", start)
    return "\n".join(
        line for line in text[start:end].splitlines() if not line.lstrip().startswith("#")
    )


def position(steps: list[Step], marker: str) -> int:
    """The index of the one step whose code holds `marker`."""
    holding = [step.index for step in steps if marker in step.code]
    assert len(holding) == 1, f"{marker!r} is in steps {holding}"
    return holding[0]


STORE = "harrier store"


def environment_steps(environment: str) -> list[Step]:
    return workflow_jobs()[environment]


def test_the_workflow_reads_its_jobs() -> None:
    jobs = workflow_jobs()
    assert set(jobs) == {"staging", "staging-ran", "production"}
    assert all(len(steps) > 5 for name, steps in jobs.items() if name != "staging-ran")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_deploy_workflow_migrates_before_it_deploys(environment: str) -> None:
    steps = environment_steps(environment)
    order = [
        # 1. the image, built here for staging and promoted for production.
        position(
            steps, "docker build --target hosted" if environment == "staging" else "skopeo inspect"
        ),
        # 2. migrate, as the migrator.
        position(steps, f"{STORE} migrate"),
        # 3. grant: the API's, the run's and the scheduler's login roles, and
        #    the schedule reader to the scheduler's.
        position(steps, f'{STORE} grant-tenant "$API_LOGIN"'),
        position(steps, f'{STORE} grant-tenant "$RUN_LOGIN"'),
        position(steps, f'{STORE} grant-tenant "$SCHEDULER_LOGIN"'),
        position(steps, f'{STORE} grant-schedule-reader "$SCHEDULER_LOGIN"'),
        # 4. check.
        position(steps, f"{STORE} check-deploy"),
        # 5. the project's values into a copy, then deploy the API from it,
        #    then count its machines.
        position(steps, "fill-fly-config"),
        position(steps, "fly deploy"),
        position(steps, "check-machine-count"),
        # The scheduler machine moves to the same image.
        position(steps, "fly machine update"),
        # 6. smoke: the revision, the Data API, the Auth settings.
        position(steps, "/health"),
        position(steps, "/rest/v1/jobs"),
        position(steps, "deploy check-auth-settings"),
        # 7. the SPA, last.
        position(steps, "vercel deploy"),
    ]
    assert order == sorted(order) and len(set(order)) == len(order), order


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_deploy_checks_name_what_they_check(environment: str) -> None:
    steps = environment_steps(environment)
    check = steps[position(steps, f"{STORE} check-deploy")].code
    # Every login role is checked, the scheduler's by its own kind.
    assert '--login "$API_LOGIN"' in check
    assert '--login "$RUN_LOGIN"' in check
    assert '--scheduler-login "$SCHEDULER_LOGIN"' in check

    deploy = steps[position(steps, "fly deploy")].code
    # The image by digest, never a rebuild, one machine, and the copy this
    # environment's file was filled into, not the file as committed.
    assert re.search(r'--image "registry\.fly\.io/\$FLY_APP@\$DIGEST"', deploy)
    assert "--ha=false" in deploy
    assert f'--config "$RUNNER_TEMP/fly.{environment}.toml"' in deploy
    assert f"deploy/fly.{environment}" not in deploy
    fill = steps[position(steps, "fill-fly-config")].code
    assert f"--config deploy/fly.{environment}.toml" in fill
    assert f'--out "$RUNNER_TEMP/fly.{environment}.toml"' in fill
    # The machines counted are the API app's; the revision asked of this
    # environment's own host, never staging's from production.
    count = steps[position(steps, "check-machine-count")].code
    assert 'fly machines list --app "$FLY_APP" --json' in count
    assert '"https://$FLY_APP.fly.dev/health"' in steps[position(steps, "/health")].code

    smoke = steps[position(steps, "/health")].code
    assert ".revision == $sha" in smoke  # equals the commit
    rest = steps[position(steps, "/rest/v1/jobs")].code
    assert "== 2*" in rest  # a 2xx answer fails the deploy

    spa = steps[position(steps, "vercel deploy")].code
    pulled = [step.index for step in steps if "vercel pull" in step.code]
    built = [step.index for step in steps if "vercel build" in step.code]
    assert pulled == built == [position(steps, "vercel deploy")]
    assert spa.index("vercel pull") < spa.index("vercel build") < spa.index("vercel deploy")
    assert "--prebuilt" in spa


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_scheduler_machine_is_never_updated_without_its_start(environment: str) -> None:
    """Spec 108, amendment of 2026-10-12: `--skip-start` leaves a scheduled
    machine without ticks until it is started once. The deploy never passes
    it, in a command or in a comment-free line of any step."""
    steps = environment_steps(environment)
    assert not any("--skip-start" in step.code for step in steps)
    update = steps[position(steps, "fly machine update")].code
    assert "--image" in update


def test_production_checks_that_the_scheduler_ticked_and_staging_does_not() -> None:
    production = environment_steps("production")
    tick = position(production, "check-scheduler-tick")
    assert tick > position(production, "vercel deploy")  # after its last step
    step = production[tick]
    # Up to 70 minutes, so the job's limit is longer than the wait.
    limit = re.search(r"timeout-minutes:\s*(\d+)", step.text)
    assert limit is not None
    assert int(limit.group(1)) > 70
    assert '--machine "$MACHINE"' in step.code and '--since "$UPDATED_AT"' in step.code
    # The machine and the update time come from the update's own step.
    update = production[position(production, "fly machine update")]
    assert "machine=" in update.code and "updated_at=" in update.code
    # The time is taken after the update, so the update's own start is not a
    # tick after it.
    assert update.code.index("fly machine update") < update.code.index("updated_at=")
    # Staging's scheduler machine has no schedule, so there is nothing to wait for.
    assert not any("check-scheduler-tick" in s.code for s in environment_steps("staging"))


def test_production_takes_a_sha_and_refuses_one_staging_never_deployed() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    on = text[text.index("\non:\n") : text.index("\ndefaults:")]
    # A hand-started production deploy with a commit input; staging follows
    # a CI run on main; nothing runs on a pull request, so a fork's never
    # sees an environment secret.
    assert "workflow_dispatch:" in on and "sha:" in on and "required: true" in on
    assert "workflow_run:" in on and "workflows: [CI]" in on and "branches: [main]" in on
    assert "pull_request" not in on and "pull_request_target" not in on

    # Production names its environment, where Akin approves, and waits for the
    # gate job, which has no environment of its own: it runs before the
    # production secrets are released.
    production = job_header("production")
    assert "environment: production" in production
    assert "needs: staging-ran" in production
    assert "environment:" not in job_header("staging-ran")
    gate = "\n".join(step.code for step in environment_steps("staging-ran"))
    assert "environment=staging" in gate and "sha=$SHA" in gate
    assert "= success" in gate and "exit 1" in gate
    # The gate checks the sha's shape before it asks GitHub anything.
    assert "^[0-9a-f]{40}$" in gate
    assert gate.index("^[0-9a-f]{40}$") < gate.index("gh api")

    # Staging only after a CI run that passed, on a push.
    staging = job_header("staging")
    assert "environment: staging" in staging
    assert "workflow_run.conclusion == 'success'" in staging
    assert "workflow_run.event == 'push'" in staging


def test_two_deploys_of_one_environment_never_interleave() -> None:
    for environment in ("staging", "production"):
        header = job_header(environment)
        assert f"group: deploy-{environment}" in header
        assert "cancel-in-progress: false" in header


def test_each_secret_reaches_only_the_steps_that_need_it() -> None:
    """GitHub never holds the API's password, the service role key or the
    credentials key; Fly never holds the migrator's. Each secret is read in an
    `env:` block, never inside a script, and the migrator's URL only by the
    steps that run `harrier store`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    used = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    assert used == {
        "HARRIER_MIGRATE_DATABASE_URL",
        "FLY_API_TOKEN",
        "FLY_SCHEDULER_API_TOKEN",
        "FLY_STAGING_API_TOKEN",
        "VERCEL_TOKEN",
    }
    for name in ("HARRIER_DATABASE_URL", "HARRIER_RUN_DATABASE_URL", "HARRIER_CREDENTIALS_KEY"):
        assert f"secrets.{name}" not in text
    assert not re.search(r"service_role|sb_secret_|SERVICE_ROLE", text, re.IGNORECASE)

    for steps in workflow_jobs().values():
        for step in steps:
            # No expression inside a script: an input cannot be run as code.
            assert "${{" not in step.run, step.name
            if "secrets.HARRIER_MIGRATE_DATABASE_URL" in step.code:
                assert f"{STORE} " in step.run, step.name
    # The staging token reaches the production job only to read staging's image.
    staging = "\n".join(step.code for step in environment_steps("staging"))
    assert "FLY_STAGING_API_TOKEN" not in staging


def test_no_checkout_leaves_the_token_in_the_checkout() -> None:
    """Each checkout sets `persist-credentials: false`, so the token it used
    is not left in `.git/config` for the package installs and CLIs that run
    after it (CodeRabbit on PR #329). No step here pushes."""
    for job, steps in workflow_jobs().items():
        for step in steps:
            if "actions/checkout@" in step.code:
                assert "persist-credentials: false" in step.code, (job, step.name)


FLY_CALL = re.compile(r"(?:^|[\s(|;&])fly\s")


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_each_job_names_flyctl_as_fly_before_it_calls_fly(environment: str, tmp_path: Path) -> None:
    """The setup action installs `flyctl` and no `fly`, so the first staging
    deploy stopped at `fly deploy` with "command not found" (run
    38617000034). A step before the first `fly` call makes the name, next to
    the binary the action put on PATH."""
    steps = environment_steps(environment)
    naming = position(steps, "Name flyctl as fly")
    first_call = min(step.index for step in steps if FLY_CALL.search(step.run))
    assert naming < first_call

    done = run_step(environment, "Name flyctl as fly", {}, {"flyctl": "echo flyctl"}, tmp_path)
    assert done.returncode == 0, done.stderr
    linked = tmp_path / "bin" / "fly"
    assert linked.is_symlink()
    assert linked.resolve() == (tmp_path / "bin" / "flyctl").resolve()


def test_the_image_is_built_once_and_production_never_builds() -> None:
    production = "\n".join(step.code for step in environment_steps("production"))
    assert "docker build" not in production
    assert "--preserve-digests" in production
    staging = "\n".join(step.code for step in environment_steps("staging"))
    assert staging.count("docker build") == 1
    assert '--build-arg HARRIER_REVISION="$SHA"' in staging


def test_a_failed_step_stops_the_job() -> None:
    """The contract at the top of the workflow: a step that exits 1 stops the
    deploy. Nothing may make a step, or the production job, run on after a
    failure, or take a secret into every step."""
    text = WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "continue-on-error" not in code
    assert not re.search(r"always\(\)|failure\(\)|cancelled\(\)", code)
    assert not re.search(r"\|\|\s*(true|:|echo)|set \+e", code)
    for steps in workflow_jobs().values():
        for step in steps:
            # No step runs conditionally: each runs, or the job has stopped.
            assert not re.search(r"^\s{8}if:", step.code, re.MULTILINE), step.name
    # Only the jobs' own `if:` select which trigger runs which job.
    assert "if:" not in job_header("production")
    assert "if:" in job_header("staging") and "if:" in job_header("staging-ran")
    # No secret in a job's own `env:`, which every step, and every action,
    # would receive.
    for job in ("staging", "staging-ran", "production"):
        assert "secrets." not in job_header(job), job


def test_the_workflow_reader_sees_a_run_written_either_way() -> None:
    """The reader behind the checks above finds a `run:` on a step line of
    its own and one written `- run:`, so a script moved to the other form is
    still read."""
    separate = Step(0, '      - name: a\n        run: echo "${{ inputs.sha }}"', "")
    inline = Step(1, '      - run: echo "${{ inputs.sha }}"', "")
    assert "${{" in separate.run and "${{" in inline.run


def run_step(
    job: str, name_part: str, env: dict[str, str], stubs: dict[str, str], tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """One workflow step's script, run under bash with stub commands first on
    PATH: what the step decides, not what it says."""
    step = next(s for s in environment_steps(job) if name_part in s.name)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for command, body in stubs.items():
        path = bin_dir / command
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
        path.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "GITHUB_REPOSITORY": "owner/repo",
        **env,
    }
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step.run],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


GOOD_SHA = "a" * 40
# What the gate asks GitHub: the deployments of an environment and sha, then
# one deployment's statuses.
GH_STUB = """
case "$*" in
  *"/deployments?environment=staging"*"$EXPECT_SHA"*) printf '%s' "$DEPLOYMENTS" | tr ',' '\\n' ;;
  *"/statuses"*) printf '%s\\n' "$STATE" ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
"""


@pytest.mark.parametrize(
    ("sha", "deployments", "state", "passes"),
    [
        (GOOD_SHA, "11", "success", True),
        (GOOD_SHA, "11,12", "success", True),
        (GOOD_SHA, "11", "failure", False),
        (GOOD_SHA, "11", "in_progress", False),
        (GOOD_SHA, "11", "", False),
        (GOOD_SHA, "", "success", False),
        ("abc123", "11", "success", False),
        (GOOD_SHA.upper(), "11", "success", False),
        (GOOD_SHA + "a", "11", "success", False),
        ("", "11", "success", False),
    ],
    ids=[
        "deployed",
        "one of two",
        "failed",
        "not finished",
        "no status",
        "never deployed",
        "short sha",
        "upper case",
        "too long",
        "empty",
    ],
)
def test_the_production_gate_refuses_what_staging_never_deployed(
    sha: str, deployments: str, state: str, passes: bool, tmp_path: Path
) -> None:
    """The gate's script, run with a stub `gh`: exit 0 only for a commit with
    a successful staging deployment."""
    done = run_step(
        "staging-ran",
        "Refuse a commit",
        {
            "GH_TOKEN": "synthetic",
            "SHA": sha,
            "EXPECT_SHA": sha,
            "DEPLOYMENTS": deployments,
            "STATE": state,
        },
        {"gh": GH_STUB},
        tmp_path,
    )
    assert (done.returncode == 0) is passes, (done.stdout, done.stderr)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_the_smoke_steps_fail_the_deploy(environment: str, tmp_path: Path) -> None:
    """The revision and the Data API probe, run with a stub `curl`: a machine
    still on another revision, and a 2xx from the REST endpoint, exit 1."""
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed; the smoke step is not run")
    env = {
        "SHA": GOOD_SHA,
        "FLY_APP": "synthetic-app",
        "HARRIER_SUPABASE_URL": "https://project.example.invalid",
        "HARRIER_SUPABASE_PUBLISHABLE_KEY": "not-a-real-key-not-a-real-key",
    }
    health = 'printf \'{"revision": "%s"}\' "$REVISION"\n'
    for revision, passes in ((GOOD_SHA, True), ("b" * 40, False), ("unknown", False)):
        done = run_step(
            environment,
            "the API answers as the commit built",
            {**env, "REVISION": revision},
            {"curl": health},
            tmp_path,
        )
        assert (done.returncode == 0) is passes, (revision, done.stderr)

    # A probe that never reached the endpoint is not proof that the Data API
    # is off: curl prints 000 and exits non-zero, and the step's `bash -e`
    # stops there (CodeRabbit on PR #329).
    for code in (6, 7, 28):
        done = run_step(
            environment,
            "the Data API is off",
            env,
            {"curl": f'printf "000"\nexit {code}\n'},
            tmp_path,
        )
        assert done.returncode != 0, code

    probe = 'printf "%s" "$STATUS"\n'
    for status, passes in (
        ("404", True),
        ("401", True),
        ("403", True),
        ("200", False),
        ("204", False),
    ):
        done = run_step(
            environment, "the Data API is off", {**env, "STATUS": status}, {"curl": probe}, tmp_path
        )
        assert (done.returncode == 0) is passes, (status, done.stderr)
        if not passes:
            assert "turn it off" in done.stdout


# --- the compose file ---


def compose_environment() -> dict[str, str]:
    """The service's `environment:` assignments, read as the file writes
    them: `NAME: value`."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "environment:")
    found: dict[str, str] = {}
    for line in lines[start + 1 :]:
        match = re.match(r"^      ([A-Z_]+):\s*(.*)$", line)
        if not match:
            break
        found[match.group(1)] = match.group(2).strip().strip('"')
    return found


def test_the_compose_file_sets_the_store_and_the_auth_mode_empty() -> None:
    environment = compose_environment()
    assert environment["HARRIER_DATABASE_URL"] == ""
    assert environment["HARRIER_AUTH"] == ""
    # `.env` still reaches the container (the credential rides in it): the
    # override is what keeps these two out.
    assert "env_file:" in COMPOSE.read_text(encoding="utf-8")


def docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


def test_the_local_container_never_takes_a_store_from_dotenv(tmp_path: Path) -> None:
    """With a hosted store URL and the hosted mode set in `.env`, the compose
    file, as docker resolves it, hands the container empty values."""
    if not docker_compose_available():
        message = "docker compose is not installed; the compose file is not resolved"
        import os

        if os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}:
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text(
        "HARRIER_DATABASE_URL=postgresql://synthetic:synthetic@db.example.invalid:5432/postgres\n"
        "HARRIER_AUTH=supabase\n"
        "HARRIER_MARKER_FROM_DOTENV=reaches-the-container\n",
        encoding="utf-8",
    )
    resolved = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    environment = json.loads(resolved.stdout)["services"]["harrier"]["environment"]
    # The two the file sets empty.
    assert environment["HARRIER_DATABASE_URL"] == ""
    assert environment["HARRIER_AUTH"] == ""
    # The proof that `.env` is read at all: another key from it arrives.
    assert environment["HARRIER_MARKER_FROM_DOTENV"] == "reaches-the-container"


# --- the runbook ---

RUNBOOK = ROOT / "docs" / "deploy.md"


def test_the_runbook_names_every_secret_and_variable_the_workflow_reads() -> None:
    """Derived from the workflow, so a secret or variable added to it without
    the runbook fails here."""
    text = RUNBOOK.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    names = set(re.findall(r"(?:secrets|vars)\.([A-Z0-9_]+)", workflow))
    assert {"HARRIER_MIGRATE_DATABASE_URL", "FLY_API_TOKEN", "VERCEL_ORG_ID"} <= names
    missing = sorted(name for name in names if f"`{name}`" not in text)
    assert missing == [], f"docs/deploy.md never names {missing}"


def test_the_runbook_covers_what_the_spec_asks_of_it() -> None:
    # One space between words, so a line that wraps does not hide a phrase.
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    for required in (
        # Each Supabase setting, with its check.
        "Enable Data API",
        "jwks.json",
        "harrier deploy check-auth-settings",
        "{{ .Token }}",
        # Every secret's home: the three Fly secrets, and where each stays out of.
        "HARRIER_DATABASE_URL",
        "HARRIER_RUN_DATABASE_URL",
        "HARRIER_CREDENTIALS_KEY",
        "HARRIER_SUPABASE_SECRET_KEY",
        # The login roles, and the operator login.
        "harrier_run",
        "harrier_scheduler",
        "operator login",
        "\\password",
        "pg_terminate_backend",
        # The scheduler machine, scheduled in production and not in staging.
        "--schedule hourly --restart no",
        "harrier operator scheduler",
        "HARRIER_SCHEDULER_CONCURRENCY",
        "HARRIER_SCHEDULED_RUN_TIMEOUT_SECONDS",
        # No dedicated address and no volume; both Vercel projects on Hobby.
        "--shared",
        "No volume is created",
        "Hobby",
        "non-commercial",
        # What backs up the bucket.
        "Nothing backs up the Storage bucket `harrier-files`",
        # A failed deploy after a migration, and revoking each credential.
        "A deploy that fails halfway",
        "fixed forward",
        "Revoking a credential",
        "fly tokens revoke",
        # The cost check, and where its figures come from.
        "harrier deploy project-cost",
        "Cost Explorer",
        "usage page",
        "grant-schedule-reader",
    ):
        assert required in text, f"docs/deploy.md never says {required!r}"
    # The production scheduler is scheduled and the staging one is not.
    assert text.count("--schedule hourly") >= 1
    assert "without `--schedule hourly`" in text or "without --schedule hourly" in text


# --- classification ---


def test_the_classification_names_what_a_vercel_or_fly_cli_writes_into_a_checkout() -> None:
    """`vercel link` and `vercel pull` write project ids and pulled values
    into `.vercel/`; the per-environment env files and the SPA's own are
    never in git. Each is named one by one, since the classification matches
    with `fnmatch`, where a `.env.*` glob would also match the tracked
    `.env.example` (spec 110, "classification"; spec 114)."""
    raw = json.loads((ROOT / "config" / "data-classification.json").read_text(encoding="utf-8"))
    patterns = set(raw["never_in_git"]["patterns"])
    assert {
        ".env.staging",
        ".env.production",
        ".vercel/**",
        "apps/web/.vercel/**",
        "apps/web/.env",
        "apps/web/.env.local",
    } <= patterns
    assert ".env.*" not in patterns
    # And the two ignore files carry `.vercel/`, at any depth for docker.
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".vercel/" in gitignore
    assert ".vercel/" in dockerignore and "**/.vercel/" in dockerignore


# --- no error reporter ---

REPORTERS = re.compile(
    r"sentry[-_]sdk|@sentry/|\brollbar\b|\bbugsnag\b|\bhoneybadger\b", re.IGNORECASE
)


def reporters_in(manifest: str) -> list[str]:
    return sorted(set(REPORTERS.findall(manifest)))


def test_the_reporter_scan_finds_each_name_it_looks_for() -> None:
    for name in (
        'dependencies = ["sentry-sdk>=2"]',
        '"@sentry/react": "^9.0.0"',
        '"@sentry/browser": "^9.0.0"',
        'rollbar = "*"',
        '"bugsnag>=4"',
        '"honeybadger"',
        '"Sentry_SDK"',
    ):
        assert reporters_in(name), name
    assert reporters_in('dependencies = ["fastapi", "psycopg[binary]"]') == []


def test_no_error_reporter_is_installed() -> None:
    """None is installed, and adding one is its own spec: Sentry's Python SDK
    captures local variables by default, and a frame of `postgres_connect`
    holds the store's URL and its password (spec 110, "error reporters")."""
    for manifest in (
        ROOT / "services" / "api" / "pyproject.toml",
        ROOT / "apps" / "web" / "package.json",
    ):
        assert reporters_in(manifest.read_text(encoding="utf-8")) == [], manifest.name
