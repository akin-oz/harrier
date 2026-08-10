"""Behavior pins for the generated launchd schedule (spec 020).

The old repo's plists were hand-maintained and drifted into a stale
path; these pins prove the generated ones cannot. launchctl is injected
so nothing here touches the real launchd.
"""

import json
import plistlib
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from harrier.schedule import (
    ScheduleConfigError,
    install_schedule,
    load_schedule,
    next_run_after,
    render_plist_dict,
    schedule_status,
    uninstall_schedule,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG = REPO_ROOT / "config" / "schedule.json"


class FakeLaunchctl:
    """Records the command lines the installer builds."""

    def __init__(self, *, print_output: str = "", print_code: int = 1) -> None:
        self.calls: list[list[str]] = []
        self.print_output = print_output
        self.print_code = print_code

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        if args and args[0] == "print":
            return self.print_code, self.print_output, ""
        return 0, "", ""


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    return {
        "config_path": CONFIG,
        "agents_dir": tmp_path / "LaunchAgents",
        "root": tmp_path / "repo",
        "interpreter": tmp_path / "repo" / ".venv" / "bin" / "python",
        "log_directory": tmp_path / "data" / "logs",
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_committed_schedule_config_carries_the_current_cadence() -> None:
    prefix, jobs = load_schedule(CONFIG)
    assert prefix
    names = [job.name for job in jobs]
    assert names == ["discovery", "digest", "gmail-watch"]
    discovery = jobs[0]
    assert discovery.command == ("discover", "--scheduled")
    assert [(t.hour, t.minute) for t in discovery.times] == [(9, 0), (13, 0), (16, 0), (20, 0)]
    assert [(t.hour, t.minute) for t in jobs[1].times] == [(20, 30)]
    assert jobs[2].seconds == 300


def test_invalid_schedule_config_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "schedule.json"
    bad.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "discovery",
                        "command": ["discover"],
                        "trigger": {"kind": "calendar", "times": [{"hour": 99, "minute": 0}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"discovery.*hour must be 0-23"):
        load_schedule(bad)

    unknown = tmp_path / "unknown.json"
    unknown.write_text(
        json.dumps(
            {"jobs": [{"name": "x", "command": ["discover"], "trigger": {"kind": "moonphase"}}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"unknown trigger kind"):
        load_schedule(unknown)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_rendered_plists_invoke_the_cli_without_a_shell(env: dict[str, Any]) -> None:
    prefix, jobs = load_schedule(CONFIG)
    for job in jobs:
        body = render_plist_dict(
            job,
            prefix,
            root=env["root"],
            interpreter=env["interpreter"],
            log_directory=env["log_directory"],
        )
        args = body["ProgramArguments"]
        # No shell wrapper anywhere: the old .env-sourcing bash wrapper is
        # what killed the nightly digest (spec 020).
        assert not any(
            part.endswith(("/bash", "/sh", ".sh")) or part in ("bash", "sh") for part in args
        )
        assert args[0] == str(env["interpreter"])
        assert args[1:3] == ["-m", "harrier_cli.main"]
        assert body["RunAtLoad"] is False
        assert body["WorkingDirectory"] == str(env["root"])


def test_calendar_and_interval_triggers_render_correctly(env: dict[str, Any]) -> None:
    prefix, jobs = load_schedule(CONFIG)
    by_name = {job.name: job for job in jobs}
    discovery = render_plist_dict(
        by_name["discovery"],
        prefix,
        root=env["root"],
        interpreter=env["interpreter"],
        log_directory=env["log_directory"],
    )
    assert discovery["StartCalendarInterval"] == [
        {"Hour": 9, "Minute": 0},
        {"Hour": 13, "Minute": 0},
        {"Hour": 16, "Minute": 0},
        {"Hour": 20, "Minute": 0},
    ]
    digest = render_plist_dict(
        by_name["digest"],
        prefix,
        root=env["root"],
        interpreter=env["interpreter"],
        log_directory=env["log_directory"],
    )
    # A single time renders as a dict, matching launchd's own shape.
    assert digest["StartCalendarInterval"] == {"Hour": 20, "Minute": 30}
    watch = render_plist_dict(
        by_name["gmail-watch"],
        prefix,
        root=env["root"],
        interpreter=env["interpreter"],
        log_directory=env["log_directory"],
    )
    assert watch["StartInterval"] == 300
    assert "StartCalendarInterval" not in watch


def test_next_run_after_calendar_and_interval() -> None:
    _prefix, jobs = load_schedule(CONFIG)
    by_name = {job.name: job for job in jobs}
    morning = datetime(2026, 8, 10, 10, 0)
    assert next_run_after(by_name["discovery"], morning) == datetime(2026, 8, 10, 13, 0)
    late = datetime(2026, 8, 10, 23, 0)
    # Past the last slot, the next run rolls to tomorrow.
    assert next_run_after(by_name["discovery"], late) == datetime(2026, 8, 11, 9, 0)
    assert next_run_after(by_name["gmail-watch"], morning) == datetime(2026, 8, 10, 10, 5)


# ---------------------------------------------------------------------------
# Install, status, uninstall
# ---------------------------------------------------------------------------


def test_install_writes_three_plists_at_the_real_repo_path(env: dict[str, Any]) -> None:
    launchctl = FakeLaunchctl()
    result = install_schedule(**env, launchctl=launchctl)
    assert len(result.written) == 3
    assert len(result.loaded) == 3
    for path in result.written:
        body = plistlib.loads(path.read_bytes())
        assert body["WorkingDirectory"] == str(env["root"])
        # Nothing outside the resolved root leaks in: the stale-path defect
        # class the old hand-maintained plists carried. Asserting no home
        # directory at all keeps a real account name out of the fixture.
        assert "/Users/" not in json.dumps(body, default=str)
    bootstraps = [call for call in launchctl.calls if call[0] == "bootstrap"]
    assert len(bootstraps) == 3


def test_install_defaults_to_the_resolved_repo_root(tmp_path: Path) -> None:
    # Without an override, install must resolve the real repo (spec 020);
    # only the output directories are redirected.
    result = install_schedule(
        config_path=CONFIG,
        agents_dir=tmp_path / "LaunchAgents",
        log_directory=tmp_path / "logs",
        launchctl=FakeLaunchctl(),
    )
    body = plistlib.loads(result.written[0].read_bytes())
    assert body["WorkingDirectory"] == str(REPO_ROOT)


def test_dry_run_writes_nothing(env: dict[str, Any]) -> None:
    launchctl = FakeLaunchctl()
    result = install_schedule(**env, dry_run=True, launchctl=launchctl)
    assert result.written == []
    assert result.loaded == []
    assert launchctl.calls == []
    assert not env["agents_dir"].exists()
    assert all("would write" in line for line in result.lines)


def test_status_detects_drift(env: dict[str, Any]) -> None:
    install_schedule(**env, launchctl=FakeLaunchctl())
    loaded_output = "state = running\n\tlast exit code = 0\n"
    statuses = schedule_status(
        **env,
        now=datetime(2026, 8, 10, 10, 0),
        launchctl=FakeLaunchctl(print_output=loaded_output, print_code=0),
    )
    assert [status.name for status in statuses] == ["discovery", "digest", "gmail-watch"]
    assert all(status.installed for status in statuses)
    assert all(status.loaded for status in statuses)
    assert not any(status.drifted for status in statuses)
    assert statuses[0].last_exit_status == "0"
    assert statuses[0].next_run.startswith("2026-08-10T13:00")

    # Hand-edit one plist the way the old setup drifted.
    target = env["agents_dir"] / "dev.harrier.discovery.plist"
    body = plistlib.loads(target.read_bytes())
    body["WorkingDirectory"] = "/Users/someone/Documents/projects/job-hunt-local"
    target.write_bytes(plistlib.dumps(body))
    drifted = schedule_status(**env, launchctl=FakeLaunchctl())
    assert drifted[0].drifted is True
    assert drifted[1].drifted is False
    assert "DRIFTED" in drifted[0].line()


def test_status_reports_missing_when_not_installed(env: dict[str, Any]) -> None:
    statuses = schedule_status(**env, launchctl=FakeLaunchctl())
    assert all(not status.installed for status in statuses)
    assert all(not status.loaded for status in statuses)
    assert "missing" in statuses[0].line()


def test_uninstall_removes_plists(env: dict[str, Any]) -> None:
    install_schedule(**env, launchctl=FakeLaunchctl())
    launchctl = FakeLaunchctl()
    result = uninstall_schedule(
        config_path=env["config_path"], agents_dir=env["agents_dir"], launchctl=launchctl
    )
    assert result.ok
    assert len(result.removed) == 3
    assert list(env["agents_dir"].glob("*.plist")) == []
    assert all(call[0] == "bootout" for call in launchctl.calls)
    # A second uninstall is harmless.
    again = uninstall_schedule(
        config_path=env["config_path"], agents_dir=env["agents_dir"], launchctl=FakeLaunchctl()
    )
    assert again.ok
    assert all("not installed" in line for line in again.lines)


def test_boolean_trigger_values_are_rejected(tmp_path: Path) -> None:
    # A JSON boolean satisfies isinstance(x, int) and would render as a
    # boolean plist value (review finding).
    path = tmp_path / "bool.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "discovery",
                        "command": ["discover"],
                        "trigger": {"kind": "calendar", "times": [{"hour": True, "minute": 0}]},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"hour must be 0-23"):
        load_schedule(path)

    interval = tmp_path / "bool-interval.json"
    interval.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "watch",
                        "command": ["gmail-watch"],
                        "trigger": {"kind": "interval", "seconds": True},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"seconds must be"):
        load_schedule(interval)


def test_duplicate_job_names_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "dupe.json"
    job: dict[str, Any] = {
        "name": "discovery",
        "command": ["discover"],
        "trigger": {"kind": "interval", "seconds": 60},
    }
    path.write_text(json.dumps({"jobs": [job, dict(job)]}), encoding="utf-8")
    with pytest.raises(ScheduleConfigError, match=r"duplicate job name"):
        load_schedule(path)


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "nested/name", "/absolute", ".", ".."],
)
def test_identifiers_cannot_escape_their_directories(tmp_path: Path, bad_name: str) -> None:
    path = tmp_path / "escape.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": bad_name,
                        "command": ["discover"],
                        "trigger": {"kind": "interval", "seconds": 60},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"name must not"):
        load_schedule(path)

    prefix_path = tmp_path / "prefix.json"
    prefix_path.write_text(
        json.dumps(
            {
                "label_prefix": "/etc/cron.d",
                "jobs": [
                    {
                        "name": "discovery",
                        "command": ["discover"],
                        "trigger": {"kind": "interval", "seconds": 60},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ScheduleConfigError, match=r"label_prefix must not"):
        load_schedule(prefix_path)


def test_rendered_paths_stay_inside_their_directories(env: dict[str, Any]) -> None:
    result = install_schedule(**env, launchctl=FakeLaunchctl())
    for path in result.written:
        assert path.parent == env["agents_dir"]
        body = plistlib.loads(path.read_bytes())
        for key in ("StandardOutPath", "StandardErrorPath"):
            assert Path(body[key]).parent == env["log_directory"]


def test_malformed_env_line_does_not_break_the_python_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect class the shell wrapper had: a value spanning a line
    break. Harrier's loader skips the continuation instead of failing."""
    from harrier_cli.main import load_project_env

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GOOD_ONE=first",
                "BROKEN_TOKEN=starts-here",
                "continuation-fragment",
                "GOOD_TWO=second",
                "",
            ]
        ),
        encoding="utf-8",
    )
    for name in ("GOOD_ONE", "GOOD_TWO", "BROKEN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    load_project_env(env_file)
    import os

    assert os.environ["GOOD_ONE"] == "first"
    # The line after the malformed one still loads: no abort.
    assert os.environ["GOOD_TWO"] == "second"
    assert os.environ["BROKEN_TOKEN"] == "starts-here"


def test_unload_failure_keeps_the_plist(env: dict[str, Any]) -> None:
    install_schedule(**env, launchctl=FakeLaunchctl())

    class RefusingLaunchctl(FakeLaunchctl):
        def __call__(self, args: list[str]) -> tuple[int, str, str]:
            self.calls.append(args)
            return 9, "", "Boot-out failed: 9: Bad file descriptor"

    result = uninstall_schedule(
        config_path=env["config_path"],
        agents_dir=env["agents_dir"],
        launchctl=RefusingLaunchctl(),
    )
    assert not result.ok
    assert result.removed == []
    # A job that will not unload keeps its plist rather than running with
    # no on-disk definition (review finding).
    assert len(list(env["agents_dir"].glob("*.plist"))) == 3
    assert all("unload failed" in line for line in result.lines)


def test_not_loaded_job_uninstalls_cleanly(env: dict[str, Any]) -> None:
    install_schedule(**env, launchctl=FakeLaunchctl())

    class NotLoadedLaunchctl(FakeLaunchctl):
        def __call__(self, args: list[str]) -> tuple[int, str, str]:
            self.calls.append(args)
            return 3, "", "Boot-out failed: 3: No such process"

    result = uninstall_schedule(
        config_path=env["config_path"],
        agents_dir=env["agents_dir"],
        launchctl=NotLoadedLaunchctl(),
    )
    assert result.ok
    assert len(result.removed) == 3


def test_load_failure_is_reported(env: dict[str, Any]) -> None:
    class FailingLaunchctl(FakeLaunchctl):
        def __call__(self, args: list[str]) -> tuple[int, str, str]:
            self.calls.append(args)
            if args and args[0] == "bootstrap":
                return 5, "", "Load failed: 5: Input/output error"
            return 0, "", ""

    result = install_schedule(**env, launchctl=FailingLaunchctl())
    assert len(result.written) == 3
    assert result.loaded == []
    # The failure must reach the caller, not just the printed lines.
    assert not result.ok
    assert len(result.failures) == 3
    assert any("load failed" in line for line in result.lines)
