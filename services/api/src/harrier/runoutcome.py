"""Whether a run worked, and when each job last did (spec 029).

This project exists because a scheduled job can fail silently.
The rewrite reproduced the mechanism: discovery ended in a literal `return 0`,
every upstream failure was absorbed into the summary rather than the exit
status, and the only notification was gated on having found something. A run
where every board answered 404 exited zero and said nothing.

Two ideas here, and they are separate on purpose.

`classify_run` answers "did this run work", from the summary the run already
produced. Its threshold is total failure, not any failure: a day where one
board is down and four are fine is a normal day, and an exit status that
fails a run the operator would call fine gets ignored within a week.

`record_success` and `last_success` answer "when did this job last work",
which is the only thing that catches a job that stopped running altogether.
A job that fails loudly is visible in its exit status. A job that hangs, or
that launchd stopped starting, produces no status at all, and the only
evidence is the absence of a recent success.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

# Exit codes. 0 and 1 are the shell's; 2 is already taken by the CLI for
# usage errors, so a failed run is 3 and cannot be confused with a bad flag.
EXIT_OK = 0
EXIT_RUN_FAILED = 3

DISCOVERY_JOB = "discovery"
DIGEST_JOB = "digest"
MAIL_WATCH_JOB = "mail-watch"

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class RunOutcome:
    """What a run did, in the terms the exit status is decided by."""

    attempted: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def succeeded(self) -> tuple[str, ...]:
        return tuple(name for name in self.attempted if name not in set(self.failed))

    @property
    def total_failure(self) -> bool:
        """Every source that was tried failed, or none was tried.

        Both are the shape of a broken installation rather than a quiet week.
        Nothing attempted usually means nothing is configured or every source
        was skipped, and a scheduled job that runs nothing every four hours
        indefinitely is the exact outage this spec exists for.
        """
        return not self.attempted or len(self.failed) == len(self.attempted)

    @property
    def exit_code(self) -> int:
        return EXIT_RUN_FAILED if self.total_failure else EXIT_OK

    def describe(self) -> str:
        if not self.attempted:
            reason = "no source was attempted"
            if self.skipped:
                reason += f" ({len(self.skipped)} skipped: {', '.join(self.skipped)})"
            return reason
        if self.failed and self.total_failure:
            return f"every attempted source failed: {', '.join(self.failed)}"
        if self.failed:
            return (
                f"{len(self.succeeded)} of {len(self.attempted)} sources ran; "
                f"failed: {', '.join(self.failed)}"
            )
        return f"{len(self.attempted)} of {len(self.attempted)} sources ran"


def source_failed(summary: dict[str, object]) -> bool:
    """Whether this source produced nothing because it could not.

    Deliberately not "did anything go wrong". A board that 404s while its
    four siblings answer is a dead board, which is spec 025's problem, not a
    failed run. The source failed only when it raised, or when every board it
    was given errored.
    """
    errors = summary.get("errors")
    if isinstance(errors, list) and errors:
        return True
    board_errors = summary.get("board_errors")
    board_urls = summary.get("board_urls")
    if isinstance(board_errors, list) and isinstance(board_urls, list) and board_urls:
        return len(cast("list[object]", board_errors)) >= len(cast("list[object]", board_urls))
    return False


def classify_run(aggregate: dict[str, object]) -> RunOutcome:
    """Read the run's own summary. No second source of truth."""
    raw = aggregate.get("source_summaries")
    summaries = cast("list[dict[str, object]]", raw) if isinstance(raw, list) else []
    attempted: list[str] = []
    failed: list[str] = []
    for summary in summaries:
        name = str(summary.get("source", "")) or "unknown"
        attempted.append(name)
        if source_failed(summary):
            failed.append(name)
    raw_skipped = aggregate.get("skipped_sources")
    skipped = [
        str(cast("dict[str, object]", entry).get("source", ""))
        for entry in cast("list[object]", raw_skipped or [])
        if isinstance(entry, dict)
    ]
    return RunOutcome(tuple(attempted), tuple(failed), tuple(skipped))


# --- when each scheduled job last worked -----------------------------------


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def record_success(conn: sqlite3.Connection, job: str, *, at: str | None = None) -> None:
    """Written on the success path only.

    A failing run must leave the previous value alone: the number the digest
    reports is the age of the last time this job *worked*, and touching it on
    failure would make a job that has been broken for a month look fresh.
    """
    conn.execute(
        """
        INSERT INTO job_runs (job, last_success_at) VALUES (?, ?)
        ON CONFLICT(job) DO UPDATE SET last_success_at = excluded.last_success_at
        """,
        (job, at or now_iso()),
    )
    conn.commit()


def last_success(conn: sqlite3.Connection, job: str) -> str | None:
    row = conn.execute("SELECT last_success_at FROM job_runs WHERE job = ?", (job,)).fetchone()
    return str(row[0]) if row and row[0] else None


def all_last_success(conn: sqlite3.Connection) -> dict[str, str]:
    rows: list[Any] = conn.execute("SELECT job, last_success_at FROM job_runs").fetchall()
    return {str(row[0]): str(row[1]) for row in rows if row[1]}


def age_in_days(timestamp: str, *, now: datetime | None = None) -> int | None:
    """Whole days since that moment, or None if it cannot be read.

    Unreadable returns None rather than 0, because 0 reads as "succeeded
    today" and a corrupt timestamp must never look like a healthy job.
    """
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, ((now or datetime.now(UTC)) - moment).days)


def describe_age(job: str, timestamp: str | None, *, now: datetime | None = None) -> str:
    """One line per job, phrased so an outage cannot be misread.

    "Discovery last succeeded 61 days ago" is unambiguous. The number this
    replaces, a count of new prospects, reads identically on a quiet Tuesday
    and in the second month of a silent failure.
    """
    if timestamp is None:
        return f"{job}: has never recorded a success"
    days = age_in_days(timestamp, now=now)
    if days is None:
        return f"{job}: last success time is unreadable ({timestamp})"
    if days == 0:
        return f"{job}: last succeeded today"
    return f"{job}: last succeeded {days} day{'s' if days != 1 else ''} ago"
