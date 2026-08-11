"""The score means one thing, and it says which policy produced it (spec 033).

Four defects that reduced to the same thing: the number the whole product
ranks by was not trustworthy. A cutoff that could not reject on the path that
mattered and rejected for the wrong reason on the path where it could. A
rescore that scored against less input than the import had, then overwrote
the real number with the result. Two score fields that different readers
preferred. And a bare integer stored with no record of the weights that
produced it, sorted across months of history.

The tests here are mostly derivations rather than assertions of remembered
numbers. A test that says "the floor is 59" is a second copy of the
arithmetic and goes stale silently; a test that computes the floor from the
configuration fails when the arithmetic moves, which is the point.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from harrier.db import connect
from harrier.screening import rules
from harrier.screening.normalized import make_normalized_job
from harrier.screening.policy import policy_version
from harrier.tracker.schema import MIGRATIONS, NOTE_KEYS, TRACKER_FIELDS
from harrier.tracker.score import SCORE_FIELDS, UNKNOWN_VERSION, score_fields, stored_score


@pytest.fixture
def cfg() -> dict[str, object]:
    """The committed example configuration, not a rigged one.

    The old cutoff test had to zero five values and empty both weight
    dictionaries to manufacture a single rejection. A screening claim proved
    against a configuration nobody runs is not a claim about this product.
    """
    path = Path(__file__).resolve().parents[3] / "config" / "candidate.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _passes_and_scores(
    cfg: dict[str, object], title: str, location: str, description: str = "", signal: str = ""
) -> int | None:
    """The score a posting would receive, or None if a gate rejects it."""
    job = make_normalized_job(
        source="greenhouse",
        company="Example Labs",
        title=title,
        location=location,
        url="https://boards.example.com/example/1",
        description=description,
    )
    if signal:
        job["remote_signal"] = signal
    if not rules.title_allowed(title, cfg):
        return None
    allowed, _ = rules.remote_region_allowed(job, cfg)
    if not allowed:
        return None
    return rules.score_job(job, cfg)[0]


# --- the cutoff, and why it is gone ------------------------------------------


def test_the_arithmetic_floor_is_derived_from_the_rules(cfg: dict[str, object]) -> None:
    """The floor is computed here, not remembered.

    Anything reaching the scorer has passed `title_allowed`, so it matched an
    include keyword, and passed `remote_region_allowed`, which requires the
    same patterns over the same text that the remote bonus rewards. On the ATS
    path the region gate forces the region bonus too.

    If a weight change lifts the ATS floor, or drops it below what a
    reintroduced cutoff would catch, this fails rather than letting the note in
    rules.py quietly go stale.
    """
    targets = cfg["targets"]
    assert isinstance(targets, dict)
    raw_keywords: object = cast("dict[str, object]", targets)["title_keywords_include"]
    assert isinstance(raw_keywords, list)
    keywords = [str(word) for word in cast("list[object]", raw_keywords)]

    ats = [
        _passes_and_scores(cfg, f"{word.title()} Engineer", "Remote, Europe") for word in keywords
    ]
    linkedin = [
        _passes_and_scores(cfg, f"{word.title()} Engineer", "Remote", signal="linkedin_search")
        for word in keywords
    ]
    assert all(score is not None for score in ats), "an include keyword no longer passes the gates"
    assert all(score is not None for score in linkedin)

    scoring = rules.scoring_config(cfg)
    unavoidable = int(scoring["base_score"]) + int(scoring["remote_bonus"])
    assert min(s for s in ats if s is not None) >= unavoidable + int(
        scoring["preferred_region_bonus"]
    ), "the ATS path no longer earns the region bonus unconditionally"

    # The LinkedIn path forfeits the region bonus, because those searches are
    # region-filtered at query level and return early from the gate. Its floor
    # is lower by exactly that bonus, and that gap is the whole reason a single
    # threshold could not be fair to both paths.
    assert min(s for s in linkedin if s is not None) == min(s for s in ats if s is not None) - int(
        scoring["preferred_region_bonus"]
    )


def test_there_is_no_score_cutoff(cfg: dict[str, object]) -> None:
    """Removing it is the spec's conclusion, so this pins the absence.

    Reintroducing a constant here without redoing the derivation above is the
    mistake this guards: any threshold between the two floors rejects LinkedIn
    postings for being correctly region-filtered.
    """
    assert not hasattr(rules, "SCORE_CUTOFF")
    from harrier.screening import pipeline

    assert "low_score" not in Path(pipeline.__file__).read_text(encoding="utf-8").replace(
        "# ", "@ "
    ), "the low-score rejection is back in the pipeline"


def test_a_realistic_posting_is_accepted_without_rigging_the_configuration(
    cfg: dict[str, object],
) -> None:
    score = _passes_and_scores(
        cfg,
        "Senior Frontend Engineer",
        "Remote, Europe",
        "TypeScript and React, remote across Europe, ownership and testing.",
    )
    assert score is not None and score > 0


# --- one score ----------------------------------------------------------------


def test_every_score_field_is_written_together() -> None:
    """The two fields diverged because two call sites each wrote the subset
    they cared about. Every field a reader might take is written at once."""
    written = score_fields(72, ["a", "b"], "abc123")
    assert set(written) == set(SCORE_FIELDS)
    assert written["fit_score"] == written["score"] == "72"


def test_the_score_fields_are_all_real_tracker_columns() -> None:
    """A field written into a row that has no column is silently dropped."""
    columns = set(TRACKER_FIELDS) | set(NOTE_KEYS)
    assert set(SCORE_FIELDS) <= columns


def test_no_reader_takes_a_field_the_writer_does_not_fill() -> None:
    """Enumerated from the tree rather than from memory.

    Every module that reads a score field out of a tracker row is listed here
    with the field it takes. If a reader starts taking a field `score_fields`
    does not write, this fails, which is the drift that produced two numbers
    for one job.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "harrier"
    readers: dict[str, set[str]] = {}
    for path in source.rglob("*.py"):
        if "outreach" in path.parts:
            # Contacts carry their own fit_score, about a person's relevance
            # rather than a job's fit. Same word, different quantity.
            continue
        text = path.read_text(encoding="utf-8")
        for field in ("fit_score", "score", "signals", "scoring_version"):
            if f'get("{field}"' in text or f"get('{field}'" in text:
                readers.setdefault(field, set()).add(path.name)
    assert readers, "no readers found: this test stopped looking rather than passing"
    assert set(readers) <= set(SCORE_FIELDS)


