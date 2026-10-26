"""`HARRIER_TEST_SHARD` splits the suite into shards CI runs as jobs (spec 168).

Each test runs pytest's collection in a child process over a few real test
files, with the shard variable set as the case needs, so the hook in
`tests/conftest.py` decides exactly as it does in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[1]
SHARD_VARIABLE = "HARRIER_TEST_SHARD"
# Files with enough tests that three shards each get several, and nothing
# collected from them needs a server.
TARGETS = ("tests/test_spec_structure.py", "tests/test_spec_gate.py")


def _collect(shard: str | None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # A CI shard runs this test with its own shard set; the child decides
    # only on the value given here.
    env.pop(SHARD_VARIABLE, None)
    if shard is not None:
        env[SHARD_VARIABLE] = shard
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *TARGETS,
        ],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _collected(shard: str | None) -> list[str]:
    result = _collect(shard)
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if "::" in line]


def test_shards_partition_the_collection():
    everything = _collected(None)
    shards = [_collected(f"{index}/3") for index in (1, 2, 3)]

    assert len(everything) >= 3
    assert sum(len(shard) for shard in shards) == len(everything)
    assert sorted(test for shard in shards for test in shard) == sorted(everything)


def test_shards_differ_in_size_by_at_most_one():
    sizes = [len(_collected(f"{index}/4")) for index in (1, 2, 3, 4)]

    assert min(sizes) >= 1
    assert max(sizes) - min(sizes) <= 1


def test_an_empty_shard_runs_everything():
    assert _collected("") == _collected(None)


@pytest.mark.parametrize("value", ["0/4", "5/4", "2/0", "x", "1/", "1/4/2"])
def test_a_malformed_shard_is_a_usage_error(value: str):
    result = _collect(value)

    assert result.returncode == pytest.ExitCode.USAGE_ERROR
    assert SHARD_VARIABLE in result.stderr
    assert repr(value) in result.stderr
    assert "::" not in result.stdout
