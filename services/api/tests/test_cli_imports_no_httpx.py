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