def test_the_queue_and_the_digest_rank_by_the_same_field() -> None:
    """The disagreement in one assertion. `parse_score` took `score` first
    while every other reader took `fit_score`, so a rescore that wrote one and
    not the other made the command line and the nightly digest disagree."""
    from harrier.tracker.queue import parse_score

    row = {"fit_score": "80", "score": "20"}
    assert parse_score(row) == stored_score(row) == 80


# --- the version --------------------------------------------------------------


def test_a_stored_score_carries_the_policy_that_produced_it(
    cfg: dict[str, object],
) -> None:
    written = score_fields(72, [], policy_version(cfg))
    assert written["scoring_version"] == policy_version(cfg)


def test_a_score_written_without_a_version_reads_as_unknown() -> None:
    """Rows that predate this change are not recomputed. Rescoring history
    under today's rules would destroy the record of what was decided at the
    time, so they say `unknown` instead of claiming a policy."""
    assert score_fields(72, [], "")["scoring_version"] == UNKNOWN_VERSION


def test_changing_a_weight_changes_the_version(
    cfg: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = policy_version(cfg)
    monkeypatch.setattr(rules, "DEFAULT_SCORING", {**rules.DEFAULT_SCORING, "remote_bonus": 99})
    assert policy_version(cfg) != before


# --- the schema ---------------------------------------------------------------


def test_a_migrated_database_matches_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two paths into the same table.

    Migration 1 derived its column list from the live NOTE_KEYS, so adding a
    key changed history: a fresh database got the column at migration 1 and
    then failed on the later ALTER, while an existing one worked. Migration 1
    now records the table as it was first created, and this holds the paths
    together.
    """
    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "fresh"))
    fresh = connect()
    fresh_columns = {row[1] for row in fresh.execute("PRAGMA table_info(jobs)")}
    fresh.close()

    # A database stopped at migration 1, then brought forward.
    old = sqlite3.connect(tmp_path / "old.db")
    for version, statements in MIGRATIONS:
        if version > 1:
            continue
        for statement in statements:
            old.execute(statement)
    for version, statements in MIGRATIONS:
        if version == 1:
            continue
        for statement in statements:
            if statement.strip().upper().startswith("ALTER TABLE JOBS"):
                old.execute(statement)
    migrated_columns = {row[1] for row in old.execute("PRAGMA table_info(jobs)")}
    old.close()

    assert "scoring_version" in fresh_columns
    assert migrated_columns == fresh_columns


# --- saturation ---------------------------------------------------------------


def test_two_strong_postings_are_not_tied_by_a_cap(cfg: dict[str, object]) -> None:
    """The cap was `min(score, 120)` and a strong realistic posting reached it
    exactly, so the two best rows in the tracker scored the same. The score is
    read as a ranking, and a ranking that ties at the top is not one."""
    strong = (
        "Senior Frontend Engineer, fully remote across Europe. TypeScript, React, "
        "Next.js and Node. You will own delivery, care about testing, CI/CD, "
        "observability and performance, and have architectural influence in a "
        "strong engineering culture. EU work permit or EU-based contractor "
        "welcome. We build developer tools."
    )
    stronger = strong + " Vue and Nuxt too, full stack, product engineer."
    first = _passes_and_scores(cfg, "Senior Frontend Engineer", "Remote, Europe", strong)
    second = _passes_and_scores(cfg, "Senior Frontend Engineer", "Remote, Europe", stronger)
    assert first is not None and second is not None
    assert second > first, "a strictly better posting scores the same: the cap is back"


# --- enrichment fires on a real path ------------------------------------------


def test_a_manually_added_ats_url_is_enriched_before_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The posting that most needs enrichment, and the one path that never had it.

    Both tests exercising enrichment used a source with no importer, and the
    discovery pipeline enriches while capture did not. A person pasting a job
    URL with no description was scored on the title alone, and left no cached
    description, so `reevaluate` could not repair it later either (spec 033).
    """
    from unittest.mock import patch

    from harrier.capture import add_captured_job
    from harrier.screening import http as screening_http
    from harrier.screening.descriptions import load_cached_description
    from harrier.tracker.store import list_jobs

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    url = "https://job-boards.greenhouse.io/example/jobs/1"
    html = (
        "<html><body><p>Fully remote across Europe. TypeScript, React, testing, "
        "CI/CD, observability, ownership and performance.</p></body></html>"
    )

    with patch.object(screening_http, "request_text", return_value=html):
        result = add_captured_job(
            conn, company="Example Labs", title="Senior Frontend Engineer", url=url
        )
    assert result.status == "added"

    row = next(job for job in list_jobs(conn) if job["url"] == url)

    # What the same posting scores without the fetched description. The
    # comparison that matters is that enrichment moved the number, not merely
    # that something was stored. A second capture cannot serve here: the
    # duplicate check is on company and title, not the URL.
    bare = rules.score_job(
        make_normalized_job(
            source="manual",
            company="Example Labs",
            title="Senior Frontend Engineer",
            location="",
            url=url,
            description="",
        ),
        {},
    )[0]
    assert int(row["fit_score"]) > bare

    # And it reached the cache, so a later rescore has the input the capture had.
    assert "typescript" in load_cached_description(url).lower()
    assert row["scoring_version"] != ""
    conn.close()


def test_capture_can_be_told_not_to_reach_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import patch

    from harrier.capture import add_captured_job
    from harrier.screening import http as screening_http

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    with patch.object(screening_http, "request_text") as fetch:
        add_captured_job(
            conn,
            company="Example Labs",
            title="Senior Frontend Engineer",
            url="https://job-boards.greenhouse.io/example/jobs/2",
            enrich=False,
        )
    assert fetch.call_count == 0
    conn.close()


# --- the run summary describes the run ----------------------------------------
