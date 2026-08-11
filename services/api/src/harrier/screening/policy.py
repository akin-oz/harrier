"""What the screening rules were when a decision was made (spec 031).

A rejection used to be permanent and anonymous. The seen state recorded that
a posting had been looked at and nothing about the rules that looked at it,
so after a rule was corrected there was no way to ask which rejections the
correction would change. Every screening fix was retroactively worthless.

The policy version is that missing fact. It is a digest of everything a
decision depends on: the candidate configuration that reaches the gates and
the scorer, and the rule tables compiled into this module. Change a weight, a
keyword list, or the cutoff, and the version changes; rename a variable or
reword a comment, and it does not.

Deliberately not a hand-maintained number. A version the author has to
remember to bump is a version that is wrong exactly when it matters, which is
after the change nobody thought was significant.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from harrier.screening import rules
from harrier.screening.rules import CandidateConfig, scoring_config

# Entries written before spec 031 carry no version. They read as unknown
# rather than as current, which makes them eligible for the first
# reconsideration instead of invisible to it.
UNKNOWN_POLICY = "unknown"

VERSION_LENGTH = 12

# The configuration paths that change a decision, section by section.
#
# Nested, because the configuration is. The first version of this listed flat
# top-level names that the real file has never contained, so the filter
# dropped every one and the digest reduced to `scoring` plus the compiled
# tables: a change to the title keywords or the preferred countries did not
# move the version, and no stale rejection became reconsiderable. The test
# passed because it inserted a flat key a real configuration does not have
# (review finding on PR #33).
#
# Listed rather than hashing the whole file, so editing a comment or a
# display-only field does not invalidate every stored decision and force a
# needless re-screen. Every path here is asserted to exist against the
# example configuration by
# tests/test_seen_policy.py::test_every_deciding_path_exists_in_the_real_configuration,
# which is what turns this list from a guess into a claim.
DECIDING_PATHS: dict[str, tuple[str, ...]] = {
    "targets": ("titles", "title_keywords_include", "title_keywords_exclude"),
    "candidate": ("preferred_regions", "preferred_countries"),
    "preferences": ("domains_preferred", "domains_secondary"),
}

# Read directly by score_job rather than through scoring_config, so it needs
# naming separately or a change to the domain bonus moves no version.
DOMAIN_BONUS_KEY = "domain_bonus"


def _config_fingerprint(candidate_cfg: CandidateConfig) -> dict[str, Any]:
    deciding: dict[str, Any] = {}
    for section, keys in DECIDING_PATHS.items():
        raw = candidate_cfg.get(section)
        values: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
        deciding[section] = {key: values.get(key) for key in keys if key in values}
    scoring = dict(scoring_config(candidate_cfg))
    raw_scoring = candidate_cfg.get("scoring")
    if isinstance(raw_scoring, dict):
        scoring[DOMAIN_BONUS_KEY] = cast("dict[str, Any]", raw_scoring).get(DOMAIN_BONUS_KEY)
    deciding["scoring"] = scoring
    return deciding


def _rule_fingerprint() -> dict[str, Any]:
    """The tables compiled into the module rather than read from config.

    These are the ones a code change moves, and they decide as much as the
    configuration does: spec 032 corrects two of them.
    """
    # Read through the module rather than bound at import, so the digest
    # reflects the tables as they are when a decision is made. Binding them
    # at import made this untestable and would have hidden a table swapped in
    # at runtime.
    return {
        "excluded_title_hints": sorted(rules.EXCLUDED_TITLE_HINTS),
        "remote_negative_hints": sorted(rules.REMOTE_NEGATIVE_HINTS),
        "region_negative_hints": sorted(rules.REGION_NEGATIVE_HINTS),
        "remote_positive_patterns": list(rules.REMOTE_POSITIVE_PATTERNS),
        "preferred_region_patterns": list(rules.PREFERRED_REGION_PATTERNS),
        "skill_signals": dict(sorted(rules.SKILL_SIGNALS.items())),
        # score_job reads this to award the domain bonus, and that bonus moves
        # a job across the cutoff, so a change to it changes decisions
        # (review finding on PR #33).
        "domain_keywords": {
            key: sorted(value) for key, value in sorted(rules.DOMAIN_KEYWORDS.items())
        },
        "preferred_signal_weights": dict(sorted(rules.PREFERRED_SIGNAL_WEIGHTS.items())),
        "score_cutoff": rules.SCORE_CUTOFF,
    }


def policy_version(candidate_cfg: CandidateConfig) -> str:
    """A short stable digest of everything a screening decision depends on.

    Stable across runs and machines: the payload is sorted and serialized
    deterministically, so two installations with the same configuration agree
    and a stored decision can be compared with a fresh one.
    """
    payload = {"config": _config_fingerprint(candidate_cfg), "rules": _rule_fingerprint()}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:VERSION_LENGTH]
