"""The hosted schedule (spec 108).

On the hosted store an owner chooses which scheduled jobs run for them and
in which timezone, as the `schedule` kind of their configuration. Every
owner shares the committed cadence in `config/schedule.json`; the local
product keeps launchd and `harrier schedule` (ADR-006).

`HOSTED_JOBS` names the jobs a hosted owner may choose. `digest` joins when
spec 109 gives owners their own Telegram settings, and `gmail-watch` when a
hosted Gmail spec exists.
"""

from __future__ import annotations

HOSTED_JOBS: tuple[str, ...] = ("discovery", "academic-discovery")
