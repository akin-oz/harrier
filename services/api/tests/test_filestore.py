"""The storage seam (spec 107).

`test_the_local_store_writes_what_it_wrote_before` is the characterization
test spec 107 asks for: written and passing before the writers moved onto
the seam, and unchanged after. For every key family it runs the module's own
writer under a fixed umask and pins three things for each file it leaves:
the path relative to the data directory, the bytes, and the file mode. Every
input is synthetic. Where a writer stamps the time or renders a document,
the renderer is replaced with a fixed one or the bytes are compared with the
canonical serialization of what they parse to, so the pin is on how the bytes
reach the disk, not on what the renderers say.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import stat
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from academic_support import academic_search_entry, fixture_items
from resume_support import REPO_ROOT, example_bundle_raw, example_documents, store_documents

import harrier.apply.letters as letters_module
import harrier.discovery as discovery_module
import harrier.mail.watch as watch_module
import harrier.offers.batch as batch_module
import harrier.offers.evaluate as evaluate_module
import harrier.outreach.drafts as drafts_module
import harrier.outreach.messages as messages_module
import harrier.resume.tailor as tailor_module
import harrier.scoring.train as train_module
import harrier.screening.linkedin as linkedin_module
import harrier.screening.seen as seen_module
from harrier.academic.discovery import AcademicOptions, run_academic_discovery, save_dataset
from harrier.apply.answers import write_output
from harrier.connection import Connection
from harrier.db import connect, data_dir
from harrier.discovery import DiscoveryOptions, run_discovery
from harrier.outreach.discovery import write_candidates_artifact
from harrier.profile.store import put_document
from harrier.scoring.export import export_features
from harrier.scoring.features import FEATURE_ORDER
from harrier.screening.descriptions import save_description_cache
from harrier.screening.seen import REJECTED, SeenDecision, save_seen
from harrier.sources import apify_academic
from harrier.tracker import add_job
from harrier.tracks import Scope, add_track, default_scope, resolve_scope
from harrier.userconfig.store import ACADEMIC_SEARCHES, set_config

# The umask every case writes under, so the modes below are exact: a plain
# write gives 0o644, and an atomic one 0o600, which `mkstemp` chooses.
UMASK = 0o022
PLAIN = 0o644
ATOMIC = 0o600

COMPANY = "Example Co"
ROLE = "Senior Frontend Engineer"
PAIR = "example-co-senior-frontend-engineer"
# The synthetic example candidate's name, slugged as the resume names it.
RESUME_SLUG = f"deniz-ornek-{PAIR}"
STAMP = "2026-10-15T09:30:00+00:00"

Manifest = dict[str, tuple[bytes, int]]
Case = Callable[[pytest.MonkeyPatch, Path], Manifest]


@contextmanager
def umask(value: int) -> Generator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def written(families: set[str]) -> Manifest:
    """Every file under the data directory in these top-level directories."""
    root = data_dir()
    found: Manifest = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_file() and relative.split("/", 1)[0] in families:
            found[relative] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
    return found


def indented(raw: bytes) -> bytes:
    """`json.dumps(..., indent=2, ensure_ascii=False)` of what `raw` holds."""
    return json.dumps(json.loads(raw), indent=2, ensure_ascii=False).encode("utf-8")


def file_bytes(relative: str) -> bytes:
    return (data_dir() / relative).read_bytes()


def file_mode(relative: str) -> int:
    return stat.S_IMODE((data_dir() / relative).stat().st_mode)


def returning(value: object) -> Callable[..., object]:
    """A stand-in for a renderer: the same value, whatever it is given."""

    def stub(*_args: object, **_kwargs: object) -> object:
        return value

    return stub


def fake_render(html_text: str, pdf_path: Path) -> None:
    pdf_path.write_bytes(b"%PDF-1.4 fake")


def passing(pdf_path: Path, html_text: str) -> list[str]:
    return []


# --- resumes ------------------------------------------------------------------------

MARKDOWN = "# Deniz Örnek\nSenior Frontend Engineer\n\nBody ü"
HTML = "<html><body>Deniz Örnek</body></html>"
METADATA: dict[str, object] = {"company": "Ünal Co", "ai_tailored": False}
EVALUATION = "# Fit ü\n"
MISSING = "# Missing ü\n"


def _fixed_renderers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tailor_module, "build_markdown", returning(MARKDOWN))
    monkeypatch.setattr(tailor_module, "render_html", returning(HTML))
    monkeypatch.setattr(tailor_module, "build_internal_metadata", returning(dict(METADATA)))
    monkeypatch.setattr(tailor_module, "format_fit_evaluation_markdown", returning(EVALUATION))
    monkeypatch.setattr(tailor_module, "missing_keywords_markdown", returning(MISSING))


def _resume_files(slug: str) -> Manifest:
    return {
        f"resumes/{slug}.evaluation.md": (EVALUATION.encode("utf-8"), PLAIN),
        f"resumes/{slug}.html": (HTML.encode("utf-8"), PLAIN),
        f"resumes/{slug}.md": ((MARKDOWN + "\n").encode("utf-8"), PLAIN),
        f"resumes/{slug}.metadata.json": (
            (json.dumps(METADATA, indent=2) + "\n").encode("utf-8"),
            PLAIN,
        ),
        f"resumes/{slug}.pdf": (b"%PDF-1.4 fake", PLAIN),
    }


def _tailor(conn: Connection, scope: Scope) -> None:
    job_id = add_job(
        conn,
        {
            "company": COMPANY,
            "title": ROLE,
            "url": f"https://example.test/{scope.track.slug}",
            "source": "manual",
            "status": "shortlisted",
        },
        scope=scope,
    )
    tailor_module.run_tailor(
        conn,
        scope,
        job_id,
        jd_text="React and TypeScript product role.",
        no_ai=True,
        render=fake_render,
        validate=passing,
    )


def case_resumes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    monkeypatch.chdir(REPO_ROOT)
    _fixed_renderers(monkeypatch)
    with closing(connect()) as conn:
        store_documents(conn, *example_documents())
        pool: dict[str, str] = example_bundle_raw()["bullet_pool"]
        put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(pool.values()))
        _tailor(conn, default_scope(conn))
    return _resume_files(RESUME_SLUG)


def case_resumes_focused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """The focused kind's missing-keywords sidecar (spec 140)."""
    monkeypatch.chdir(REPO_ROOT)
    _fixed_renderers(monkeypatch)
    framing = json.loads(
        (REPO_ROOT / "config" / "resume-framing.focused.example.json").read_text(encoding="utf-8")
    )
    with closing(connect()) as conn:
        store_documents(conn, *example_documents())
        add_track(conn, "bounded", "focused", "Bounded roles")
        scope = resolve_scope(conn, "bounded")
        put_document(
            conn,
            "resume_framing",
            "focused.json",
            "json",
            json.dumps(framing),
            track_id=scope.track.id,
        )
        pool: dict[str, str] = framing["bullet_pool"]
        put_document(conn, "resume_truth", "truth.md", "markdown", "\n".join(pool.values()))
        _tailor(conn, scope)
    return {
        **_resume_files(RESUME_SLUG),
        f"resumes/{RESUME_SLUG}.missing-keywords.md": (MISSING.encode("utf-8"), PLAIN),
    }


