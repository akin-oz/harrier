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

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from harrier.connection import Connection
from harrier.screening.normalized import normalize, stable_key
from harrier.screening.policy import policy_version
from harrier.screening.rules import CandidateConfig
from harrier.screening.seen import SeenDecision, load_seen, posting_identities, save_seen
from harrier.tracker.store import extract_note_value, list_jobs
from harrier.tracks import Scope

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


def human_rejected_keys(conn: Connection, scope: Scope) -> set[str]:
    """Normalized identities of jobs the operator has rejected.

    Matched on url and on company plus title, the same identities screening
    dedupes by, because a reconsidered posting arriving under a slightly
    different key would defeat the protection entirely.
    """
    protected: set[str] = set()
    for job in list_jobs(conn, scope):
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


def human_rejected_identities(conn: Connection, scope: Scope) -> set[str]:
    """The hashed identities of every row the operator rejected on the
    track, whatever its source, computed as the pipeline computes them for
    a seen entry (`posting_identities`). A posting rejected on one source
    and recorded on another meets its row here, where no key can be rebuilt
    (spec 031, amendment of 2026-10-11)."""
    identities: set[str] = set()
    for job in list_jobs(conn, scope):
        if job.get("status") in HUMAN_DECIDED:
            identities.update(
                posting_identities(job.get("url", ""), job.get("company", ""), job.get("title", ""))
            )
    return identities


def _damaged_labels(board: str) -> set[str]:
    """The forms a board takes when written to notes and read back: cut at
    the first `;` (industry rows), or with `;`, `|` and `=` turned to spaces
    (academic rows). A board with no separator comes back as itself."""
    return {board.split(";")[0].strip(), normalize(re.sub(r"[;|=]", " ", board))}


def human_rejected_seen_keys(conn: Connection, scope: Scope, source: str) -> set[str]:
    """The seen keys discovery recorded for this source's rejected rows.

    Discovery keys a decision by `job_key`, a hash of the source, the board
    and the posting's identity (`harrier.screening.normalized.make_normalized_job`),
    so a seen key never equals a url or a company and title. The row keeps
    what the hash was built from: its source, its `source_label`
    (`source:board`), and its external id or url.

    A label can come back damaged when the board was taken from a company
    name with a note separator in it. Notes end a value at `;`, so the
    industry row's "Smith; Jones LLP" reads back as "smith"; the academic row
    turns `;`, `|` and `=` into spaces before writing (`_clean_note` in
    harrier.screening.pipeline). The company column is stored whole, so when
    the label is one of those damaged forms of the company's board, the
    company's board is tried as well. Only then: an intact label names the
    board, and a company board beside it would protect a different posting.
    An external id is damaged the same way and cannot be recovered from the
    row.
    """
    prefix = f"{source}:"
    keys: set[str] = set()
    for job in list_jobs(conn, scope):
        if job.get("status") not in HUMAN_DECIDED or job.get("source") != source:
            continue
        notes = job.get("notes", "")
        label = job.get("source_label", "") or extract_note_value(notes, "source_label")
        label_board = normalize(label[len(prefix) :]) if label.startswith(prefix) else ""
        company_board = normalize(job.get("company", "") or source)
        if not label_board:
            boards = {company_board}
        elif label_board in _damaged_labels(company_board):
            boards = {label_board, company_board}
        else:
            boards = {label_board}
        external = job.get("external_key", "") or extract_note_value(notes, "external_key")
        external_id = external[len(prefix) :] if external.startswith(prefix) else ""
        identity = (
            external_id
            or (job.get("url", "") or "").strip()
            or stable_key(job.get("company", ""), job.get("title", ""), job.get("location", ""))
        )
        keys.update(stable_key(source, board, identity) for board in boards)
    return keys


def reconsider_source(
    conn: Connection,
    scope: Scope,
    source: str,
    candidate_cfg: CandidateConfig | None,
    *,
    dry_run: bool = True,
    key_identity: Callable[[str], set[str]] | None = None,
    policy: str | None = None,
    track_id: int | None = None,
    protected_keys: frozenset[str] = frozenset(),
    also: tuple[Scope, ...] = (),
) -> ReconsiderReport:
    """Clear rejections recorded under a policy other than the current one.

    Clearing rather than re-screening: the postings themselves are not
    stored, only their keys, so what this can do is make them eligible again.
    The next discovery run fetches and judges them under the current rules,
    which is where the decision belongs.

    `also` names more tracks whose human rejections protect an entry. A
    shared discovery run judges a posting for the industry and the focused
    track at once, so a person's rejection on either keeps it closed (spec
    139).
    """
    # An academic track passes its own version and its own seen state
    # (spec 097); the industry track computes the version from its
    # configuration, as before.
    if policy is not None:
        current = policy
    elif candidate_cfg is not None:
        current = policy_version(candidate_cfg)
    else:
        raise ValueError("reconsider needs a policy or a candidate configuration")
    decisions = load_seen(source, track_id)
    protected: set[str] = set()
    protected_seen: set[str] = set(protected_keys)
    protected_identities: set[str] = set()
    for each in (scope, *also):
        protected |= human_rejected_keys(conn, each)
        protected_seen |= human_rejected_seen_keys(conn, each, source)
        protected_identities |= human_rejected_identities(conn, each)
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
        # A seen key is a hash of a row's identity, so a rejected row is
        # matched by the key it would have been recorded under. A caller's
        # `protected_keys` adds its own derivation (spec 097). An entry's
        # recorded identities reach a row from any source.
        if (
            key in protected_seen
            or identities & protected
            or protected_identities.intersection(decision.identities)
        ):
            # The operator said no. A rule change does not overturn that.
            report.protected += 1
            remaining[key] = decision
            continue
        report.cleared.append(key)

    if not dry_run and report.cleared:
        save_seen(source, remaining, track_id)
    return report
