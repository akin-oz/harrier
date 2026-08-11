"""Re-open rejections made under older rules (spec 031).

The point of recording a policy version is being able to ask this question:
which postings were rejected by a rule that has since changed? Without it a
screening fix only affects postings nobody has seen yet, which for a mature
watchlist is almost none of them.

This clears the recorded decision; it does not re-screen. Only keys are
stored, never the postings, so the most this layer can do is make a posting
eligible again. The next discovery run fetches and judges it under the
current rules, which is where the judgement belongs, and a posting that no
longer exists is simply not fetched.

Not automatic. Clearing after every configuration edit would surprise the
operator with a burst of tracker rows from decisions they thought were
settled, so they ask for it.

Two refusals matter more than the feature:

- **A human decision outranks a rule.** A job the operator rejected in the
  tracker is never resurrected by a policy change. Rules get to change their
  own minds, not somebody else's.
- **An acceptance is not reconsidered.** It already produced a tracker row,
  and re-running it would at best do nothing and at worst duplicate it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

from harrier.screening.normalized import normalize
from harrier.screening.policy import policy_version
from harrier.screening.rules import CandidateConfig
from harrier.screening.seen import SeenDecision, load_seen, save_seen
from harrier.tracker.store import list_jobs

# The statuses that represent a decision by the operator rather than by a
# rule. Reconsideration never touches a posting that matches one of these.
HUMAN_DECIDED = frozenset({"rejected"})


@dataclass
class ReconsiderReport:
    """What a reconsideration found, per source."""

    source: str
    current_policy: str
    examined: int = 0
    stale: int = 0
    cleared: list[str] = field(default_factory=list[str])
    kept: int = 0
    protected: int = 0

    @property
    def changed(self) -> int:
        return len(self.cleared)

    def describe(self) -> str:
        return (
            f"{self.source}: {self.examined} recorded, {self.stale} under older rules, "
            f"{self.changed} now eligible again, {self.kept} still rejected, "
            f"{self.protected} left alone because you rejected them"
        )


def human_rejected_keys(conn: sqlite3.Connection) -> set[str]:
    """Normalized identities of jobs the operator has rejected.

    Matched on url and on company plus title, the same identities screening
    dedupes by, because a reconsidered posting arriving under a slightly
    different key would defeat the protection entirely.
    """
    protected: set[str] = set()
    for job in list_jobs(conn):
        if job.get("status") not in HUMAN_DECIDED:
            continue
        url = normalize(job.get("url", ""))
        if url:
            protected.add(url)
        company = normalize(job.get("company", ""))
        title = normalize(job.get("title", ""))
        if company and title:
            protected.add(f"{company}|{title}")
    return protected


def reconsider_source(
    conn: sqlite3.Connection,
    source: str,
    candidate_cfg: CandidateConfig,
    *,
    dry_run: bool = True,
    key_identity: Callable[[str], set[str]] | None = None,
) -> ReconsiderReport:
    """Clear rejections recorded under a policy other than the current one.

    Clearing rather than re-screening: the postings themselves are not
    stored, only their keys, so what this can do is make them eligible again.
    The next discovery run fetches and judges them under the current rules,
    which is where the decision belongs.
    """
    current = policy_version(candidate_cfg)
    decisions = load_seen(source)
    protected = human_rejected_keys(conn)
    report = ReconsiderReport(source=source, current_policy=current, examined=len(decisions))

    remaining: dict[str, SeenDecision] = {}
    for key, decision in decisions.items():
        if not decision.reconsiderable or decision.policy == current:
            remaining[key] = decision
            if decision.reconsiderable:
                report.kept += 1
            continue
        report.stale += 1
        identities = key_identity(key) if key_identity else {normalize(key)}
        if identities & protected:
            # The operator said no. A rule change does not overturn that.
            report.protected += 1
            remaining[key] = decision
            continue
        report.cleared.append(key)

    if not dry_run and report.cleared:
        save_seen(source, remaining)
    return report