# --- cover letters, answers, reports, the audit -------------------------------------

LETTER_MARKDOWN = "# Letter ü\n"
LETTER_HTML = "<html><body>Letter ü</body></html>"


def case_cover_letters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    monkeypatch.setattr(letters_module, "render_cover_letter_markdown", returning(LETTER_MARKDOWN))
    monkeypatch.setattr(letters_module, "render_cover_letter_html", returning(LETTER_HTML))
    with closing(connect()) as conn:
        letters_module.write_cover_letter_artifacts(
            conn,
            COMPANY,
            ROLE,
            "https://example.test/job",
            "Short version.",
            "Full version.",
            render=fake_render,
            validate=passing,
            kind="industry",
        )
    return {
        f"cover-letters/{PAIR}.html": (LETTER_HTML.encode("utf-8"), PLAIN),
        f"cover-letters/{PAIR}.md": (LETTER_MARKDOWN.encode("utf-8"), PLAIN),
        f"cover-letters/{PAIR}.pdf": (b"%PDF-1.4 fake", PLAIN),
    }


def case_answers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    write_output(COMPANY, ROLE, "# Answers ü\n")
    return {f"answers/{PAIR}.md": ("# Answers ü\n".encode(), PLAIN)}


class _Sources:
    truth_text = "Synthetic truth."
    achievements_text = ""


def case_reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    monkeypatch.setattr(evaluate_module, "load_truth_sources", returning(_Sources()))
    monkeypatch.setattr(evaluate_module, "load_seed_stories", returning([]))
    monkeypatch.setattr(evaluate_module, "build_system_prompt", returning(""))
    monkeypatch.setattr(
        evaluate_module,
        "generate_text",
        returning(json.dumps({"verdict": {"verdict": "apply", "confidence": 0.9}})),
    )
    monkeypatch.setattr(evaluate_module, "build_report", returning("# Report ü\n"))
    with closing(connect()) as conn:
        evaluate_module.evaluate_offer(conn, COMPANY, ROLE, "https://example.test/job", "A post.")
    return {f"reports/{PAIR}.md": ("# Report ü\n".encode(), PLAIN)}


