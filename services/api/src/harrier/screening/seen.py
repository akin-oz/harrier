"""Per-source seen state: what was decided, and under which rules (spec 031).

Originally this stored a set of keys and a timestamp. A posting that had been
looked at was suppressed forever, and nothing recorded why, so after a rule
was corrected there was no way to ask which rejections the correction would
change. Every screening fix was retroactively worthless, which matters
because spec 032 is a set of screening fixes.

Now each key carries the decision: the verdict, the gate that produced it,
the policy version in force at the time, and when. That is what makes
reconsideration possible, and it is the whole difference between a dedupe
cache and a record.

Two properties the old format did not have:

- **A posting is recorded after it is judged, not before.** The pipeline used
  to add the key on sight, so a posting suppressed before any gate ran could
  never be judged later.
- **Eviction is by age.** The cap used to keep the lexicographically largest
  keys, which is stable, so the same entries were evicted on every run
  forever while genuinely stale ones were retained.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from harrier.db import data_dir
from harrier.screening.policy import UNKNOWN_POLICY

logger = logging.getLogger(__name__)

SEEN_CAP = 10_000

ACCEPTED = "accepted"
REJECTED = "rejected"
# Entries migrated from the pre-spec-031 format. The verdict was not
# recorded, so it is not invented: unknown is eligible for reconsideration
# and, critically, is never treated as an acceptance.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class SeenDecision:
    """One posting, and what screening concluded about it."""

    verdict: str
    reason: str
    policy: str
    at: str

    def as_json(self) -> dict[str, str]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "policy": self.policy,
            "at": self.at,
        }

    @property
    def reconsiderable(self) -> bool:
        """Only rejections are worth re-running, and only they are safe to.

        An acceptance already produced a tracker row; re-running it would at
        best do nothing and at worst duplicate the row.
        """
        return self.verdict in (REJECTED, UNKNOWN)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _state_path(source_name: str) -> Path:
    return data_dir() / "discovery" / f"{source_name}_seen.json"


def _decision_from(raw: object, fallback_at: str) -> SeenDecision | None:
    if not isinstance(raw, dict):
        return None
    entry = cast("dict[str, Any]", raw)
    return SeenDecision(
        verdict=str(entry.get("verdict", UNKNOWN)) or UNKNOWN,
        reason=str(entry.get("reason", "")),
        policy=str(entry.get("policy", UNKNOWN_POLICY)) or UNKNOWN_POLICY,
        at=str(entry.get("at", fallback_at)) or fallback_at,
    )


def load_seen(source_name: str) -> dict[str, SeenDecision]:
    """Every recorded decision for this source.

    Reads the pre-spec-031 format too. Those entries become unknown-verdict,
    unknown-policy decisions rather than being discarded: discarding them
    would re-offer every posting the operator has already rejected, and
    treating them as current-policy rejections would hide them from the first
    reconsideration, which is the one that matters.
    """
    path = _state_path(source_name)
    if not path.is_file():
        return {}
    try:
        parsed: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        # Loudly, because the consequence is invisible otherwise: the next run
        # treats every previously rejected posting as new and re-offers all of
        # them, and the save that follows overwrites the damaged file so the
        # original is unrecoverable (review finding on PR #33). Refusing
        # outright belongs with the atomic-write work in spec 040; this at
        # least tells the operator why a source suddenly produced a flood.
        logger.warning(
            "seen state for %s could not be read (%s); every posting will look new",
            source_name,
            error,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "seen state for %s is not an object; every posting will look new", source_name
        )
        return {}
    record = cast("dict[str, object]", parsed)
    fallback_at = str(record.get("updated_at", "")) or now_iso()

    decisions_raw = record.get("decisions")
    if isinstance(decisions_raw, dict):
        decisions: dict[str, SeenDecision] = {}
        for key, value in cast("dict[str, object]", decisions_raw).items():
            decision = _decision_from(value, fallback_at)
            if decision is not None:
                decisions[str(key)] = decision
        return decisions

    # The old shape: a bare list of keys with one timestamp for all of them.
    raw_keys = record.get("seen_keys")
    if not isinstance(raw_keys, list):
        return {}
    return {
        str(key): SeenDecision(UNKNOWN, "", UNKNOWN_POLICY, fallback_at)
        for key in cast("list[object]", raw_keys)
    }


def save_seen(source_name: str, decisions: dict[str, SeenDecision]) -> None:
    """Persist, capped at SEEN_CAP by age.

    Newest kept. The previous rule sorted the keys and kept the tail, which
    is stable across runs, so once the cap was reached the same entries were
    dropped every time while older ones sat there permanently.
    """
    path = _state_path(source_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    newest = sorted(decisions.items(), key=lambda item: item[1].at, reverse=True)[:SEEN_CAP]
    payload = {
        "decisions": {key: decision.as_json() for key, decision in newest},
        "updated_at": now_iso(),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_seen_keys(source_name: str) -> set[str]:
    """Membership only, for callers that just need the dedupe question."""
    return set(load_seen(source_name))
