"""The nightly backup workflow and the runbook that restores it (spec 153).

No runner can execute GitHub's workflow here, so the properties that are the
file's own order and content are read from the file, as
`tests/test_deploy_files.py` reads `deploy.yml` and holds it to the same rules:
no secret in a job's `env`, secrets only on the steps that need them, no
`continue-on-error`. The reader below follows that file's: a job is a two
space key under `jobs:`, a step a six space `- `. What the commands do is
executed in `tests/test_hosted_backup.py`.

Every assertion reads instructions, not comments: a comment that mentions a
command satisfies nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest
from test_deploy_files import RUNBOOK

from harrier import hostedbackup
from harrier.demo import repo_root

ROOT = repo_root()
WORKFLOW = ROOT / ".github" / "workflows" / "backup.yml"


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
        """The step's `run` script without comment lines, or ''."""
        lines = self.text.splitlines()
        for number, line in enumerate(lines):
            if re.match(r"^(\s{8}|\s{6}- )run:", line):
                first = line.split("run:", 1)[1]
                body = ["" if first.strip() in ("|", ">") else first, *lines[number + 1 :]]
                return "\n".join(
                    part for part in body if part.strip() and not part.lstrip().startswith("#")
                )
        return ""


def code_of(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def workflow_steps() -> list[Step]:
    text = WORKFLOW.read_text(encoding="utf-8")
    jobs_at = text.index("\njobs:\n")
    current: list[str] = []
    steps: list[Step] = []

    def close() -> None:
        nonlocal current
        if current:
            body = "\n".join(current)
            steps.append(Step(len(steps), body, code_of(body)))
            current = []

    for line in text[jobs_at:].splitlines():
        if re.match(r"^      - ", line):
            close()
        if current or re.match(r"^      - ", line):
            current.append(line)
    close()
    return steps


def job_header() -> str:
    """Everything of the one job before its steps, comments removed."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("\n  backup:\n")
    return code_of(text[start : text.index("\n    steps:\n", start)])


def trigger_header() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    return code_of(text[: text.index("\njobs:\n")])


def position(marker: str) -> int:
    """The index of the one step whose code holds `marker`."""
    holding = [step.index for step in workflow_steps() if marker in step.code]
    assert len(holding) == 1, f"{marker!r} is in steps {holding}"
    return holding[0]


def test_the_workflow_reader_finds_the_steps() -> None:
    """The reader finds what the tests below depend on, so none is vacuous."""
    steps = workflow_steps()
    assert len(steps) == 8
    assert [step.name for step in steps][2] == "Install the PostgreSQL 17 client and age"
    assert "backup-dump" in steps[3].run
    assert "secrets.HARRIER_MIGRATE_DATABASE_URL" in steps[3].code


def test_the_workflow_reader_sees_a_run_written_either_way() -> None:
    separate = Step(0, '      - name: a\n        run: echo "${{ inputs.sha }}"', "")
    inline = Step(1, '      - run: echo "${{ inputs.sha }}"', "")
    assert "${{" in separate.run and "${{" in inline.run


def test_the_backup_workflow_uploads_only_after_the_restore_check() -> None:
    """The steps run in this order: dump, check, encrypt, upload. The check
    restores the plain dump before anything is encrypted, and the upload is
    the last step. No step is conditional and nothing lets a failed step go
    on, so a check that fails the job stops it with nothing uploaded
    (spec 153, acceptance; the check's own failure is executed in
    `tests/test_hosted_backup.py`)."""
    dump = position("harrier store backup-dump")
    check = position("harrier store backup-check")
    encrypt = position("harrier store backup-seal")
    upload = position("actions/upload-artifact@")
    assert dump < check < encrypt < upload == len(workflow_steps()) - 1

    steps = workflow_steps()
    # The check reads the directory the dump wrote; the seal reads the one the
    # check passed; the upload reads what the seal wrote.
    dump_out = re.search(r"backup-dump --out (\S+)", steps[dump].run)
    check_dir = re.search(r"backup-check\s+\\?\s*--dir (\S+)", steps[check].run)
    seal_dir = re.search(r"--dir (\S+)", steps[encrypt].run)
    assert dump_out and check_dir and seal_dir
    assert dump_out.group(1) == check_dir.group(1) == seal_dir.group(1)
    # The check restores into the service container, an empty Postgres 17.
    assert "--into postgresql://postgres:postgres@localhost:5432/" in steps[check].run
    assert "image: postgres:17" in job_header()

    code = code_of(WORKFLOW.read_text(encoding="utf-8"))
    assert "continue-on-error" not in code
    assert not re.search(r"always\(\)|failure\(\)|cancelled\(\)", code)
    assert not re.search(r"\|\|\s*(true|:|echo)|set \+e", code)
    for step in steps:
        assert not re.search(r"^\s{8}if:", step.code, re.MULTILINE), step.name


def test_the_backup_workflow_reads_only_the_migrator_url() -> None:
    """The one secret is the migrator's URL, read by the dump step alone, in
    that step's `env:` and not in a script. No step names a Supabase API key,
    and no secret is in the job's own `env:`."""
    text = WORKFLOW.read_text(encoding="utf-8")
    code = code_of(text)
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", code)) == {"HARRIER_MIGRATE_DATABASE_URL"}
    assert not re.search(r"service_role|sb_secret_|SERVICE_ROLE|SECRET_KEY", code, re.IGNORECASE)
    assert not re.search(r"HARRIER_SUPABASE|PUBLISHABLE", code)

    holding = [step for step in workflow_steps() if "secrets." in step.code]
    assert [step.name for step in holding] == ["Dump the hosted store and write its manifest"]
    assert "harrier store backup-dump" in holding[0].run
    assert "HARRIER_DATABASE_URL: ${{ secrets.HARRIER_MIGRATE_DATABASE_URL }}" in holding[0].code
    for step in workflow_steps():
        # No expression inside a script: an input cannot be run as code.
        assert "${{" not in step.run, step.name
    assert "secrets." not in job_header()
    # Nothing at the job's own level is an `env:` (the service container's is
    # nested a level lower and holds a throwaway password).
    assert not re.search(r"^    env:", job_header(), re.MULTILINE)


def test_the_backup_artifact_is_encrypted_and_kept_thirty_days() -> None:
    """The artifact is the `age` output alone, kept 30 days, named by the UTC
    date. Nothing else is uploaded, cached or printed from the plain files."""
    steps = workflow_steps()
    upload = steps[position("actions/upload-artifact@")]
    seal = steps[position("harrier store backup-seal")]
    sealed = re.search(r"--out \"?\$RUNNER_TEMP/(\S+?)\"?\s*$", seal.run, re.MULTILINE)
    assert sealed is not None and sealed.group(1).endswith(".tar.age")
    assert f"path: ${{{{ runner.temp }}}}/{sealed.group(1)}" in upload.code
    assert "retention-days: 30" in upload.code
    assert "if-no-files-found: error" in upload.code
    assert "name: hosted-backup-${{ steps.stamp.outputs.date }}" in upload.code
    assert "date -u +%Y-%m-%d" in steps[position("id: stamp")].run

    code = code_of(WORKFLOW.read_text(encoding="utf-8"))
    assert code.count("upload-artifact") == 1
    assert "actions/cache" not in code
    # The plain directory is never a path of the upload, and nothing prints,
    # copies or archives it.
    assert "path: ${{ runner.temp }}/backup" not in code
    for step in steps:
        assert not re.search(r"(?:^|[\s|;&(])(cat|tee|base64|zip|tar)\s", step.run), step.name
    assert "retention-days" not in code.replace("retention-days: 30", "")


def test_the_backup_runs_nightly_on_main_in_the_staging_environment() -> None:
    triggers = trigger_header()
    assert re.search(r'cron: "30 3 \* \* \*"', triggers)
    assert "workflow_dispatch:" in triggers
    header = job_header()
    assert "environment: staging" in header
    assert "if: github.ref == 'refs/heads/main'" in header
    assert "group: backup" in header and "cancel-in-progress: false" in header
    assert "permissions:\n      contents: read" in header


def test_the_runner_has_the_client_for_the_servers_version_and_age() -> None:
    """The runner's own `pg_dump` is older than the server's. The client comes
    from PostgreSQL's apt repository, at the version the code insists on."""
    install = workflow_steps()[position("postgresql-client-")]
    assert f"postgresql-client-{hostedbackup.PG_MAJOR}" in install.run
    assert "apt.postgresql.org.sh" in install.run
    assert re.search(r"apt-get install -y [^\n]*\bage\b", install.run)
    assert f"image: postgres:{hostedbackup.PG_MAJOR}" in job_header()


def test_the_actions_are_pinned_as_the_deploy_workflow_pins_them() -> None:
    """Major version tags of the actions `ci.yml` and `deploy.yml` already use,
    and a checkout that leaves no token behind."""
    code = code_of(WORKFLOW.read_text(encoding="utf-8"))
    used = re.findall(r"uses:\s*(\S+)", code)
    assert used == [
        "actions/checkout@v4",
        "astral-sh/setup-uv@v5",
        "actions/upload-artifact@v4",
    ]
    checkout = workflow_steps()[position("actions/checkout@")]
    assert "persist-credentials: false" in checkout.code


def test_the_runbook_names_every_secret_and_variable_the_backup_workflow_reads() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    names = set(re.findall(r"(?:secrets|vars)\.([A-Z0-9_]+)", code_of(workflow)))
    assert names == {"HARRIER_MIGRATE_DATABASE_URL"}
    assert all(f"`{name}`" in text for name in names)


def test_the_runbook_states_the_restore_procedure() -> None:
    """`docs/deploy.md` names the workflow and the commands, and states the
    restore in order, the ledger re-erase included (spec 153, acceptance),
    with the bucket and policy made before check-deploy and the files
    uploaded from the newest copy before import-artifacts (spec 155,
    acceptance)."""
    text = " ".join(RUNBOOK.read_text(encoding="utf-8").split())
    section = text[
        text.index("## Restoring the hosted store") : text.index("## A deploy that fails")
    ]
    ordered = [
        "gh run download",
        "age --decrypt",
        "fly scale count 0",
        "pg_restore",
        "harrier operator erase",
        "harrier store restore-storage",
        "harrier store check-deploy",
        "fly scale count 1",
        "harrier store restore-files",
        "harrier store import-artifacts",
    ]
    at = [section.index(phrase) for phrase in ordered]
    assert at == sorted(at), dict(zip(ordered, at, strict=True))
    assert "data/hosted/erasures.jsonl" in section
    assert "started_at" in section
    backing = text[text.index("## Backing up the hosted store") : text.index("## Restoring")]
    for named in (
        ".github/workflows/backup.yml",
        "harrier store backup-dump",
        "harrier store backup-check",
        "harrier store backup-seal",
        "harrier store backup-files",
        "hosted-backup-<UTC date>",
        "30 days",
        "deploy/backup-recipient.txt",
    ):
        assert named in backing, named
    assert "Nothing backs up the Storage bucket `harrier-files` on a schedule" in text


def test_the_runbook_restores_in_the_order_the_check_does() -> None:
    """The check is the rehearsal of the procedure, so the runbook's
    `pg_restore` lines carry the flags `restore_steps` gives each step, in the
    same order, and its list filter is the pattern the code uses."""
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text[
        text.index("## Restoring the hosted store") : text.index("## A deploy that fails")
    ]
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("pg_restore")]
    steps = hostedbackup.restore_steps(
        ROOT / "x", "postgresql://target/db", "pg_restore", ROOT / "public.list"
    )
    cursor = 0
    for _name, argv in steps:
        wanted = [
            arg for arg in argv if arg.startswith("--section=") or arg.startswith("--use-list=")
        ]
        wanted = [
            arg.split("=")[0] + ("=public.list" if "use-list" in arg else "=" + arg.split("=")[1])
            for arg in wanted
        ]
        while cursor < len(lines) and not all(flag in lines[cursor] for flag in wanted):
            cursor += 1
        assert cursor < len(lines), (argv, wanted)
        cursor += 1
    assert f"grep -v -E '{hostedbackup.PUBLIC_SCHEMA_LINES}'" in section


@pytest.mark.parametrize("name", ["backup-dump", "backup-check", "backup-seal", "backup-files"])
def test_each_backup_command_is_registered_once(name: str) -> None:
    """The CLI's table of where a command runs and the settings route's table
    of commands the web app cannot run each hold the command exactly once."""
    from harrier_api.settings_routes import COMMAND_PLACES, TerminalOnly
    from harrier_cli.main import COMMAND_CLASSES

    assert f"store {name}" in COMMAND_CLASSES
    assert isinstance(COMMAND_PLACES[f"store {name}"], TerminalOnly)