def case_evaluations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    first: dict[str, object] = {"at": STAMP, "company": "Ünal Co", "verdict": "skip"}
    second: dict[str, object] = {"at": STAMP, "company": COMPANY, "verdict": "skip"}
    batch_module._append_audit(first)  # pyright: ignore[reportPrivateUsage]
    batch_module._append_audit(second)  # pyright: ignore[reportPrivateUsage]
    lines = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in (first, second))
    return {"evaluations/audit.jsonl": (lines.encode("utf-8"), PLAIN)}


# --- outreach -----------------------------------------------------------------------


def case_outreach(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """Staged candidates, drafts, message bundles and the target store."""
    candidates: dict[str, object] = {"candidates": [{"person_name": "Ünal Example"}]}
    write_candidates_artifact(COMPANY, ROLE, candidates)

    monkeypatch.setattr(drafts_module, "render_outreach_markdown", returning("# Draft ü\n"))
    draft: dict[str, object] = {"request": {"audience": "recruiter"}, "text": "Merhaba ü"}
    drafts_module.write_outreach_draft(COMPANY, ROLE, draft)

    bundle_json: dict[str, object] = {"messages": ["Ü"]}
    monkeypatch.setattr(messages_module, "render_bundle_json", returning(bundle_json))
    monkeypatch.setattr(messages_module, "render_bundle_markdown", returning("# Bundle ü\n"))
    request = messages_module.OutreachRequest(
        job_post_url="https://example.test/job",
        company_name=COMPANY,
        role_title=ROLE,
        target_person_name="Example Person",
    )
    messages_module.save_generated_bundle(request, {})
    targets: list[dict[str, Any]] = [{"company_name": "Ünal Co", "role_title": ROLE}]
    messages_module.save_target_store(targets)

    stem = f"{PAIR}-example-person-recruiter-direct"
    return {
        f"outreach/{PAIR}-candidates.json": (
            json.dumps(candidates, indent=2, ensure_ascii=False).encode("utf-8"),
            PLAIN,
        ),
        f"outreach/{PAIR}-recruiter.json": (
            json.dumps(draft, indent=2, ensure_ascii=False).encode("utf-8"),
            PLAIN,
        ),
        f"outreach/{PAIR}-recruiter.md": ("# Draft ü\n".encode(), PLAIN),
        f"outreach/messages/{stem}.json": (
            json.dumps(bundle_json, indent=2, ensure_ascii=False).encode("utf-8"),
            PLAIN,
        ),
        f"outreach/messages/{stem}.md": ("# Bundle ü\n".encode(), PLAIN),
        "outreach/targets.json": (
            json.dumps(targets, indent=2, ensure_ascii=False).encode("utf-8"),
            PLAIN,
        ),
    }


# --- screening caches and seen state ------------------------------------------------

URL = "https://example.test/jobs/1"
URL_KEY = hashlib.sha256(URL.encode()).hexdigest()[:24]


def case_descriptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    save_description_cache(URL, "Description ü")
    payload = json.dumps({"url": URL, "description": "Description ü"}, ensure_ascii=False)
    return {f"descriptions/{URL_KEY}.json": (payload.encode("utf-8"), ATOMIC)}


def case_linkedin_page_verdicts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    linkedin_module._save_cached_verdict(URL, "remote")  # pyright: ignore[reportPrivateUsage]
    payload = json.dumps({"url": URL, "verdict": "remote"})
    return {f"linkedin-page-verdicts/{URL_KEY}.json": (payload.encode("utf-8"), PLAIN)}


def case_discovery_seen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """Both forms: the default track's file and a track's own (spec 097)."""
    monkeypatch.setattr(seen_module, "now_iso", lambda: STAMP)
    decisions = {"key-ü": SeenDecision(REJECTED, "reason", "policy", STAMP, ("identity",))}
    save_seen("greenhouse", decisions)
    save_seen("apify_academic", decisions, 2)
    payload = json.dumps(
        {
            "decisions": {key: decision.as_json() for key, decision in decisions.items()},
            "updated_at": STAMP,
        },
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        "discovery/2/apify_academic_seen.json": (payload, ATOMIC),
        "discovery/greenhouse_seen.json": (payload, ATOMIC),
    }


# --- discovery runs and their summaries ---------------------------------------------


def case_incoming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """The industry run's per-source and whole-run summaries."""
    workspace = tmp_path / "workspace"
    (workspace / "config").mkdir(parents=True)
    for example in (REPO_ROOT / "config").glob("*.example.*"):
        real = example.name.replace(".example.", ".", 1)
        (workspace / "config" / real).write_bytes(example.read_bytes())
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        discovery_module,
        "load_ats_feeds",
        returning({"greenhouse": ["https://boards.example.test/co"], "ashby": [], "lever": []}),
    )
    monkeypatch.setattr(discovery_module, "fetch_greenhouse_jobs", returning([]))
    with closing(connect()) as conn:
        run_discovery(
            conn,
            default_scope(conn),
            DiscoveryOptions(notify=False, only_sources=frozenset({"greenhouse"})),
        )
    found: Manifest = {}
    for name in ("incoming/greenhouse_latest.json", "incoming/job_imports_run.json"):
        raw = file_bytes(name)
        assert raw == indented(raw), name
        found[name] = (raw, file_mode(name))
    assert {mode for _, mode in found.values()} == {PLAIN}
    return found


