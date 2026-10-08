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
import re
import sqlite3
from pathlib import Path
from typing import cast

import pytest

from harrier.db import connect
from harrier.screening import rules
from harrier.screening.config import load_candidate_config
from harrier.screening.normalized import make_normalized_job
from harrier.screening.policy import policy_version
from harrier.tracker.schema import MIGRATIONS, NOTE_KEYS, TRACKER_FIELDS
from harrier.tracker.score import SCORE_FIELDS, UNKNOWN_VERSION, score_fields, stored_score
from harrier.tracks import default_scope


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

    # The LinkedIn path forfeits only the region bonus, because those searches
    # are region-scoped at query level and skip the preferred-region text
    # requirement. Since spec 053 it shares the remote-text requirement with
    # every other path (the query-level remote filter no longer exists), so
    # the remote bonus is forced there too and the floors differ by exactly
    # the region bonus. That gap is the whole reason a single threshold could
    # not be fair to both paths.
    assert min(s for s in linkedin if s is not None) == min(s for s in ats if s is not None) - int(
        scoring["preferred_region_bonus"]
    )

    # The derivation above holds only because the LinkedIn gate now demands
    # the remote text it used to assume: a bare-city LinkedIn posting with no
    # remote wording is rejected, not scored (spec 053).
    assert (
        _passes_and_scores(
            cfg, f"{keywords[0].title()} Engineer", "Berlin, Germany", signal="linkedin_search"
        )
        is None
    ), "the linkedin gate accepted a posting with no remote evidence"


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

    The first version of this searched only for the four names already in
    SCORE_FIELDS, so the set it built was a subset by construction and the
    assertion could not fail: a guard shaped like a check (review finding on
    PR #42). It now matches any score-shaped field read off a row, so a
    reader of a fifth field is something it can actually find.
    """
    # Score-shaped fields only. A bare `\w*version` also matched the cover
    # letter's short_version and full_version, which are letter variants and
    # have nothing to do with a tracker row.
    pattern = re.compile(r"""\.get\(\s*["'](\w*score\w*|signals|scoring_\w+)["']""")
    source = Path(__file__).resolve().parents[1] / "src" / "harrier"
    found: set[str] = set()
    for path in source.rglob("*.py"):
        if "outreach" in path.parts:
            # Contacts carry their own fit_score, about a person's relevance
            # rather than a job's fit. Same word, different quantity.
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    assert found, "the search found nothing: it stopped looking rather than passing"
    assert found <= set(SCORE_FIELDS), (
        f"a reader takes a field score_fields does not write: {found - set(SCORE_FIELDS)}"
    )


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


def test_a_row_written_before_versions_reads_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real pre-migration row, not a constructed one.

    Migration 3 defaults the column to the empty string, and `score_fields`
    only substitutes `unknown` on write, so a row that predates this change
    read as blank. The spec promises those rows say `unknown`, and testing
    the writer alone could not see that they did not (review finding on
    PR #42).
    """
    from harrier.tracker.score import stored_version
    from harrier.tracker.store import add_job, get_job

    monkeypatch.setenv("HARRIER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    conn = connect()
    job_id = add_job(
        conn,
        {
            "company": "Example Labs",
            "title": "Senior Frontend Engineer",
            "url": "https://boards.example.com/example/9",
            "fit_score": "70",
        },
        scope=default_scope(conn),
    )
    row = get_job(conn, default_scope(conn), job_id)
    assert row["scoring_version"] == "", "the column no longer defaults blank"
    assert stored_version(row) == UNKNOWN_VERSION
    conn.close()


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

    # And the newest migration on top of a database that already exists: one
    # stopped at the migration before it, brought forward by the real runner,
    # ends with exactly the schema a fresh one has. Spec 079's `job_events`
    # is the migration this was extended for.
    latest = MIGRATIONS[-1][0]
    stopped = sqlite3.connect(tmp_path / "stopped.db")
    stopped.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
    for version, statements in MIGRATIONS:
        if version >= latest:
            continue
        for statement in statements:
            stopped.execute(statement)
        stopped.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
    stopped.commit()
    stopped.close()

    def schema(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        return sorted((row[0], row[1], " ".join(str(row[2]).split())) for row in rows)

    brought_forward = connect(tmp_path / "stopped.db")
    fresh = connect()
    assert any(name == "job_events" for _, name, _ in schema(fresh))
    assert any(name == "tracks" for _, name, _ in schema(fresh))
    assert schema(brought_forward) == schema(fresh)
    brought_forward.close()
    fresh.close()


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
            conn,
            default_scope(conn),
            company="Example Labs",
            title="Senior Frontend Engineer",
            url=url,
        )
    assert result.status == "added"

    row = next(job for job in list_jobs(conn, default_scope(conn)) if job["url"] == url)

    # What the same posting scores without the fetched description. The
    # comparison that matters is that enrichment moved the number, not merely
    # that something was stored. A second capture cannot serve here: the
    # duplicate check is on company and title, not the URL.
    # The same configuration the capture loaded. Scoring the baseline against
    # `{}` compared two things at once, so the assertion could have passed on
    # a configuration difference rather than on the enrichment (review finding
    # on PR #42).
    bare = rules.score_job(
        make_normalized_job(
            source="manual",
            company="Example Labs",
            title="Senior Frontend Engineer",
            location="",
            url=url,
            description="",
        ),
        load_candidate_config(conn),
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
            default_scope(conn),
            company="Example Labs",
            title="Senior Frontend Engineer",
            url="https://job-boards.greenhouse.io/example/jobs/2",
            enrich=False,
        )
    assert fetch.call_count == 0
    conn.close()


# --- the run summary describes the run ----------------------------------------


# --- blocked postings rank last (spec 078) -------------------------------------


def _job(title: str, location: str, description: str):
    return make_normalized_job(
        source="greenhouse",
        company="Example Labs",
        title=title,
        location=location,
        url="https://boards.example.com/example/2",
        description=description,
    )


_SKILLS = "TypeScript, React, testing, ownership, remote."


def test_a_us_only_w2_posting_ranks_below_an_emea_remote_one(cfg: dict[str, object]) -> None:
    """The 151 case, rebuilt from invented text: "anywhere in the US" matched
    the preferred-region pattern and the score only ever added."""
    us_only = _job(
        "Senior Frontend Engineer",
        "Remote",
        f"Open to candidates anywhere in the US. W-2 position, no visa sponsorship. {_SKILLS}",
    )
    emea = _job("Senior Frontend Engineer", "Remote, Europe", f"Remote across Europe. {_SKILLS}")

    us_score, us_reasons = rules.score_job(us_only, cfg)
    emea_score, emea_reasons = rules.score_job(emea, cfg)

    assert us_score < emea_score
    assert 'blocker=us_scope "anywhere in the us"' in us_reasons
    assert 'blocker=employment "w-2"' in us_reasons
    assert not [reason for reason in emea_reasons if reason.startswith("blocker=")]


@pytest.mark.parametrize("include_cap", [None, 100])
def test_the_blocker_penalty_is_derived_from_the_rules(
    cfg: dict[str, object], include_cap: int | None
) -> None:
    """Computed from `score_bounds`, not restated. The strongest posting this
    configuration allows proves the upper bound is reachable, and blocking it
    with one phrase must still land it below the weakest posting the gates let
    through. A cap above what the keywords can earn shows the bound counts
    the include bonus as `score_job` does, not as the bare cap."""
    if include_cap is not None:
        cfg = json.loads(json.dumps(cfg))
        cast("dict[str, object]", cfg["scoring"])["include_keyword_bonus_cap"] = include_cap
    low, high = rules.score_bounds(cfg)
    assert rules.blocker_penalty(cfg) == high - low + 1

    scoring = rules.scoring_config(cfg)
    targets = cast("dict[str, list[str]]", cfg["targets"])
    every_signal = " ".join(
        [
            *cast("dict[str, int]", scoring["skill_signals"]),
            *cast("dict[str, int]", scoring["preferred_signal_weights"]),
            *targets["title_keywords_include"],
            "developer tools",
            "remote across europe",
        ]
    )
    strongest = _job(targets["titles"][0], "Remote, Europe", every_signal)
    assert rules.score_job(strongest, cfg)[0] == high, "the upper bound is not reachable"

    blocked = _job(targets["titles"][0], "Remote, Europe", f"{every_signal} W-2 only.")
    blocked_score = rules.score_job(blocked, cfg)[0]
    assert blocked_score < low

    keyword = targets["title_keywords_include"][0]
    weakest = _passes_and_scores(
        cfg, f"{keyword.title()} Engineer", "Remote", signal="linkedin_search"
    )
    assert weakest is not None and weakest >= low
    assert blocked_score < weakest


def test_blockers_do_not_stack(cfg: dict[str, object]) -> None:
    one = _job("Frontend Engineer", "Remote", f"W-2 role. {_SKILLS}")
    two = _job("Frontend Engineer", "Remote", f"W-2 role, US-only. {_SKILLS}")
    unblocked = _job("Frontend Engineer", "Remote", f"Contract role. {_SKILLS}")

    one_score, _ = rules.score_job(one, cfg)
    two_score, two_reasons = rules.score_job(two, cfg)
    base = rules.score_job(unblocked, cfg)[0]

    assert len([r for r in two_reasons if r.startswith("blocker=")]) == 2
    assert base - one_score == base - two_score == rules.blocker_penalty(cfg)


def test_an_explicit_emea_location_overrides_us_scope() -> None:
    description = "We also hire anywhere in the US. W-2 available for US hires."
    in_europe = rules.blockers(_job("Frontend Engineer", "Remote, Europe", description))
    unscoped = rules.blockers(_job("Frontend Engineer", "Remote", description))
    reach_only = rules.blockers(_job("Frontend Engineer", "Remote, Worldwide", description))

    assert [kind for kind, _ in in_europe] == ["employment"], "W-2 is never overridden"
    assert [kind for kind, _ in unscoped] == ["us_scope", "employment"]
    # "Worldwide" is a reach, not a region: a US-only posting can say it too.
    assert [kind for kind, _ in reach_only] == ["us_scope", "employment"]


@pytest.mark.parametrize(
    "description",
    [
        "Must be based in the EU.",
        "EU work permit required.",
        "We work with an EU-based contractor entity.",
        "Must reside in Europe; right to work in the EU.",
    ],
)
def test_eu_permit_phrases_are_never_blockers(
    description: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The product invariant: these are positive signals, never filters, and
    a penalty that sinks a posting to the bottom is a filter in all but name.

    No table entry today matches inside these phrases, so a planted one does:
    the phrases must be gone before any table reads the text, whatever the
    tables come to hold."""
    planted = (*rules.EMPLOYMENT_BLOCKER_PATTERNS, r"\beu\b")
    monkeypatch.setattr(rules, "EMPLOYMENT_BLOCKER_PATTERNS", planted)
    assert rules.blockers(_job("Frontend Engineer", "Remote", description)) == []


@pytest.mark.parametrize(
    "location",
    [
        "Remote, Worldwide",
        "Remote (Global)",
        "Anywhere",
        "Remote-first",
        "Remote, UTC",
        "Remote, GMT",
    ],
)
def test_a_reach_is_not_a_region(location: str) -> None:
    """One location per ambiguous word: each leaves US scope standing, because
    a US-only posting can say any of them (spec 078)."""
    found = rules.blockers(_job("Frontend Engineer", location, "We hire anywhere in the US."))
    assert [kind for kind, _ in found] == ["us_scope"]


def test_every_ambiguous_word_is_a_region_pattern() -> None:
    """An ambiguous entry that is not a region pattern excludes nothing, so a
    typo there fails open: the word it meant would vouch for a location."""
    assert set(rules.PREFERRED_REGION_PATTERNS) >= rules.AMBIGUOUS_REGION_PATTERNS


@pytest.mark.parametrize(
    ("location", "description"),
    [
        ("Remote", "We sponsor visas for the right people."),
        ("Remote, Europe", "Unlike US-only roles, this one is open across Europe."),
        ("Remote", "A campus-based team."),
        ("Remote", "We build AcmeW2Go, a payroll tool."),
        ("Remote", "Opt in to our newsletter. F1 fans welcome."),
        ("Remote", "Join us only if you love TypeScript."),
        ("Remote, Europe", "Our team is based in the US and Spain."),
        ("Remote", "E-Verify applies to our U.S. based roles only."),
        (
            "Remote, Spain",
            "We cannot offer visa sponsorship; you need the right to work in the EU.",
        ),
        ("Remote", "Unfortunately we are unable to provide visa sponsorship."),
    ],
)
def test_blocker_tables_do_not_fire_on_eligible_postings(location: str, description: str) -> None:
    assert rules.blockers(_job("Frontend Engineer", location, description)) == []


# --- spec 078 amendment: review of the merged range ------------------------------


@pytest.mark.parametrize(
    "description",
    [
        "Must be based in the US or the EU.",
        "Eligible to work in the US or Europe.",
        "Open to candidates anywhere in the US or EMEA.",
        "Must be based in the US/EU.",
        "Must be based in the US and the EU.",
        # Spellings the first version of the rule missed.
        "Must be based in the U.S. or the EU.",
        "Remote anywhere in the US, Canada, or Europe.",
        "Must be based in the US, or the EU.",
        "Must be based in the US or in Europe.",
        "Must be based in the US or the EU or Canada.",
        "Must be based in the US or the EU, and speak fluent English.",
    ],
)
def test_a_us_phrase_with_an_emea_alternative_is_not_a_blocker(description: str) -> None:
    """A posting open to the US or to Europe is open to Europe. Flooring it
    buried a job the candidate can take (spec 078 amendment)."""
    assert rules.blockers(_job("Frontend Engineer", "Remote", f"{description} {_SKILLS}")) == []


@pytest.mark.parametrize(
    "description",
    ["Must be based in the US or Canada.", "Anywhere in the US or anywhere else we choose."],
)
def test_a_non_emea_alternative_still_blocks(description: str) -> None:
    found = rules.blockers(_job("Frontend Engineer", "Remote", f"{description} {_SKILLS}"))
    assert [kind for kind, _ in found] == ["us_scope"]


@pytest.mark.parametrize(
    "description",
    [
        "Candidates must be authorized to work in the US, EU candidates are not eligible.",
        "This role is US-only, EMEA applicants please see our other openings.",
        "US time zones only, CET overlap is not possible.",
        "Must be located in the US and EU applicants will not be considered.",
    ],
)
def test_a_region_opening_the_next_clause_offers_nothing(description: str) -> None:
    """A US-only posting that goes on to mention Europe is still US-only. The
    first version of the alternative rule read the next word alone, so each
    of these escaped the floor and ranked with the postings open to Europe
    (spec 078 amendment)."""
    found = rules.blockers(_job("Frontend Engineer", "Remote", f"{description} {_SKILLS}"))
    assert [kind for kind, _ in found] == ["us_scope"]


def test_an_eu_permit_location_names_emea() -> None:
    """ "Must be based in the EU" names the region it permits; the location
    override reads the location as written."""
    location = "Remote (must be based in the EU)"
    assert rules.location_names_explicit_emea(location)
    assert (
        rules.blockers(
            _job("Frontend Engineer", location, f"We also hire anywhere in the US. {_SKILLS}")
        )
        == []
    )


def test_a_gmt_offset_is_not_a_region() -> None:
    """GMT-5 is the US east coast: a band, not an EMEA location."""
    found = rules.blockers(
        _job("Frontend Engineer", "Remote (GMT-5)", f"Anywhere in the US. {_SKILLS}")
    )
    assert [kind for kind, _ in found] == ["us_scope"]


def test_the_floor_holds_for_a_remote_only_board_posting(cfg: dict[str, object]) -> None:
    """A remote-only board passes the gates with no remote text, so it never
    earns the remote bonus. The floor's lower bound assumed it did, and the
    strongest blocked posting outranked such a job."""
    with_keyword = json.loads(json.dumps(cfg))
    cast("dict[str, list[str]]", with_keyword["targets"])["title_keywords_include"].append(
        "javascript"
    )
    weak = _job("JavaScript Engineer", "Germany", "Build our web app.")
    weak["remote_signal"] = "remote_only_board"
    assert rules.remote_region_allowed(weak, with_keyword)[0]
    low, _ = rules.score_bounds(with_keyword)
    assert rules.score_job(weak, with_keyword)[0] >= low

    targets = cast("dict[str, list[str]]", with_keyword["targets"])
    scoring = rules.scoring_config(with_keyword)
    every_signal = " ".join(
        [
            *cast("dict[str, int]", scoring["skill_signals"]),
            *cast("dict[str, int]", scoring["preferred_signal_weights"]),
            *targets["title_keywords_include"],
            "developer tools",
            "remote across europe",
        ]
    )
    blocked = _job(targets["titles"][0], "Remote, Europe", f"{every_signal} W-2 only.")
    assert rules.score_job(blocked, with_keyword)[0] < rules.score_job(weak, with_keyword)[0]


@pytest.mark.parametrize(
    "negative",
    [
        "preferred_region_bonus",
        "exact_title_bonus",
        "include_keyword_bonus",
        "domain bonus",
        "signal weight",
    ],
)
def test_the_floor_holds_with_a_negative_contribution(
    cfg: dict[str, object], negative: str
) -> None:
    """A configuration may set any bonus or signal weight below zero, and the
    lower bound counts each. Large enough that the posting's other bonuses
    cannot cover it, so a bound that left one out would sit above a posting
    that earns it."""
    penalized = json.loads(json.dumps(cfg))
    scoring = cast("dict[str, object]", penalized["scoring"])
    if negative == "signal weight":
        scoring["skill_signals"] = {"europe": -200}
    elif negative == "domain bonus":
        scoring["domain_bonus"] = {"primary": -200, "secondary": 3}
    else:
        scoring[negative] = -200
    low, high = rules.score_bounds(penalized)
    # Earns every bonus: an exact title with an include keyword, remote,
    # region, and a preferred domain.
    text = "Remote across Europe. We build developer tools."
    eligible = _job("Senior Frontend Engineer", "Remote, Europe", text)
    assert rules.score_job(eligible, penalized)[0] >= low
    blocked = _job("Senior Frontend Engineer", "Remote, Europe", f"{text} W-2 only.")
    assert rules.score_job(blocked, penalized)[0] < low
    assert rules.blocker_penalty(penalized) == high - low + 1


# --- US payroll benefits are a blocker (spec 088) ----------------------------------

_BENEFITS = (
    "Benefits\n"
    "- Remote work environment\n"
    "- Subsidized medical, dental, and vision insurance\n"
    "- Short- and long-term disability coverage\n"
    "- 401(k) plan\n"
    "- Company retreats\n"
)


def _kinds(location: str, description: str) -> list[str]:
    return [kind for kind, _ in rules.blockers(_job("Frontend Engineer", location, description))]


def test_a_401k_posting_relayed_as_worldwide_ranks_last(cfg: dict[str, object]) -> None:
    """The case behind spec 088, rebuilt from invented text: an aggregator
    relays a US consultancy's posting as worldwide and remote, and the only
    sign that it is US payroll is the benefit list."""
    with_plan = _job("Senior Frontend Engineer", "Worldwide", f"{_SKILLS}\n{_BENEFITS}")
    no_plan = _BENEFITS.replace("- 401(k) plan\n", "")
    without = _job("Senior Frontend Engineer", "Worldwide", f"{_SKILLS}\n{no_plan}")

    with_score, with_reasons = rules.score_job(with_plan, cfg)
    without_score, without_reasons = rules.score_job(without, cfg)

    assert with_score < without_score
    assert 'blocker=us_payroll "401(k)"' in with_reasons
    assert not [reason for reason in without_reasons if reason.startswith("blocker=")]


@pytest.mark.parametrize(
    "benefit",
    [
        "401(k) plan",
        "401k with a match",
        "401 k matching",
        "401 (k) plan",
        "403(b) plan",
        "403b plan",
        "403 b plan",
        "403 (b) plan",
        "Health savings account",
        "Health savings accounts with an employer contribution",
    ],
)
def test_every_us_payroll_spelling_fires(benefit: str) -> None:
    assert _kinds("Remote", f"{_SKILLS}\nBenefits:\n- {benefit}\n") == ["us_payroll"]


def test_an_explicit_emea_location_overrides_us_payroll() -> None:
    description = f"{_SKILLS}\n{_BENEFITS}"
    assert _kinds("Remote, EMEA", description) == []
    assert _kinds("Remote, Europe", description) == []
    assert _kinds("Worldwide", description) == ["us_payroll"]
    assert _kinds("Remote", description) == ["us_payroll"]


@pytest.mark.parametrize(
    "benefit",
    [
        "401k (US employees)",
        "401(k) for US-based staff",
        "US: 401(k) match",
        "401(k) for U.S. employees, pension elsewhere",
        "Health savings account (United States staff)",
    ],
)
def test_a_us_qualified_benefit_is_not_a_blocker(benefit: str) -> None:
    """A benefit labelled as US staff's says other regions exist."""
    assert _kinds("Remote", f"{_SKILLS}\nBenefits:\n- Stock options\n- {benefit}\n") == []


@pytest.mark.parametrize(
    "description",
    [
        "Offices in the US.\n- 401(k) plan\n",
        "We are a US consultancy; 401(k) plan included.",
        "Our clients are US banks. 401(k) plan included.",
        # Lower case "us" is the pronoun, not a label.
        "Grow with us: 401(k) plan with us matching 4%.",
    ],
)
def test_a_us_word_in_another_list_item_qualifies_nothing(description: str) -> None:
    assert _kinds("Remote", f"{_SKILLS} {description}") == ["us_payroll"]


@pytest.mark.parametrize(
    "description",
    [
        "Salary: $350k to $401k.",
        "Base pay USD 401k.",
        "Salary between 350k and 401k.",
        "Compensation 350k-401k depending on level.",
        "Compensation 401k to 450k depending on level.",
        "Order 1401k units.",
    ],
)
def test_a_salary_is_not_a_401k(description: str) -> None:
    assert _kinds("Remote", f"{_SKILLS} {description}") == []


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Fully remote, work from anywhere.", []),
        ("Hire from any country we can contract in.", []),
        ("Our team is globally distributed.", []),
        # Also US scope's own "anywhere in the US".
        ("Work from anywhere in the US.", ["us_scope", "us_payroll"]),
    ],
)
def test_a_reach_phrase_overrides_us_payroll(description: str, expected: list[str]) -> None:
    """A US company that says the role can be worked from anywhere also hires
    abroad, and lists its US benefits without labelling them. The read-only
    check before spec 088 found this on a posting the candidate could take."""
    assert _kinds("Remote", f"{_SKILLS} {description}\n{_BENEFITS}") == expected


@pytest.mark.parametrize(
    "description",
    ["Trusted by teams worldwide.", "Loved by customers around the world.", "A global brand."],
)
def test_a_customer_reach_is_not_a_hiring_reach(description: str) -> None:
    assert _kinds("Remote", f"{_SKILLS} {description}\n{_BENEFITS}") == ["us_payroll"]


@pytest.mark.parametrize(
    "description",
    [
        "Subsidized medical, dental, and vision insurance.",
        "Short- and long-term disability coverage.",
        "Life insurance paid by the company.",
        "Salary range: $105,000 to $125,000 USD.",
        "Four on-site visits per year to our client in Atlanta, GA.",
        "Candidates in the Atlanta area will be given priority.",
    ],
)
def test_benefits_offered_outside_the_us_are_not_blockers(description: str) -> None:
    assert _kinds("Remote", f"{_SKILLS} {description}") == []


def test_us_payroll_and_employment_take_one_penalty(cfg: dict[str, object]) -> None:
    both = _job("Frontend Engineer", "Remote", f"W-2 role. {_SKILLS}\n- 401(k) plan\n")
    unblocked = _job("Frontend Engineer", "Remote", f"Contract role. {_SKILLS}")

    score, reasons = rules.score_job(both, cfg)

    assert [r for r in reasons if r.startswith("blocker=")] == [
        'blocker=employment "w-2"',
        'blocker=us_payroll "401(k)"',
    ]
    assert rules.score_job(unblocked, cfg)[0] - score == rules.blocker_penalty(cfg)
