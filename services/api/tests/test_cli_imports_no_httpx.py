"""The CLI's start loads no dev-only dependency (spec 111, Amendment
(2026-10-15): `.env` never supplies the secret key).

`httpx` is in the dev dependency group only, so the hosted images and the
local container do not have it. `load_project_env` runs before every local
command, the scheduler machine's included. It once imported
`harrier.operator`, which imports `httpx`, to read one variable name, and
every command would then have failed on an image without it. Run in a child
process, so no other test's imports count.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROBE = """
import sys
from pathlib import Path
from harrier_cli.main import load_project_env
before = "httpx" in sys.modules
env = Path(sys.argv[1])
env.write_text("HARRIER_SUPABASE_SECRET_KEY=sb_secret_synthetic\\n", encoding="utf-8")
load_project_env(env)
print(before, "httpx" in sys.modules)
"""


def test_loading_dotenv_imports_no_httpx(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", PROBE, str(tmp_path / "dotenv")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False", "False"]


# The local file seam and the local API, with a publishable key in the
# environment that is not one: a local process neither reads it nor imports
# httpx (spec 107, PR 2's acceptance criteria). Only a hosted process sends
# a Storage request.
LOCAL_SEAM_PROBE = """
import sys
from harrier.filestore import files, load_text, save_text
import harrier_api.app
save_text("answers/probe.md", "local", atomic=False)
assert load_text("answers/probe.md") == "local"
assert files().stat("answers/probe.md") is not None
print("httpx" in sys.modules)
"""


def test_the_local_seam_imports_no_httpx(tmp_path: Path) -> None:
    import os

    environ = {
        **os.environ,
        "HARRIER_DATA_DIR": str(tmp_path / "data"),
        "HARRIER_DATABASE_URL": "",
        "HARRIER_SUPABASE_PUBLISHABLE_KEY": "not a key the local API would accept",
    }
    environ.pop("HARRIER_AUTH", None)
    done = subprocess.run(
        [sys.executable, "-c", LOCAL_SEAM_PROBE],
        capture_output=True,
        text=True,
        check=False,
        env=environ,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["False"]
    assert (tmp_path / "data" / "answers" / "probe.md").read_text() == "local"