def _academic_scope(monkeypatch: pytest.MonkeyPatch, conn: Connection) -> Scope:
    monkeypatch.setenv(apify_academic.TOKEN_ENV, "token-sentinel-value")
    monkeypatch.setenv(apify_academic.ACTOR_ENV, "actor-sentinel-name")
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    monkeypatch.delenv(apify_academic.PORTALS_ENV, raising=False)
    add_track(conn, "second-search", "academic", "Second search")
    set_config(conn, ACADEMIC_SEARCHES, {"second-search": academic_search_entry()})
    return resolve_scope(conn, "second-search")


def case_academic_incoming(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    dataset = tmp_path / "dataset.json"
    dataset.write_text(json.dumps(fixture_items()), encoding="utf-8")
    with closing(connect()) as conn:
        scope = _academic_scope(monkeypatch, conn)
        run_academic_discovery(
            conn,
            scope,
            AcademicOptions(
                dataset_files=[str(dataset)], now=datetime(2026, 6, 15, 10, 0), notify=False
            ),
        )
        track_id = scope.track.id
    name = f"incoming/{track_id}/apify_academic_latest.json"
    raw = file_bytes(name)
    assert raw == indented(raw)
    return {name: (raw, ATOMIC)}


def case_academic_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """A run's raw dataset, and the pruning that keeps the newest."""
    from harrier.academic.discovery import SAVED_RUNS_CAP

    with closing(connect()) as conn:
        scope = _academic_scope(monkeypatch, conn)
    directory = f"discovery/{scope.track.id}/apify_academic/runs"
    items: list[dict[str, Any]] = [{"id": "porta_0001", "title": "Researcher ü"}]
    run_ids = [f"run{index:02d}" for index in range(SAVED_RUNS_CAP + 1)]
    for age, run_id in enumerate(run_ids):
        save_dataset(scope, run_id, items)
        # Older first, by modification time, which is what the pruning reads.
        stamp = datetime(2026, 1, 1, tzinfo=UTC).timestamp() + age * 60
        os.utime(data_dir() / directory / f"{run_id}.json", (stamp, stamp))
    save_dataset(scope, "runlast", items)
    kept = [*run_ids[2:], "runlast"]
    found: Manifest = {}
    for run_id in kept:
        name = f"{directory}/{run_id}.json"
        raw = file_bytes(name)
        parsed = json.loads(raw)
        assert parsed["run_id"] == run_id and parsed["items"] == items
        assert raw == indented(raw)
        found[name] = (raw, ATOMIC)
    return found


# --- scoring --------------------------------------------------------------------------


def case_scoring_exports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """The default track's export, and another track's under its slug."""
    with closing(connect()) as conn:
        export_features(conn, default_scope(conn), today="2026-10-15")
        add_track(conn, "bounded", "focused", "Bounded roles")
        export_features(conn, resolve_scope(conn, "bounded"), today="2026-10-15")
    found: Manifest = {}
    for name in (
        "scoring/exports/bounded/features-20261015.jsonl",
        "scoring/exports/features-20261015.jsonl",
    ):
        raw = file_bytes(name)
        lines = raw.decode("utf-8").splitlines()
        canonical = "\n".join(json.dumps(json.loads(line), sort_keys=True) for line in lines)
        assert raw == (canonical + "\n").encode("utf-8")
        found[name] = (raw, ATOMIC)
    return found


def _training_export() -> None:
    rng = random.Random(7)
    start = datetime(2026, 8, 1, tzinfo=UTC)
    rows: list[str] = []
    for index in range(400):
        label = 1 if rng.random() < 0.5 else 0
        values = {name: rng.random() for name in FEATURE_ORDER}
        values["skill_signal"] = rng.uniform(18, 34) if label else rng.uniform(0, 20)
        decided = start + timedelta(minutes=index * 37)
        rows.append(
            json.dumps(
                {
                    "kind": "row",
                    "job_id": index + 1,
                    "decided_at": decided.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "live": True,
                    "label": label,
                    "features": [values[name] for name in FEATURE_ORDER],
                    "baseline": rng.uniform(0, 120),
                    "shown": "50",
                    "shown_version": "0a1b2c3d4e5f",
                }
            )
        )
    header: dict[str, object] = {
        "kind": "header",
        "format_version": 1,
        "created_at": "2026-10-06",
        "feature_order": list(FEATURE_ORDER),
        "rules_version": "5e5e5e5e5e5e",
        "rows": len(rows),
        "excluded": {},
    }
    path = data_dir() / "scoring" / "exports" / "features-20261006.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([json.dumps(header), *rows]) + "\n", encoding="utf-8")


def case_scoring_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """A trained model, its report, and the active model it becomes."""
    metrics = {"model": 0.9, "baseline": 0.5, "delta_ci95": [0.1, 0.2]}
    monkeypatch.setattr(train_module, "evaluate", returning(dict(metrics)))
    _training_export()
    outcome = train_module.train(activate=True, today="2026-10-15")
    assert outcome.activated
    model = file_bytes("scoring/active-model.json")
    identity = hashlib.sha256(model).hexdigest()[:12]
    canonical = json.dumps(json.loads(model), sort_keys=True, indent=2, ensure_ascii=True)
    assert model == (canonical + "\n").encode("utf-8")
    report = file_bytes("scoring/reports/report-20261015.json")
    assert report == indented(report)
    return {
        "scoring/active-model.json": (model, ATOMIC),
        f"scoring/models/job-fit-20261015-{identity}.json": (model, ATOMIC),
        "scoring/reports/report-20261015.json": (report, ATOMIC),
    }


# --- the mail watch -------------------------------------------------------------------


def case_gmail_watch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Manifest:
    """The seen state, and the events archive appended and rotated."""
    watch_module.save_state({"seen_message_ids": ["ü-1"]})
    monkeypatch.setattr(watch_module, "EVENTS_MAX_LINES", 2)
    events: list[dict[str, object]] = [
        {"kind": "interview", "company": f"Ünal {index}", "timestamp": STAMP} for index in range(3)
    ]
    for event in events:
        watch_module.append_event(event)
    kept = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events[1:])
    state = json.dumps({"seen_message_ids": ["ü-1"]}, indent=2, ensure_ascii=False)
    return {
        "gmail-watch/events.jsonl": (kept.encode("utf-8"), PLAIN),
        "gmail-watch/seen_messages.json": (state.encode("utf-8"), ATOMIC),
    }


