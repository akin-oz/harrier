"""Discovery on an academic track (spec 097).

Every database is built under the per-test data directory with synthetic
rows. Every actor response is the synthetic fixture
(`fixtures/academic-dataset.json`) or a stub transport: no test reaches the
network, and no test needs a real token or actor name.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from academic_support import (
    ACADEMIC_FIXTURE,
    EXAMPLE_SEARCHES,
    academic_search_entry,
    fixture_items,
)

import harrier.academic.discovery as academic_discovery
import harrier.discovery as industry_discovery
from harrier.academic.discovery import (
    SAVED_RUNS_CAP,
    AcademicOptions,
    run_academic_discovery,
    run_configured_tracks,
    save_dataset,
)
from harrier.academic.search import (
    SearchError,
    compile_input,
    parse_entry,
    policy_fingerprint,
    validate_searches,
)
from harrier.db import connect, data_dir
from harrier.digest import schedule_health_lines
from harrier.runoutcome import all_last_success, record_success
from harrier.screening.normalized import stable_key
from harrier.screening.pipeline import (
    ACADEMIC_GATE_ORDER,
    AcademicGates,
    AcademicIndexes,
    screen_jobs,
)
from harrier.screening.policy import academic_policy_version
from harrier.screening.rules import EXCLUDED_TITLE_HINTS, AcademicTerm, academic_match, fold_text
from harrier.screening.seen import REJECTED, SeenDecision, load_seen, save_seen
from harrier.sources import apify_academic as source
from harrier.tracker.store import extract_note_value, list_jobs
from harrier.tracks import Scope, default_scope, resolve_scope
from harrier.userconfig.store import ACADEMIC_SEARCHES, ConfigError, get_config, set_config
from harrier_cli.main import main

SLUG = "second-search"
OTHER = "third-search"
LABEL = "Second search"
TOKEN_SENTINEL = "token-sentinel-value"
ACTOR_SENTINEL = "actor-sentinel-name"
NOW = datetime(2026, 6, 15, 10, 0)
RUN_DATE = NOW.date()

# --- helpers --------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_log_setup(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    """`main` installs a handler on the captured stream, which pytest closes
    between tests; the academic tests read summaries, not logs."""

    def no_logging(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr("harrier_cli.main.configure_logging", no_logging)


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(source.TOKEN_ENV, TOKEN_SENTINEL)
    monkeypatch.setenv(source.ACTOR_ENV, ACTOR_SENTINEL)
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv(source.PORTALS_ENV, raising=False)


@pytest.fixture
def track(keys: None, capsys: pytest.CaptureFixture[str]) -> Iterator[str]:
    assert main(["tracks", "add", SLUG, "--kind", "academic", "--label", LABEL]) == 0
    capsys.readouterr()
    store({SLUG: academic_search_entry()})
    yield SLUG


def add_track(slug: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tracks", "add", slug, "--kind", "academic", "--label", slug.title()]) == 0
    capsys.readouterr()


def store(value: dict[str, Any]) -> None:
    with closing(connect()) as conn:
        set_config(conn, ACADEMIC_SEARCHES, value)


def store_raw(value: object) -> None:
    """A row written around `config set`, as a restored backup could be."""
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO user_config (kind, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT (kind) DO UPDATE SET value = excluded.value",
            (ACADEMIC_SEARCHES, json.dumps(value)),
        )
        conn.commit()


def item(number: int, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"porta_{number:04d}",
        "title": "Researcher in Example Modelling",
        "institution": f"Example Institute {number}",
        "department": None,
        "country": "FR",
        "city": "Exampleville",
        "deadline": "2099-03-31",
        "postedDate": "2026-06-01",
        "salaryInfo": None,
        "contractDuration": "Three years",
        "workingLanguage": "English",
        "jobType": "Research position",
        "field": "Physics",
        "fundingSource": "Example Research Council",
        "descriptionRaw": "A post in example modelling.",
        "applicationUrl": f"https://positions.example.org/calls/{number:04d}",
        "sourcePortal": "portal-a",
        "scrapedAt": "2026-06-14T09:00:00.000Z",
    }
    base.update(fields)
    return base


def dataset(tmp_path: Path, items: list[dict[str, Any]], name: str = "dataset.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


def scope_for(conn: sqlite3.Connection, slug: str = SLUG) -> Scope:
    return resolve_scope(conn, slug)


def discover(
    tmp_path: Path,
    items: list[dict[str, Any]],
    *,
    slug: str = SLUG,
    dry_run: bool = False,
    now: datetime = NOW,
    notify: bool = False,
) -> dict[str, object]:
    path = dataset(tmp_path, items, f"{slug}-{len(items)}-{dry_run}.json")
    with closing(connect()) as conn:
        return run_academic_discovery(
            conn,
            scope_for(conn, slug),
            AcademicOptions(dataset_files=[path], dry_run=dry_run, now=now, notify=notify),
        )


def rows(slug: str = SLUG) -> list[dict[str, str]]:
    with closing(connect()) as conn:
        return list_jobs(conn, scope_for(conn, slug))


def seen(slug: str = SLUG) -> dict[str, SeenDecision]:
    with closing(connect()) as conn:
        track_id = scope_for(conn, slug).track.id
    return load_seen(source.SOURCE_NAME, track_id)


def screen(
    items: list[dict[str, Any]],
    *,
    run_date: date = RUN_DATE,
    previous: dict[str, SeenDecision] | None = None,
    indexes: AcademicIndexes | None = None,
    entry_value: dict[str, Any] | None = None,
    **overrides: Any,
) -> tuple[Any, dict[str, SeenDecision]]:
    """The academic gates alone, over items, with no database."""
    raw = entry_value if entry_value is not None else academic_search_entry(**overrides)
    entry = parse_entry(SLUG, raw)
    jobs = source.normalize_items(items).jobs
    decisions = dict(previous or {})
    result = screen_jobs(
        jobs,
        source_seen=decisions,
        academic=AcademicGates(
            entry=entry,
            run_date=run_date,
            policy=academic_policy_version(policy_fingerprint(entry)),
            track_slug=SLUG,
            indexes=indexes or AcademicIndexes(),
        ),
    )
    return result, decisions


def reasons(decisions: dict[str, SeenDecision]) -> list[str]:
    return sorted(decision.reason for decision in decisions.values())


class FakeApify:
    """A stub transport: the run starts, polls to `status`, and its dataset
    is `items`. Every call is recorded with its method, URL and payload."""

    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        status: str = "SUCCEEDED",
        charged: dict[str, int] | None = None,
        prices: dict[str, float] | None = None,
        never_finishes: bool = False,
        missing_run: bool = False,
    ) -> None:
        self.items = items
        self.status = status
        self.charged = charged
        self.prices = prices
        self.never_finishes = never_finishes
        self.missing_run = missing_run
        self.calls: list[tuple[str, str, object]] = []

    def run_object(self, status: str) -> dict[str, Any]:
        run: dict[str, Any] = {"id": "run-1", "status": status, "defaultDatasetId": "ds-1"}
        if self.charged is not None:
            run["chargedEventCounts"] = self.charged
        if self.prices is not None:
            run["pricingInfo"] = {
                "pricingPerEvent": {
                    "actorChargeEvents": {
                        name: {"eventPriceUsd": price} for name, price in self.prices.items()
                    }
                }
            }
        return run

    def __call__(self, url: str, *, method: str = "GET", payload: object = None) -> object:
        self.calls.append((method, url, payload))
        path = urlsplit(url).path
        if path.endswith("/abort"):
            return {"data": self.run_object("ABORTED")}
        if method == "POST" and path.endswith("/runs"):
            return {"data": self.run_object("RUNNING")}
        if "/actor-runs/" in path:
            if self.missing_run:
                raise RuntimeError("Apify request failed after 3 attempts: HTTP Error 404")
            status = "RUNNING" if self.never_finishes else self.status
            return {"data": self.run_object(status)}
        if "/datasets/" in path:
            return self.items
        raise AssertionError(f"unexpected request {method} {path}")

    def started(self) -> list[tuple[str, str, object]]:
        return [
            call
            for call in self.calls
            if call[0] == "POST" and call[1].split("?")[0].endswith("/runs")
        ]

    def query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.started()[0][1]).query)


def live(
    transport: FakeApify,
    *,
    slug: str = SLUG,
    dry_run: bool = False,
    clock: Any = None,
) -> dict[str, object]:
    options = AcademicOptions(
        dry_run=dry_run, now=NOW, notify=False, transport=transport, sleep=lambda _: None
    )
    if clock is not None:
        options.clock = clock
    with closing(connect()) as conn:
        return run_academic_discovery(conn, scope_for(conn, slug), options)


# --- the search entry and where it lives ----------------------------------------------


def test_the_source_input_is_compiled_from_the_profile() -> None:
    entry = parse_entry(SLUG, academic_search_entry(window_days=21))
    compiled = compile_input(entry)
    assert compiled[source.INPUT_MAP["keywords"]] == ["example modelling", "uberbeispiel"]
    assert compiled[source.INPUT_MAP["countries"]] == ["FR", "IT"]
    assert compiled[source.INPUT_MAP["window"]] == 21
    assert compiled[source.INPUT_MAP["result_cap"]] == 200
    assert source.INPUT_MAP["portals"] not in compiled


def test_the_input_map_names_every_compiled_field() -> None:
    compiled_values = {
        "keywords",
        "countries",
        "window",
        "result_cap",
        "portals",
        "translation",
        "memory",
    }
    assert set(source.INPUT_MAP) == compiled_values
    assert all(source.INPUT_MAP[name] for name in compiled_values)


def test_keyword_translation_and_source_memory_are_always_compiled_off() -> None:
    compiled = compile_input(parse_entry(SLUG, academic_search_entry()))
    assert compiled[source.INPUT_MAP["translation"]] is False
    assert compiled[source.INPUT_MAP["memory"]] is False
    # No entry field can turn them on: an unknown field is refused.
    with pytest.raises(SearchError, match="unknown fields"):
        validate_searches({SLUG: academic_search_entry(translate=True)})


def test_position_terms_are_not_sent_to_the_source() -> None:
    compiled = compile_input(parse_entry(SLUG, academic_search_entry()))
    keywords = cast("list[str]", compiled[source.INPUT_MAP["keywords"]])
    assert "researcher" not in keywords
    assert "research engineer" not in keywords


def test_the_search_entry_carries_no_actor_field_name() -> None:
    example = json.loads(EXAMPLE_SEARCHES.read_text(encoding="utf-8"))
    entry_text = json.dumps(example)
    actor_fields = {value for value in source.INPUT_MAP.values()} | {
        value for value in source.FIELD_MAP.values() if value is not None
    }
    # Words the entry and the actor share by meaning, not by coupling.
    shared_names = {
        "title",
        "deadline",
        "id",
        "field",
        "country",
        "city",
        "department",
        "countries",
    }
    for name in actor_fields - shared_names:
        assert f'"{name}"' not in entry_text, name


def test_the_academic_search_is_validated_on_write_and_on_read(keys: None) -> None:
    bad_values: list[tuple[object, str]] = [
        ({SLUG: academic_search_entry(colour="blue")}, "unknown fields"),
        ({SLUG: {k: v for k, v in academic_search_entry().items() if k != "ceilings"}}, "ceilings"),
        ({SLUG: academic_search_entry(ceilings={"max_results": 10})}, "max_charge_usd"),
        ({SLUG: academic_search_entry(countries=["ZZ"])}, "does not support"),
        ({SLUG: academic_search_entry(portals=["portal-a"])}, "portals"),
        ({SLUG: academic_search_entry(areas=[])}, "areas"),
        ({"Not A Slug": academic_search_entry()}, "not a track slug"),
        ([], "must be an object"),
    ]
    with closing(connect()) as conn:
        for value, message in bad_values:
            with pytest.raises(ConfigError, match=message):
                set_config(conn, ACADEMIC_SEARCHES, value)
            store_raw(value)
            with pytest.raises(ConfigError, match=message):
                get_config(conn, ACADEMIC_SEARCHES)


def test_a_profile_keyed_by_a_malformed_slug_is_refused_at_the_write(keys: None) -> None:
    with closing(connect()) as conn, pytest.raises(ConfigError, match="not a track slug"):
        set_config(conn, ACADEMIC_SEARCHES, {"Bad Slug!": academic_search_entry()})


def test_a_corrupted_profile_row_is_refused_on_read(keys: None) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO user_config (kind, value) VALUES (?, ?)", (ACADEMIC_SEARCHES, "{not json")
        )
        conn.commit()
        with pytest.raises(ConfigError, match="not valid JSON"):
            get_config(conn, ACADEMIC_SEARCHES)


def test_the_academic_search_resolves_store_or_empty(
    keys: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A working copy beside the example is never read: no file fallback.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "academic-searches.json").write_text(
        json.dumps({SLUG: academic_search_entry()}), encoding="utf-8"
    )
    with closing(connect()) as conn:
        assert get_config(conn, ACADEMIC_SEARCHES) is None
        assert academic_discovery.load_searches(conn) == {}
        set_config(conn, ACADEMIC_SEARCHES, {SLUG: academic_search_entry()})
        assert list(academic_discovery.load_searches(conn)) == [SLUG]


def test_config_set_writes_the_academic_search(
    keys: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "searches.json"
    path.write_text(json.dumps({SLUG: academic_search_entry()}), encoding="utf-8")
    assert main(["config", "set", ACADEMIC_SEARCHES, "--file", str(path)]) == 0
    with closing(connect()) as conn:
        assert list(academic_discovery.load_searches(conn)) == [SLUG]
    path.write_text(json.dumps({SLUG: academic_search_entry(window_days=0)}), encoding="utf-8")
    capsys.readouterr()
    assert main(["config", "set", ACADEMIC_SEARCHES, "--file", str(path)]) != 0
    assert "window_days" in capsys.readouterr().err


def test_academic_discover_reads_only_the_profile_kind(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []
    real_connect = sqlite3.connect

    def tracing(database: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(database, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracing)
    path = dataset(tmp_path, fixture_items())
    assert main(["--track", SLUG, "discover", "--dataset-file", path, "--no-notify"]) == 0
    assert not any("profile_documents" in statement for statement in statements)
    config_reads = [statement for statement in statements if "user_config" in statement]
    assert config_reads
    assert all(ACADEMIC_SEARCHES in statement for statement in config_reads), config_reads


def test_an_academic_discover_loads_no_candidate_configuration_or_hold_list(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_: object, **__: object) -> object:
        raise AssertionError("an academic discover read the industry configuration")

    monkeypatch.setattr(industry_discovery, "load_candidate_config", refused)
    monkeypatch.setattr(industry_discovery, "load_hold_companies", refused)
    monkeypatch.setattr(industry_discovery, "load_ats_feeds", refused)
    monkeypatch.setattr("harrier.screening.config.load_candidate_config", refused)
    summary = discover(tmp_path, fixture_items())
    assert int(str(summary["kept"])) > 0


def test_no_committed_file_holds_a_search() -> None:
    classification = json.loads(
        (EXAMPLE_SEARCHES.parent / "data-classification.json").read_text(encoding="utf-8")
    )
    assert "config/academic-searches.json" in classification["never_in_git"]["patterns"]
    example = json.loads(EXAMPLE_SEARCHES.read_text(encoding="utf-8"))
    assert list(example) == ["example-track"]
    entry = example["example-track"]
    for area in entry["areas"]:
        assert all(
            "example" in term["text"] or "beispiel" in term["text"] for term in area["terms"]
        )
    # The actor is named in the environment only.
    source_text = Path(source.__file__).read_text(encoding="utf-8")
    assert "ACTOR_ENV" in source_text
    assert "DEFAULT_ACTOR" not in source_text


# --- the gates and the matcher -----------------------------------------------------------


def test_a_local_language_variant_is_both_searched_and_matched() -> None:
    entry = academic_search_entry(
        areas=[
            {
                "label": "Example area",
                "terms": [
                    {"text": "example modelling"},
                    {"text": "beispielmodell", "prefix": True},
                ],
            }
        ],
        require_area_match=True,
    )
    compiled = compile_input(parse_entry(SLUG, entry))
    assert "beispielmodell" in cast("list[str]", compiled[source.INPUT_MAP["keywords"]])
    result, _ = screen(
        [item(1, title="Researcher, Beispielmodellierung", descriptionRaw="", field=None)],
        **{key: value for key, value in entry.items()},
    )
    assert len(result.new_tracker_rows) == 1
    assert "beispielmodell" in result.new_tracker_rows[0]["notes"]


def test_a_posting_with_no_area_match_is_kept_and_recorded_as_none() -> None:
    result, _ = screen([item(1, title="Researcher in History", descriptionRaw="", field=None)])
    assert len(result.new_tracker_rows) == 1
    assert extract_note_value(result.new_tracker_rows[0]["notes"], "matched") == "none"


def test_require_area_match_rejects_as_area_unmatched() -> None:
    result, decisions = screen(
        [item(1, title="Researcher in History", descriptionRaw="", field=None)],
        require_area_match=True,
    )
    assert result.new_tracker_rows == []
    assert reasons(decisions) == ["area_unmatched"]


def test_position_terms_gate_on_their_configured_fields() -> None:
    by_type = item(1, title="Example Modelling Post", jobType="Researcher")
    neither = item(2, title="Example Modelling Post", jobType="Technical staff")
    in_description = item(
        3, title="Example Modelling Post", jobType=None, descriptionRaw="For a researcher."
    )
    result, decisions = screen([by_type, neither, in_description])
    assert [row["title"] for row in result.new_tracker_rows] == ["Example Modelling Post"]
    assert reasons(decisions).count("position_unmatched") == 2
    widened, _ = screen(
        [in_description],
        position={"terms": [{"text": "researcher"}], "in": ["title", "description"]},
    )
    assert len(widened.new_tracker_rows) == 1


def test_an_exclude_on_organisation_rejects_by_the_organisation_field() -> None:
    excluded = item(1, institution="Excluded Example College")
    named_in_title = item(2, title="Researcher, Excluded Example College partnership")
    _, decisions = screen([excluded, named_in_title])
    assert reasons(decisions) == [
        "exclude:Excluded Example College@organisation",
        "passed every gate",
    ]


@pytest.mark.parametrize("hint", sorted(EXCLUDED_TITLE_HINTS))
def test_the_academic_matcher_applies_no_industry_title_hint(hint: str) -> None:
    result, decisions = screen([item(1, title=f"Researcher, {hint} example modelling")])
    assert len(result.new_tracker_rows) == 1, (hint, reasons(decisions))


def test_an_empty_list_rejects_nothing() -> None:
    assert academic_match([], "anything at all") is None
    entry = academic_search_entry(exclude=[])
    entry.pop("position")
    result, _ = screen(
        [item(1, title="Any Post At All", jobType=None, descriptionRaw="", field=None)],
        entry_value=entry,
    )
    assert len(result.new_tracker_rows) == 1


def test_a_prefix_term_matches_a_compound_word() -> None:
    term = AcademicTerm("uberbeispiel", prefix=True)
    assert academic_match([term], "Überbeispielforschung Researcher") == term
    assert academic_match([AcademicTerm("uberbeispiel")], "Überbeispielforschung") is None


def test_a_whole_word_term_does_not_match_inside_a_longer_word() -> None:
    assert academic_match([AcademicTerm("art")], "Particle studies") is None
    assert academic_match([AcademicTerm("art")], "Art and studies") is not None


def test_matching_ignores_case_accents_and_unicode_form() -> None:
    term = AcademicTerm("cafe etude")
    assert academic_match([term], "CAFÉ ÉTUDE") == term
    # A compatibility form (a full-width letter) folds under NFKC.
    assert academic_match([AcademicTerm("research")], "\uff52esearch post") is not None
    assert fold_text("Étude") == fold_text("Étude") == "etude"


def test_periods_inside_an_abbreviation_do_not_block_a_match() -> None:
    assert academic_match([AcademicTerm("abc")], "An A.B.C. position") is not None
    assert academic_match([AcademicTerm("a.b.c")], "An ABC position") is not None


def test_a_phrase_matches_across_hyphens_and_slashes() -> None:
    term = AcademicTerm("example modelling")
    assert academic_match([term], "Example-Modelling post") == term
    assert academic_match([term], "example / modelling post") == term
    assert academic_match([term], "example, modelling post") is None


def test_the_academic_gate_order_is_pinned() -> None:
    assert ACADEMIC_GATE_ORDER == ("seen", "exclude", "position", "area", "deadline", "dedupe")
    passed = "2001-01-01"
    every = item(
        1, title="Lecturer", jobType="Other", deadline=passed, descriptionRaw="", field=None
    )
    no_exclude = item(
        2, title="Post", jobType="Other", deadline=passed, descriptionRaw="", field=None
    )
    position_ok = item(3, title="Researcher", deadline=passed, descriptionRaw="", field=None)
    area_ok = item(4, title="Researcher in Example Modelling", deadline=passed)
    indexes = AcademicIndexes()
    indexes.add(
        url="https://positions.example.org/calls/0004",
        external_key="",
        apply_link="",
        company="",
        title="",
        deadline="",
        slug="job",
    )
    _, decisions = screen(
        [every, no_exclude, position_ok, area_ok], require_area_match=True, indexes=indexes
    )
    by_title = {
        job["title"]: decisions[job["job_key"]].reason
        for job in source.normalize_items([every, no_exclude, position_ok, area_ok]).jobs
    }
    assert by_title == {
        "Lecturer": "exclude:lecturer@title",
        "Post": "position_unmatched",
        "Researcher": "area_unmatched",
        "Researcher in Example Modelling": "deadline_passed",
    }


def test_discover_on_an_academic_track_runs_only_its_sources(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_: object, **__: object) -> object:
        raise AssertionError("an industry source ran on an academic track")

    for name in (
        "fetch_greenhouse_jobs",
        "fetch_ashby_jobs",
        "fetch_lever_jobs",
        "fetch_remoteok_jobs",
        "fetch_apify_linkedin_jobs",
        "run_discovery",
    ):
        monkeypatch.setattr(industry_discovery, name, refused)
    path = dataset(tmp_path, [item(1)])
    assert main(["--track", SLUG, "discover", "--dataset-file", path, "--no-notify"]) == 0
    kept = rows()
    assert [row["deadline"] for row in kept] == ["2099-03-31"]
    with closing(connect()) as conn:
        assert list_jobs(conn, default_scope(conn)) == []


def test_an_academic_row_has_no_score_signals_or_remote_filter(track: str, tmp_path: Path) -> None:
    discover(tmp_path, [item(1)])
    [row] = rows()
    for column in ("score", "fit_score", "signals", "remote_filter", "scoring_version"):
        assert row.get(column, "") == "", column
    assert row["next_action"] == "read the call and decide whether to apply"


def test_seen_state_is_kept_per_track(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_track(OTHER, capsys)
    excluding = academic_search_entry(
        exclude=[{"terms": [{"text": "researcher"}], "in": ["title"]}]
    )
    store({SLUG: excluding, OTHER: academic_search_entry()})
    discover(tmp_path, [item(1)], slug=SLUG)
    assert rows(SLUG) == []
    discover(tmp_path, [item(1)], slug=OTHER)
    assert len(rows(OTHER)) == 1


def test_seen_state_files_are_keyed_by_track(track: str, tmp_path: Path) -> None:
    discover(tmp_path, [item(1)])
    with closing(connect()) as conn:
        track_id = scope_for(conn).track.id
    assert (data_dir() / "discovery" / str(track_id) / f"{source.SOURCE_NAME}_seen.json").is_file()
    assert not (data_dir() / "discovery" / f"{source.SOURCE_NAME}_seen.json").exists()


# --- the ceilings --------------------------------------------------------------------


def test_the_result_cap_is_written_into_the_source_input(track: str) -> None:
    fake = FakeApify([item(1)])
    live(fake)
    payload = fake.started()[0][2]
    assert isinstance(payload, dict)
    assert payload[source.INPUT_MAP["result_cap"]] == 200
    assert fake.query()["maxItems"] == ["200"]
    assert fake.query()["maxTotalChargeUsd"] == ["1.00"]


def test_a_profile_whose_caps_can_cost_more_than_its_ceiling_is_refused(keys: None) -> None:
    entry = academic_search_entry(ceilings={"max_results": 300, "max_charge_usd": 1.0})
    with pytest.raises(SearchError, match=r"can cost 1\.21 USD, above max_charge_usd 1\.0"):
        validate_searches({SLUG: entry})


def test_a_ceiling_above_its_bound_is_refused_at_the_write(keys: None) -> None:
    for ceilings, field_name in (
        ({"max_results": 600, "max_charge_usd": 5.0}, "max_results"),
        ({"max_results": 100, "max_charge_usd": 6.0}, "max_charge_usd"),
    ):
        with pytest.raises(SearchError, match=f"{field_name} .* above its hard limit"):
            validate_searches({SLUG: academic_search_entry(ceilings=ceilings)})


def test_a_stored_ceiling_above_its_bound_is_clamped_at_use(track: str) -> None:
    store_raw({SLUG: academic_search_entry(ceilings={"max_results": 900, "max_charge_usd": 50})})
    fake = FakeApify([item(1)])
    summary = live(fake)
    assert summary["clamped"] == ["max_results", "max_charge_usd"]
    assert fake.query()["maxItems"] == [str(source.ACADEMIC_MAX_RESULTS)]
    assert fake.query()["maxTotalChargeUsd"] == [f"{source.ACADEMIC_MAX_CHARGE_USD:.2f}"]


def test_the_run_carries_timeout_and_memory_options(track: str) -> None:
    fake = FakeApify([item(1)])
    live(fake)
    assert fake.query()["memory"] == [str(source.MEMORY_MB)]
    assert fake.query()["timeout"] == [str(source.TIMEOUT_SECONDS)]


def test_a_run_stopped_at_the_charge_ceiling_is_read_and_screened(track: str) -> None:
    fake = FakeApify(
        [item(1), item(2)],
        status="ABORTED",
        charged={"apify-actor-start": 1, "result": 247},
        prices={"apify-actor-start": 0.01, "result": 0.004},
    )
    summary = live(fake)
    assert summary["stopped_at_ceiling"] is True
    assert summary["run_status"] == "ABORTED"
    assert len(rows()) == 2


def test_an_aborted_run_below_the_ceiling_writes_nothing(track: str) -> None:
    fake = FakeApify(
        [item(1)],
        status="ABORTED",
        charged={"apify-actor-start": 1, "result": 10},
        prices={"apify-actor-start": 0.01, "result": 0.004},
    )
    summary = live(fake)
    assert "ABORTED" in str(summary["failed"])
    assert rows() == []
    assert seen() == {}


def test_a_local_wait_timeout_aborts_the_remote_run(track: str) -> None:
    ticks = iter(range(0, 100_000, 1000))
    fake = FakeApify([item(1)], never_finishes=True)
    summary = live(fake, clock=lambda: float(next(ticks)))
    assert "aborted" in str(summary["failed"])
    assert any(call[1].split("?")[0].endswith("/actor-runs/run-1/abort") for call in fake.calls)
    assert rows() == []


def test_the_summary_prices_charged_events_from_the_run_object(track: str) -> None:
    fake = FakeApify(
        [item(1)],
        charged={"apify-actor-start": 1, "result": 1},
        prices={"apify-actor-start": 0.01, "result": 0.004},
    )
    summary = live(fake)
    assert summary["charged_events"] == {"apify-actor-start": 1, "result": 1}
    assert summary["charged_usd"] == pytest.approx(0.014)
    assert summary["pricing_differs"] is False
    differing = FakeApify(
        [item(2)],
        charged={"apify-actor-start": 1, "result": 1},
        prices={"apify-actor-start": 0.01, "result": 0.005},
    )
    assert live(differing)["pricing_differs"] is True


def test_a_missing_actor_variable_refuses_before_billing(
    track: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for variable in (source.TOKEN_ENV, source.ACTOR_ENV):
        with monkeypatch.context() as patched:
            patched.delenv(variable)
            fake = FakeApify([item(1)])
            summary = live(fake)
            assert variable in str(summary["failed"])
            assert fake.calls == []


def test_a_failed_run_writes_nothing(track: str) -> None:
    summary = live(FakeApify([item(1)], status="FAILED"))
    assert "FAILED" in str(summary["failed"])
    assert rows() == []
    assert seen() == {}


# --- policy version and reconsider ---------------------------------------------------


def _version(**overrides: Any) -> str:
    return academic_policy_version(
        policy_fingerprint(parse_entry(SLUG, academic_search_entry(**overrides)))
    )


def test_an_academic_decision_carries_the_profiles_policy_version(
    track: str, tmp_path: Path
) -> None:
    summary = discover(tmp_path, [item(1), item(2, title="Lecturer")])
    assert summary["policy_version"] == _version()
    assert {decision.policy for decision in seen().values()} == {_version()}


def test_editing_a_deciding_profile_field_moves_the_academic_version() -> None:
    base = _version()
    assert _version(exclude=[]) != base
    assert _version(position={"terms": [{"text": "fellow"}]}) != base
    assert _version(require_area_match=True) != base


def test_editing_a_labelling_field_does_not_move_it() -> None:
    base = _version()
    relabelled = academic_search_entry()["areas"]
    relabelled[0]["label"] = "Another label"
    assert _version(areas=relabelled) == base
    assert _version(flag_phrases={"eligibility": ["another phrase"]}) == base
    assert _version(countries=["IT"]) == base
    assert _version(window_days=30) == base
    assert _version(ceilings={"max_results": 10, "max_charge_usd": 0.5}) == base


def test_editing_the_industry_configuration_does_not_move_the_academic_version(
    track: str, tmp_path: Path
) -> None:
    from harrier.profile.store import put_document

    before = discover(tmp_path, [item(1)])["policy_version"]
    with closing(connect()) as conn:
        put_document(
            conn,
            "candidate",
            "candidate.json",
            "json",
            json.dumps({"targets": {"title_keywords_include": ["something else"]}}),
        )
    after = discover(tmp_path, [item(2)])["policy_version"]
    assert before == after == _version()


def test_activating_a_learned_model_does_not_move_the_academic_version(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = discover(tmp_path, [item(1)])["policy_version"]
    monkeypatch.setattr("harrier.scoring.model.active_model_identity", lambda: "model-xyz")
    after = discover(tmp_path, [item(2)])["policy_version"]
    assert before == after


def test_an_edited_exclusion_reopens_the_rejections_it_caused(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store({SLUG: academic_search_entry(exclude=[{"terms": [{"text": "researcher"}]}])})
    discover(tmp_path, [item(1)])
    assert rows() == []
    store({SLUG: academic_search_entry()})
    capsys.readouterr()
    assert main(["--track", SLUG, "reconsider", "--apply"]) == 0
    assert "1 cleared" in capsys.readouterr().out
    discover(tmp_path, [item(1)])
    assert len(rows()) == 1


def test_reconsider_on_an_academic_track_clears_only_its_own_stale_rejections(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add_track(OTHER, capsys)
    narrow = academic_search_entry(exclude=[{"terms": [{"text": "researcher"}]}])
    store({SLUG: narrow, OTHER: narrow})
    discover(tmp_path, [item(1)], slug=SLUG)
    discover(tmp_path, [item(1)], slug=OTHER)
    store({SLUG: academic_search_entry(), OTHER: narrow})
    other_before = seen(OTHER)
    assert main(["--track", SLUG, "reconsider", "--apply"]) == 0
    assert seen(SLUG) == {}
    assert seen(OTHER) == other_before
    # And the default track's own seen state is never touched.
    assert not (data_dir() / "discovery" / f"{source.SOURCE_NAME}_seen.json").exists()


def test_reconsider_never_reopens_a_human_rejection_on_an_academic_track(
    track: str, capsys: pytest.CaptureFixture[str]
) -> None:
    url = "https://positions.example.org/calls/0042"
    company = "Example Institute 42"
    assert (
        main(["--track", SLUG, "add", "--company", company, "--title", "Researcher", "--url", url])
        == 0
    )
    with closing(connect()) as conn:
        job_id = list_jobs(conn, scope_for(conn))[0]["id"]
        track_id = scope_for(conn).track.id
    assert main(["--track", SLUG, "reject", job_id, "closed"]) == 0
    key = stable_key(source.SOURCE_NAME, "example institute 42", url)
    save_seen(
        source.SOURCE_NAME,
        {key: SeenDecision(REJECTED, "exclude:researcher@title", "older-policy", NOW.isoformat())},
        track_id,
    )
    capsys.readouterr()
    assert main(["--track", SLUG, "reconsider", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "1 left alone because you rejected them" in out
    assert key in seen()


# --- dry runs, shadow runs and replays -----------------------------------------------


def test_shadow_on_an_academic_track_makes_no_request_and_prints_the_compiled_input(
    track: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(source.TOKEN_ENV)
    monkeypatch.delenv(source.ACTOR_ENV)

    def no_network(*_: object, **__: object) -> object:
        raise AssertionError("a shadow run made a request")

    monkeypatch.setattr(source, "_default_transport", no_network)
    assert main(["--track", SLUG, "discover", "--shadow"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["shadow"] is True
    assert plan["compiled_input"][source.INPUT_MAP["keywords"]] == [
        "example modelling",
        "uberbeispiel",
    ]
    assert plan["ceilings"]["worst_case_usd"] == pytest.approx(0.81)
    assert plan["gates"]["exclude"]
    assert plan["policy_version"] == _version()
    assert rows() == []
    assert seen() == {}
    with closing(connect()) as conn:
        assert all_last_success(conn) == {}


def test_shadow_reports_a_refused_profile_and_exits_2(
    track: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store_raw({SLUG: academic_search_entry(ceilings={"max_results": 400, "max_charge_usd": 1.0})})
    assert main(["--track", SLUG, "discover", "--shadow"]) == 2
    assert "max_results 400 can cost" in capsys.readouterr().err


def test_an_academic_dataset_file_replays_without_a_request(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_: object, **__: object) -> object:
        raise AssertionError("a replay made a request")

    monkeypatch.setattr("harrier.sources.apify_linkedin.request_json", no_network)
    monkeypatch.setattr(source, "_default_transport", no_network)
    summary = discover(tmp_path, [item(1)])
    assert summary["kept"] == 1


def test_from_run_reads_an_existing_dataset_without_starting_a_run(track: str) -> None:
    fake = FakeApify([item(1)])
    with closing(connect()) as conn:
        summary = run_academic_discovery(
            conn,
            scope_for(conn),
            AcademicOptions(from_run="run-1", now=NOW, notify=False, transport=fake),
        )
    assert fake.started() == []
    assert summary["run_id"] == "run-1"
    assert len(rows()) == 1


def test_a_billed_run_saves_its_raw_dataset_before_normalization(track: str) -> None:
    raw = [item(1), item(2, title="")]
    summary = live(FakeApify(raw))
    saved = Path(str(summary["saved_dataset"]))
    with closing(connect()) as conn:
        track_id = scope_for(conn).track.id
    assert (
        saved
        == data_dir() / "discovery" / str(track_id) / source.SOURCE_NAME / "runs" / "run-1.json"
    )
    stored = json.loads(saved.read_text(encoding="utf-8"))
    assert stored["run_id"] == "run-1"
    # The item normalization skips is still there: saved before it ran.
    assert stored["items"] == raw


def test_a_dry_run_writes_no_dataset_file_and_prints_its_run_id(
    track: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeApify([item(1)])
    monkeypatch.setattr(source, "_default_transport", fake)
    assert main(["--track", SLUG, "discover", "--dry-run", "--no-notify"]) == 0
    assert "run id: run-1" in capsys.readouterr().out
    assert not (data_dir() / "discovery").exists() or not list(
        (data_dir() / "discovery").rglob("runs/*.json")
    )


def test_an_academic_dry_run_writes_no_row_seen_file_summary_or_message(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(academic_discovery, "send_telegram_message", sent.append)
    path = dataset(tmp_path, fixture_items())
    with closing(connect()) as conn:
        run_academic_discovery(
            conn, scope_for(conn), AcademicOptions(dataset_files=[path], dry_run=True, now=NOW)
        )
        assert all_last_success(conn) == {}
    assert rows() == []
    assert seen() == {}
    assert not (data_dir() / "incoming").exists()
    assert sent == []


def test_a_dry_run_then_a_real_run_judges_the_same_postings(track: str, tmp_path: Path) -> None:
    dry = discover(tmp_path, fixture_items(), dry_run=True)
    real = discover(tmp_path, fixture_items())
    assert dry["kept"] == real["kept"]
    assert dry["rejected_counts"] == real["rejected_counts"]


def test_a_replayed_dataset_uses_the_same_screening_and_seen_state(
    track: str, tmp_path: Path
) -> None:
    replayed = discover(tmp_path, fixture_items(), dry_run=True)
    fetched = live(FakeApify(fixture_items()), dry_run=True)
    for key in ("rejected_counts", "kept", "skipped_missing_title_or_url", "deadline_unreadable"):
        assert replayed[key] == fetched[key], key
    # A real replay writes the seen state a live run then reads: every
    # posting it judged is skipped, except a passed deadline, judged again.
    first = discover(tmp_path, fixture_items())
    again = live(FakeApify(fixture_items()))
    assert again["kept"] == 0
    rejected = first["rejected_counts"]
    assert isinstance(rejected, dict)
    assert again["rejected_counts"] == {"deadline_passed": rejected["deadline_passed"]}


def test_saved_runs_beyond_the_cap_are_evicted_by_age(track: str) -> None:
    with closing(connect()) as conn:
        scope = scope_for(conn)
    saved: list[Path] = []
    for index in range(SAVED_RUNS_CAP + 3):
        path = save_dataset(scope, f"run-{index:03d}", [])
        stamp = 1_700_000_000 + index * 60
        os.utime(path, (stamp, stamp))
        saved.append(path)
    directory = saved[-1].parent
    kept = sorted(file.stem for file in directory.glob("*.json"))
    assert len(kept) <= SAVED_RUNS_CAP
    assert "run-000" not in kept
    assert f"run-{SAVED_RUNS_CAP + 2:03d}" in kept


def test_a_failed_write_after_a_successful_run_is_finished_from_the_saved_run(
    track: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    with monkeypatch.context() as patched:
        patched.setattr(academic_discovery, "add_job", broken)
        with pytest.raises(sqlite3.OperationalError):
            live(FakeApify([item(1)]))
    saved = next((data_dir() / "discovery").rglob("runs/run-1.json"))
    with closing(connect()) as conn:
        run_academic_discovery(
            conn,
            scope_for(conn),
            AcademicOptions(dataset_files=[str(saved)], now=NOW, notify=False),
        )
    assert len(rows()) == 1


# --- window and records --------------------------------------------------------------


def test_a_profile_without_a_posting_age_window_is_refused() -> None:
    entry = academic_search_entry()
    entry.pop("window_days")
    with pytest.raises(SearchError, match="missing 'window_days'"):
        validate_searches({SLUG: entry})


def test_the_window_is_written_into_the_source_input(track: str) -> None:
    store({SLUG: academic_search_entry(window_days=21)})
    fake = FakeApify([item(1)])
    live(fake)
    payload = fake.started()[0][2]
    assert isinstance(payload, dict)
    assert payload[source.INPUT_MAP["window"]] == 21


def test_each_track_records_its_own_last_successful_discovery(track: str, tmp_path: Path) -> None:
    discover(tmp_path, [item(1)])
    with closing(connect()) as conn:
        recorded = all_last_success(conn)
        track_id = scope_for(conn).track.id
    assert list(recorded) == [f"discovery:{track_id}"]


def test_a_gap_longer_than_the_window_is_named_in_the_summary(track: str, tmp_path: Path) -> None:
    with closing(connect()) as conn:
        track_id = scope_for(conn).track.id
        record_success(conn, f"discovery:{track_id}", at=(NOW - timedelta(days=30)).isoformat())
    summary = discover(tmp_path, [item(1)])
    assert "longer than the 14-day window" in str(summary["gap"])
    recent = discover(tmp_path, [item(2)])
    assert recent["gap"] == ""


def test_items_without_a_posting_date_are_counted(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1, postedDate=None), item(2)])
    assert summary["without_posting_date"] == 1


def test_the_summary_says_results_below_the_cap_were_not_fetched(
    track: str, tmp_path: Path
) -> None:
    store({SLUG: academic_search_entry(ceilings={"max_results": 2, "max_charge_usd": 1.0})})
    capped = discover(tmp_path, [item(1), item(2)])
    assert "were not fetched" in str(capped["cap_reached"])
    under = discover(tmp_path, [item(3)])
    assert "cap_reached" not in under


def test_each_academic_rejection_records_its_gate_and_detail(track: str, tmp_path: Path) -> None:
    discover(tmp_path, fixture_items())
    recorded = {decision.reason for decision in seen().values()}
    assert "exclude:lecturer@title" in recorded
    assert "exclude:Excluded Example College@organisation" in recorded
    assert "deadline_passed" in recorded
    assert "position_unmatched" in recorded


def test_the_summary_keys_rejections_by_gate(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, fixture_items())
    counts = cast("dict[str, int]", summary["rejected_counts"])
    assert set(counts) <= {
        "exclude",
        "position_unmatched",
        "area_unmatched",
        "deadline_passed",
        "tracker_duplicate",
    }
    assert counts["exclude"] == 2
    details = summary["rejected_details"]
    assert isinstance(details, dict)
    assert details["exclude:lecturer@title"] == 1


def test_term_hits_name_unused_terms(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1)])
    hits = summary["term_hits"]
    assert isinstance(hits, dict)
    assert hits["example modelling"] == 1
    assert hits["researcher"] == 1
    assert summary["unused_terms"] == ["uberbeispiel", "research engineer"]


def test_a_dry_run_on_an_academic_track_lists_rejected_postings_with_reasons(
    track: str, tmp_path: Path
) -> None:
    summary = discover(tmp_path, [item(1, title="Lecturer in Example Modelling")], dry_run=True)
    assert summary["rejected_postings"] == [
        {
            "title": "Lecturer in Example Modelling",
            "organisation": "Example Institute 1",
            "reason": "exclude:lecturer@title",
        }
    ]


def test_a_kept_academic_row_records_the_terms_that_kept_it_in_its_notes(
    track: str, tmp_path: Path
) -> None:
    discover(tmp_path, [item(1)])
    [row] = rows()
    assert extract_note_value(row["notes"], "matched") == "Example area:example modelling@title"
    assert extract_note_value(row["notes"], "position") == "researcher@title"


def test_an_academic_run_leaves_the_default_tracks_summary_and_health_alone(
    track: str, tmp_path: Path
) -> None:
    discover(tmp_path, [item(1)])
    assert not (data_dir() / "incoming" / "job_imports_run.json").exists()
    with closing(connect()) as conn:
        assert "discovery" not in all_last_success(conn)
        track_id = scope_for(conn).track.id
    assert (data_dir() / "incoming" / str(track_id) / f"{source.SOURCE_NAME}_latest.json").is_file()


def test_schedule_health_names_the_academic_job(track: str) -> None:
    with closing(connect()) as conn:
        track_id = scope_for(conn).track.id
        lines = schedule_health_lines(conn)
    assert any(f"discovery:{track_id}" in line for line in lines)


def test_the_academic_telegram_summary_shows_deadlines_and_no_terms(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(academic_discovery, "send_telegram_message", sent.append)
    # Titles that carry no search term, so the message is held to its own
    # fields: it must not add the terms, the flags or the description.
    plain = {"jobType": "Researcher", "descriptionRaw": "example modelling text"}
    discover(
        tmp_path,
        [item(1, title="Post One", **plain), item(2, title="Post Two", deadline=None, **plain)],
        notify=True,
    )
    [message] = sent
    assert LABEL in message
    assert "deadline: 2099-03-31" in message
    assert "deadline: no deadline" in message
    assert "score" not in message
    for term in ("example modelling", "researcher", "uberbeispiel", "flags"):
        assert term not in message.lower(), term


def test_configured_tracks_reads_slugs_from_the_store(
    track: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_track(OTHER, capsys)
    store({SLUG: academic_search_entry(), OTHER: academic_search_entry()})
    monkeypatch.setenv("HARRIER_DEMO", "1")
    with closing(connect()) as conn:
        reports = run_configured_tracks(conn, AcademicOptions(now=NOW, notify=False))
    assert [report.slug for report in reports] == [SLUG, OTHER]
    assert all(report.summary is not None and not report.problem for report in reports)


def test_configured_tracks_reports_an_unknown_archived_or_industry_slug_and_runs_the_rest(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    add_track(OTHER, capsys)
    assert main(["tracks", "archive", OTHER]) == 0
    store(
        {
            "no-such-track": academic_search_entry(),
            OTHER: academic_search_entry(),
            "job": academic_search_entry(),
            SLUG: academic_search_entry(),
        }
    )
    monkeypatch.setenv("HARRIER_DEMO", "1")
    capsys.readouterr()
    assert main(["discover", "--scheduled", "--configured-tracks", "--no-notify"]) == 3
    err = capsys.readouterr().err
    assert "no-such-track: no track named no-such-track" in err
    assert f"{OTHER}: track {OTHER} is archived" in err
    assert "job: track job is not an academic track" in err
    assert len(rows(SLUG)) > 0
    with closing(connect()) as conn:
        assert list_jobs(conn, default_scope(conn)) == []


def test_configured_tracks_leave_industry_seen_state_untouched(
    track: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    industry_seen = data_dir() / "discovery" / "greenhouse_seen.json"
    industry_seen.parent.mkdir(parents=True, exist_ok=True)
    industry_seen.write_text('{"decisions": {}, "updated_at": "2026-01-01"}', encoding="utf-8")
    before = industry_seen.read_bytes()
    monkeypatch.setenv("HARRIER_DEMO", "1")
    assert main(["discover", "--configured-tracks", "--no-notify"]) == 0
    assert industry_seen.read_bytes() == before
    assert sorted(path.name for path in (data_dir() / "discovery").glob("*_seen.json")) == [
        "greenhouse_seen.json"
    ]


# --- components, flags and the deadline ------------------------------------------------


def test_field_map_carries_optional_fields_into_metadata_and_notes(
    track: str, tmp_path: Path
) -> None:
    [job] = source.normalize_items([item(1)]).jobs
    assert job["metadata"]["position_type"] == "Research position"
    assert job["metadata"]["subject"] == "Physics"
    assert job["metadata"]["portal"] == "portal-a"
    discover(tmp_path, [item(1)])
    [row] = rows()
    for name, value in (
        ("position_type", "Research position"),
        ("subject", "Physics"),
        ("funding", "Example Research Council"),
        ("contract", "Three years"),
        ("posting_language", "English"),
        ("portal", "portal-a"),
    ):
        assert extract_note_value(row["notes"], name) == value, name


def test_a_missing_optional_field_reads_not_stated(track: str, tmp_path: Path) -> None:
    discover(tmp_path, [item(1, fundingSource=None, salaryInfo=None)])
    [row] = rows()
    assert extract_note_value(row["notes"], "funding") == "not stated"
    assert extract_note_value(row["notes"], "salary_text") == "not stated"
    assert extract_note_value(row["notes"], "start") == "not stated"


def test_each_flag_records_its_phrase_and_field(track: str, tmp_path: Path) -> None:
    discover(
        tmp_path,
        [
            item(
                1,
                descriptionRaw="Applicants must meet the example eligibility phrase.",
                fundingSource="Example unfunded value",
            )
        ],
    )
    [row] = rows()
    flags = extract_note_value(row["notes"], "flags").split("|")
    assert "eligibility:description:example eligibility phrase" in flags
    assert "funding:funding:Example unfunded value" in flags


def test_a_flag_never_rejects_and_never_reorders_the_queue(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    flagged = item(1, deadline="2099-05-01", descriptionRaw="the example eligibility phrase")
    plain = item(2, deadline="2099-04-01")
    discover(tmp_path, [flagged, plain])
    assert len(rows()) == 2
    capsys.readouterr()
    assert main(["--track", SLUG, "next"]) == 0
    out = capsys.readouterr().out
    assert out.index("Example Institute 2") < out.index("Example Institute 1")


def test_an_excluded_position_type_is_rejected_and_an_unlisted_or_empty_one_kept() -> None:
    entry = academic_search_entry(
        exclude=[{"terms": [{"text": "technical staff"}], "in": ["position_type"]}],
        position={"terms": [{"text": "researcher"}], "in": ["title"]},
    )
    excluded = item(1, jobType="Technical staff")
    unlisted = item(2, jobType="A type never seen before")
    empty = item(3, jobType=None)
    result, decisions = screen([excluded, unlisted, empty], **entry)
    assert len(result.new_tracker_rows) == 2
    assert "exclude:technical staff@position_type" in reasons(decisions)


def test_the_summary_counts_postings_per_position_type(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1), item(2, jobType="Fellowship"), item(3, jobType=None)])
    assert summary["position_type_counts"] == {
        "Research position": 1,
        "Fellowship": 1,
        "not stated": 1,
    }


def test_next_and_review_print_components_without_a_number(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    discover(tmp_path, [item(1, fundingSource=None)])
    for verb in ("next", "review"):
        capsys.readouterr()
        assert main(["--track", SLUG, verb]) == 0
        out = capsys.readouterr().out
        assert "matched: 'example modelling' in title" in out
        assert "position: 'researcher' in title" in out
        assert "funding: not stated" in out
        assert "score" not in out


def test_flag_evidence_with_separators_round_trips_through_notes(
    track: str, tmp_path: Path
) -> None:
    value = "unfunded; see = notes | here"
    store({SLUG: academic_search_entry(funding_flag_values=[value])})
    discover(tmp_path, [item(1, fundingSource=value)])
    [row] = rows()
    assert (
        extract_note_value(row["notes"], "flags") == "funding:funding:unfunded  see   notes   here"
    )
    # The separators were stripped, so every later note still reads.
    assert extract_note_value(row["notes"], "portal") == "portal-a"
    assert extract_note_value(row["notes"], "funding") == "unfunded  see   notes   here"


def test_a_deadline_on_the_run_date_or_the_day_before_is_kept() -> None:
    result, _ = screen(
        [item(1, deadline="2026-06-15"), item(2, deadline="2026-06-14")],
        run_date=date(2026, 6, 15),
    )
    assert len(result.new_tracker_rows) == 2


def test_a_deadline_two_days_before_the_run_date_is_rejected() -> None:
    _, decisions = screen([item(1, deadline="2026-06-13")], run_date=date(2026, 6, 15))
    assert reasons(decisions) == ["deadline_passed"]


def test_the_run_date_is_taken_once_per_run(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates: list[date] = []
    real = academic_discovery.screen_jobs

    def capture(*args: Any, **kwargs: Any) -> Any:
        dates.append(kwargs["academic"].run_date)
        return real(*args, **kwargs)

    monkeypatch.setattr(academic_discovery, "screen_jobs", capture)
    pinned = datetime(2026, 6, 15, 23, 59)
    discover(tmp_path, [item(1, deadline="2026-06-14")], now=pinned)
    assert dates == [date(2026, 6, 15)]
    assert len(rows()) == 1


def test_an_ambiguous_numeric_deadline_is_stored_empty_with_its_text(
    track: str, tmp_path: Path
) -> None:
    discover(tmp_path, [item(1, deadline="03/04/2099")])
    [row] = rows()
    assert row["deadline"] == ""
    assert extract_note_value(row["notes"], "deadline_text") == "03/04/2099"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2099-06-30", "2099-06-30"),
        ("2099-06-30T23:59:00Z", "2099-06-30"),
        ("30 June 2099", "2099-06-30"),
        ("June 30, 2099", "2099-06-30"),
        ("30 Jun 2099", "2099-06-30"),
        ("31/12/2099", "2099-12-31"),
        ("12/31/2099", "2099-12-31"),
        ("05.05.2099", "2099-05-05"),
    ],
)
def test_a_month_name_or_unambiguous_numeric_deadline_is_converted(raw: str, expected: str) -> None:
    assert source.parse_deadline(raw) == (expected, False)


@pytest.mark.parametrize("raw", ["03/04/2099", "1-15 June 2099", "open until filled", "2099-02-30"])
def test_an_unconvertible_deadline_is_left_empty(raw: str) -> None:
    assert source.parse_deadline(raw) == ("", True)


def test_a_deadline_passed_posting_is_judged_again_and_kept_when_extended() -> None:
    first, decisions = screen([item(1, deadline="2026-06-01")])
    assert first.new_tracker_rows == []
    assert reasons(decisions) == ["deadline_passed"]
    extended, after = screen([item(1, deadline="2026-07-01")], previous=decisions)
    assert len(extended.new_tracker_rows) == 1
    assert reasons(after) == ["passed every gate"]


def test_an_unmappable_item_is_skipped_and_counted(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1, title=""), item(2, applicationUrl=None), item(3)])
    assert summary["skipped_missing_title_or_url"] == 2
    assert summary["kept"] == 1


def test_an_unreadable_deadline_is_kept_empty_and_counted(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1, deadline="sometime in spring")])
    assert summary["deadline_unreadable"] == 1
    assert summary["kept"] == 1
    assert summary["kept_without_deadline"] == 1


# --- dedupe and coverage ---------------------------------------------------------------


def test_two_calls_with_one_title_and_different_deadlines_are_both_kept(
    track: str, tmp_path: Path
) -> None:
    first = item(1, institution="Sample University", deadline="2099-03-31")
    second = item(2, institution="Sample University", deadline="2099-09-30")
    discover(tmp_path, [first, second])
    assert len(rows()) == 2


def test_one_call_listed_on_two_portals_is_kept_once_by_its_apply_link() -> None:
    first = item(
        1, applicationUrl="https://Positions.example.org/calls/9/", sourcePortal="portal-a"
    )
    second = item(
        2, applicationUrl="https://positions.example.org/calls/9#apply", sourcePortal="portal-b"
    )
    result, decisions = screen([first, second])
    assert len(result.new_tracker_rows) == 1
    assert f"tracker_duplicate:apply_url:{SLUG}" in reasons(decisions)


def test_apply_links_differing_only_in_query_string_are_not_merged() -> None:
    first = item(1, applicationUrl="https://positions.example.org/view?id=1")
    second = item(2, applicationUrl="https://positions.example.org/view?id=2")
    result, _ = screen([first, second])
    assert len(result.new_tracker_rows) == 2


def test_an_academic_duplicate_names_the_identity_and_the_track_that_holds_it(
    track: str, tmp_path: Path
) -> None:
    url = "https://boards.example.com/northwind/101"
    assert (
        main(["add", "--company", "Northwind Labs", "--title", "Senior Engineer", "--url", url])
        == 0
    )
    discover(tmp_path, [item(1, applicationUrl=url)])
    assert rows() == []
    assert reasons(seen()) == ["tracker_duplicate:url:job"]


def test_a_company_and_title_match_with_an_empty_deadline_is_still_a_duplicate(
    track: str, tmp_path: Path
) -> None:
    discover(tmp_path, [item(1, institution="Sample University", deadline=None)])
    discover(tmp_path, [item(2, institution="Sample University", deadline="2099-09-30")])
    assert len(rows()) == 1
    assert f"tracker_duplicate:organisation_title:{SLUG}" in reasons(seen())


def test_the_summary_counts_items_per_portal(track: str, tmp_path: Path) -> None:
    summary = discover(
        tmp_path, [item(1), item(2, sourcePortal="portal-b"), item(3, sourcePortal="portal-b")]
    )
    assert summary["portal_counts"] == {"portal-a": 1, "portal-b": 2}


def test_a_listed_portal_with_no_items_is_named_in_the_summary(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(source.PORTALS_ENV, "portal-a,portal-c")
    store({SLUG: academic_search_entry(portals=["portal-a", "portal-c"])})
    summary = discover(tmp_path, [item(1)])
    assert summary["listed_portals_without_items"] == ["portal-c"]


def test_unlisted_portals_are_not_reported_as_empty(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1)])
    assert summary["listed_portals_without_items"] == []
    assert summary["portal_counts"] == {"portal-a": 1}


# --- fixture, demo and privacy -----------------------------------------------------------


def test_demo_discovery_screens_the_academic_fixture_through_every_gate(
    track: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HARRIER_DEMO", "1")
    url = "https://boards.example.com/northwind/101"
    assert (
        main(["add", "--company", "Northwind Labs", "--title", "Senior Engineer", "--url", url])
        == 0
    )
    store({SLUG: academic_search_entry(require_area_match=True)})
    capsys.readouterr()
    assert main(["--track", SLUG, "discover", "--no-notify"]) == 0
    summary = json.loads(capsys.readouterr().out.split("\nrun id:")[0])
    assert summary["replayed_files"] == ["academic-dataset.json"]
    assert summary["rejected_counts"] == {
        "exclude": 2,
        "position_unmatched": 1,
        "area_unmatched": 1,
        "deadline_passed": 1,
        "tracker_duplicate": 1,
    }
    assert summary["skipped_missing_title_or_url"] == 2
    assert summary["dataset_duplicates"] == 1
    assert summary["deadline_unreadable"] == 1
    assert summary["kept"] == 5


def test_the_academic_demo_makes_no_request_and_needs_no_keys(
    track: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_: object, **__: object) -> object:
        raise AssertionError("the demo made a request")

    monkeypatch.setenv("HARRIER_DEMO", "1")
    monkeypatch.delenv(source.TOKEN_ENV)
    monkeypatch.delenv(source.ACTOR_ENV)
    monkeypatch.setattr(source, "_default_transport", no_network)
    monkeypatch.setattr("harrier.sources.apify_linkedin.request_json", no_network)
    assert main(["--track", SLUG, "discover", "--no-notify"]) == 0
    assert len(rows()) > 0


def test_the_field_map_reads_the_fixture_shape() -> None:
    keys: set[str] = set()
    for entry in fixture_items():
        keys |= set(entry)
    mapped = {value for value in source.FIELD_MAP.values() if value is not None}
    assert mapped <= keys, mapped - keys
    assert any(name != value for name, value in source.FIELD_MAP.items() if value is not None)
    assert ACADEMIC_FIXTURE.is_file()


def test_the_deadline_gate_uses_the_run_date_not_the_clock(track: str, tmp_path: Path) -> None:
    summary = discover(tmp_path, [item(1, deadline="2001-01-31")], now=datetime(2001, 1, 15, 9, 0))
    assert summary["kept"] == 1


def test_the_academic_source_never_logs_the_actor_or_its_input(
    track: str, caplog: pytest.LogCaptureFixture
) -> None:
    keyword = "sentinelkeywordterm"
    title = "Sentinel Title Text"
    store(
        {
            SLUG: academic_search_entry(
                areas=[{"label": "Sentinel area", "terms": [{"text": keyword}]}]
            )
        }
    )
    caplog.set_level(logging.DEBUG)
    live(FakeApify([item(1, title=title, descriptionRaw="sentinel description text")]))
    logged = "\n".join(
        record.getMessage() for record in caplog.records if record.levelno >= logging.INFO
    )
    for sentinel in (ACTOR_SENTINEL, TOKEN_SENTINEL, keyword, title, "sentinel description text"):
        assert sentinel not in logged, sentinel
    assert "run-1" in logged


def test_no_item_text_reaches_the_summary_or_telegram(
    track: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(academic_discovery, "send_telegram_message", sent.append)
    description = "sentinel description text"
    summary = discover(tmp_path, [item(1, descriptionRaw=description)], notify=True)
    assert description not in json.dumps(summary)
    assert description not in "\n".join(sent)
    latest = next((data_dir() / "incoming").rglob(f"{source.SOURCE_NAME}_latest.json"))
    assert description not in latest.read_text(encoding="utf-8")


# --- review of PR #187 -----------------------------------------------------------


def test_an_unreadable_stored_search_is_reported_by_configured_tracks(
    track: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with closing(connect()) as conn:
        conn.execute(
            "UPDATE user_config SET value = ? WHERE kind = ?", ("{not json", ACADEMIC_SEARCHES)
        )
        conn.commit()
    capsys.readouterr()
    assert main(["discover", "--scheduled", "--configured-tracks", "--no-notify"]) == 3
    assert f"{ACADEMIC_SEARCHES}: stored {ACADEMIC_SEARCHES}" in capsys.readouterr().err


def test_a_flag_phrase_with_no_word_is_refused_at_the_write() -> None:
    with pytest.raises(SearchError, match=r"flag_phrases\.eligibility has a phrase with no word"):
        validate_searches({SLUG: academic_search_entry(flag_phrases={"eligibility": ["!!!"]})})


def test_an_apply_link_with_a_query_string_is_stored_whole_and_matches_the_next_run(
    track: str, tmp_path: Path
) -> None:
    link = "https://positions.example.org/view?id=1"
    discover(tmp_path, [item(1, applicationUrl=link)])
    [row] = rows()
    assert extract_note_value(row["notes"], "apply_url") == link
    # The same call on another portal, its listing URL differing only by a
    # fragment: a later run matches it through the stored application link.
    discover(tmp_path, [item(2, applicationUrl=f"{link}#apply")])
    assert len(rows()) == 1
    assert f"tracker_duplicate:apply_url:{SLUG}" in reasons(seen())


def test_an_apply_link_is_shown_as_the_posting_gave_it(
    track: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stored escaped, so the notes stay parseable; shown whole, including a
    `;` and a `%3B` the link already held."""
    link = "https://positions.example.org/apply;session=abc?ref=a%3Bb"
    discover(tmp_path, [item(1, applicationUrl=link)])
    [row] = rows()
    assert ";" not in extract_note_value(row["notes"], "apply_url")
    assert extract_note_value(row["notes"], "portal") == "portal-a"
    capsys.readouterr()
    assert main(["--track", SLUG, "next"]) == 0
    assert f"apply url: {link}" in capsys.readouterr().out