CASES: dict[str, Case] = {
    "academic-incoming": case_academic_incoming,
    "academic-runs": case_academic_runs,
    "answers": case_answers,
    "cover-letters": case_cover_letters,
    "descriptions": case_descriptions,
    "discovery-seen": case_discovery_seen,
    "evaluations": case_evaluations,
    "gmail-watch": case_gmail_watch,
    "incoming": case_incoming,
    "linkedin-page-verdicts": case_linkedin_page_verdicts,
    "outreach": case_outreach,
    "reports": case_reports,
    "resumes": case_resumes,
    "resumes-focused": case_resumes_focused,
    "scoring-exports": case_scoring_exports,
    "scoring-models": case_scoring_models,
}


@pytest.mark.parametrize("family", sorted(CASES))
def test_the_local_store_writes_what_it_wrote_before(
    family: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Spec 107, PR 1: the same relative path, bytes and mode for every
    family, and nothing else in its directory, no temporary file included."""
    with umask(UMASK):
        expected = CASES[family](monkeypatch, tmp_path)
    families = {name.split("/", 1)[0] for name in expected}
    actual = written(families)
    if family == "scoring-models":
        # The training input is the test's own, written above.
        actual.pop("scoring/exports/features-20261006.jsonl")
    assert sorted(actual) == sorted(expected)
    for name, (content, mode) in expected.items():
        assert actual[name][0] == content, name
        assert actual[name][1] == mode, f"{name}: {oct(actual[name][1])} != {oct(mode)}"
