"""Resume content as two documents: shared facts and an industry framing
(spec 098).

Everything here runs on the committed synthetic examples or on synthetic
values written inline. No personal document is read.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from resume_support import (
    example_bundle_raw,
    example_documents,
    single_document_raw,
    store_documents,
    store_example,
)

import harrier.resume.split as split_module
from harrier.connection import Connection, StoreIntegrityError
from harrier.db import connect
from harrier.profile.store import ProfileDocumentError, put_document, put_documents_and_rekind
from harrier.resume.content import (
    ResumeBundleError,
    bundle_from_documents,
    load_bundle,
    load_forbidden_phrases,
    load_skill_vocabulary,
    parse_bundle,
)
from harrier.resume.documents import (
    FACTS_KIND,
    FRAMING_KIND,
    merge_documents,
    split_document,
)
from harrier.resume.split import PRESPLIT_KIND, split_resume
from harrier.tracks import default_scope
from harrier_cli.main import main

ACADEMIC = "lab-search"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def db(data_dir: Path) -> Iterator[Connection]:
    conn = connect()
    yield conn
    conn.close()


def store_old_shape(conn: Connection, raw: dict[str, Any]) -> None:
    put_document(conn, "resume_data", "resume-content.json", "json", json.dumps(raw, indent=2))


def documents(conn: Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT kind, name, content FROM profile_documents ORDER BY kind, name"
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


# --- the split keeps the bundle ------------------------------------------------


def test_split_bundle_equals_the_one_document_bundle() -> None:
    """Against the original one-document example as it was committed, not
    the pair's own merge (review of PR #212)."""
    raw = single_document_raw()
    facts, framing, unplaced = split_document(raw)
    assert unplaced == []
    assert bundle_from_documents(facts, framing) == parse_bundle(raw)
    assert bundle_from_documents(*example_documents()) == parse_bundle(raw)
    # The committed examples are that split, comments apart.
    committed_facts, committed_framing = example_documents()
    assert {k: v for k, v in committed_facts.items() if k != "_comment"} == {
        k: v for k, v in facts.items() if k != "_comment"
    }
    assert {k: v for k, v in committed_framing.items() if k != "_comment"} == framing


def test_every_field_lands_in_exactly_one_document() -> None:
    facts, framing, _ = split_document(example_bundle_raw())
    top = (set(facts) | set(framing)) - {"candidate", "roles", "_comment"}
    assert not (set(facts) & set(framing)) - {"candidate", "roles"}
    assert top
    fact_candidate = cast("dict[str, Any]", facts["candidate"])
    frame_candidate = cast("dict[str, Any]", framing["candidate"])
    assert not set(fact_candidate) & set(frame_candidate)
    assert set(frame_candidate) == {
        "primary_identity",
        "positioning_technologies",
        "experience_statement",
    }
    assert set(framing) - {"_comment"} >= {"bullet_pool", "evaluation_dimensions"}
    assert "forbidden_phrases" in facts


# --- loading -------------------------------------------------------------------


def test_load_bundle_refuses_each_mixed_shape(db: Connection) -> None:
    store_old_shape(db, example_bundle_raw())
    with pytest.raises(ResumeBundleError, match="run harrier profile split-resume"):
        load_bundle(db, default_scope(db))
    with pytest.raises(ResumeBundleError, match="run harrier profile split-resume"):
        load_skill_vocabulary(db)

    store_example(db)
    with pytest.raises(ResumeBundleError, match="keep one shape"):
        load_bundle(db, default_scope(db))
    with pytest.raises(ResumeBundleError, match="keep one shape"):
        load_forbidden_phrases(db)

    with db.transaction():
        db.execute(
            "DELETE FROM profile_documents WHERE kind IN ('resume_data', ?)", (FRAMING_KIND,)
        )
    with pytest.raises(ResumeBundleError, match="track job has no resume framing"):
        load_bundle(db, default_scope(db))


def test_a_key_in_the_wrong_document_is_refused(db: Connection) -> None:
    facts, framing = example_documents()
    facts["bullet_pool"] = {"x": "Shipped a thing."}
    framing_candidate = framing["candidate"]
    framing_candidate["email"] = "someone@example.test"
    store_documents(db, facts, framing)
    with pytest.raises(ResumeBundleError) as refused:
        load_bundle(db, default_scope(db))
    message = str(refused.value)
    assert "resume_facts: bullet_pool belongs in resume_framing" in message
    assert "resume_framing: candidate.email belongs in resume_facts" in message


def test_framing_role_must_name_a_facts_role(db: Connection) -> None:
    facts, framing = example_documents()
    roles = framing["roles"]
    roles.append({"id": "no-such-role", "bullet_count": 2})
    store_documents(db, facts, framing)
    with pytest.raises(ResumeBundleError, match=r"resume_framing: roles\[\d+\] names unknown role"):
        load_bundle(db, default_scope(db))


def test_a_role_framed_twice_is_refused() -> None:
    facts, framing = example_documents()
    framing["roles"].append(dict(framing["roles"][0]))
    with pytest.raises(ResumeBundleError, match=r"resume_framing: roles\[\d+\] frames role"):
        bundle_from_documents(facts, framing)


def test_each_bundle_error_names_its_document() -> None:
    facts, framing = example_documents()
    facts_candidate = facts["candidate"]
    del facts_candidate["email"]
    framing_roles = framing["roles"]
    framing_roles[-1]["bullet_count"] = 0
    with pytest.raises(ResumeBundleError) as refused:
        bundle_from_documents(facts, framing)
    message = str(refused.value)
    assert "resume_facts: candidate: missing or empty email" in message
    last = len(framing_roles) - 1
    assert f"resume_framing: roles[{last}]: bullet_count must be a positive integer" in message


def test_a_missing_facts_candidate_is_blamed_on_the_facts_only() -> None:
    facts, framing = example_documents()
    del facts["candidate"]
    with pytest.raises(ResumeBundleError) as refused:
        bundle_from_documents(facts, framing)
    message = str(refused.value)
    assert "resume_facts: candidate: missing or empty name" in message
    assert "resume_framing:" not in message


def test_the_split_write_is_all_or_nothing(db: Connection) -> None:
    """A failure after the first document is written leaves the store as it
    was: here the rename collides with a row already holding its target."""
    put_document(db, "resume_data", "resume-content.json", "json", "{}")
    put_document(db, PRESPLIT_KIND, "resume-content.json", "json", "{}")
    before = documents(db)
    with pytest.raises(StoreIntegrityError):
        put_documents_and_rekind(
            db,
            [(FACTS_KIND, "resume-facts.json", "json", "{}", None)],
            ("resume_data", "resume-content.json", PRESPLIT_KIND),
        )
    assert documents(db) == before


def test_a_facts_role_without_a_framing_keeps_the_defaults() -> None:
    facts, framing = example_documents()
    framing_roles = framing["roles"]
    dropped = framing_roles.pop()
    bundle = bundle_from_documents(facts, {**framing, "default_achievements": []})
    role = next(r for r in bundle.roles if r.id == dropped["id"])
    assert (role.bullet_count, role.default_bullets, role.competencies) == (2, (), ())


def test_letters_read_the_never_claim_list_from_facts(db: Connection) -> None:
    store_documents(db, {"forbidden_phrases": ["world-class expert"]}, {})
    assert load_forbidden_phrases(db) == ("world-class expert",)
    with db.transaction():
        db.execute("DELETE FROM profile_documents")
    assert load_forbidden_phrases(db) == ()


# --- harrier profile split-resume ---------------------------------------------


def test_split_resume_dry_run_writes_nothing(
    db: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = example_bundle_raw()
    store_old_shape(db, raw)
    before = documents(db)
    assert main(["profile", "split-resume"]) == 0
    out = capsys.readouterr().out
    assert documents(db) == before
    assert "resume_facts/resume-facts.json:" in out
    assert "  candidate.email" in out
    assert "  bullet_pool" in out
    assert "dry run: nothing written" in out
    # Field paths only: no value of the bundle reaches the output.
    candidate = raw["candidate"]
    for value in (candidate["name"], candidate["email"], next(iter(raw["bullet_pool"].values()))):
        assert value not in out


def test_split_resume_writes_the_pair_and_keeps_the_original(
    db: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = example_bundle_raw()
    store_old_shape(db, raw)
    original = documents(db)[0][2]
    assert main(["profile", "split-resume", "--write"]) == 0
    stored = {(kind, name): content for kind, name, content in documents(db)}
    assert set(stored) == {
        (FACTS_KIND, "resume-facts.json"),
        (FRAMING_KIND, "industry.json"),
        (PRESPLIT_KIND, "resume-content.json"),
    }
    assert stored[(PRESPLIT_KIND, "resume-content.json")] == original
    assert load_bundle(db, default_scope(db)) == parse_bundle(raw)
    assert stored[(FACTS_KIND, "resume-facts.json")].startswith('{\n  "')
    capsys.readouterr()

    assert main(["profile", "split-resume", "--write"]) == 0
    assert "already split" in capsys.readouterr().out
    assert {(kind, name) for kind, name, _ in documents(db)} == set(stored)


def test_split_resume_refuses_an_unplaced_key(
    db: Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = copy.deepcopy(example_bundle_raw())
    raw["favourite_colour"] = "teal"
    raw["roles"][0]["mood"] = "calm"
    store_old_shape(db, raw)
    before = documents(db)
    assert main(["profile", "split-resume", "--write"]) == 1
    err = capsys.readouterr().err
    assert "favourite_colour" in err and "roles[0].mood" in err
    assert documents(db) == before


@pytest.mark.parametrize(
    ("setup", "code", "message"),
    [
        ("nothing", 1, "no resume_data document to split"),
        ("both", 1, "keep one shape"),
        ("invalid", 1, "resume_data document is not valid JSON"),
        ("unparseable", 1, "invalid resume content bundle"),
        ("two", 1, "more than one resume_data document"),
        ("presplit", 1, "resume_data_presplit/resume-content.json is already stored"),
        ("unplaced", 1, "keys the split cannot place: favourite_colour"),
        ("framing elsewhere", 1, "the default track has no resume_framing"),
    ],
)
@pytest.mark.parametrize("write", [True, False], ids=["write", "dry-run"])
def test_split_resume_failure_modes(
    db: Connection,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    code: int,
    message: str,
    write: bool,
) -> None:
    """Every refusal is the same with or without --write: a dry run never
    reports success over a blocker the write would refuse (review of PR
    #212)."""
    if setup == "both":
        store_old_shape(db, example_bundle_raw())
        store_example(db)
    elif setup == "invalid":
        put_document(db, "resume_data", "resume-content.json", "json", "{not json")
    elif setup == "unparseable":
        store_old_shape(db, {"candidate": {}})
    elif setup == "two":
        store_old_shape(db, example_bundle_raw())
        put_document(db, "resume_data", "second.json", "json", "{}")
    elif setup == "presplit":
        store_old_shape(db, example_bundle_raw())
        put_document(db, PRESPLIT_KIND, "resume-content.json", "json", "{}")
    elif setup == "unplaced":
        store_old_shape(db, {**example_bundle_raw(), "favourite_colour": "teal"})
    elif setup == "framing elsewhere":
        # The facts, and a framing only another track owns.
        facts, framing = example_documents()
        put_document(db, FACTS_KIND, "resume-facts.json", "json", json.dumps(facts))
        assert main(["tracks", "add", ACADEMIC, "--kind", "academic", "--label", "Lab"]) == 0
        capsys.readouterr()
        lab = db.execute("SELECT id FROM tracks WHERE slug = ?", (ACADEMIC,)).fetchone()[0]
        put_document(db, FRAMING_KIND, "academic.json", "json", json.dumps(framing), track_id=lab)
    before = documents(db)
    argv = ["profile", "split-resume", *(["--write"] if write else [])]
    assert main(argv) == code
    assert message in capsys.readouterr().err
    assert documents(db) == before


def test_the_split_rolls_back_a_failure_partway(db: Connection) -> None:
    """A failure after the pair is written leaves the store as it was: the
    rename of the original is refused here by a trigger (review of PR #212)."""
    store_old_shape(db, example_bundle_raw())
    with db.transaction():
        db.execute(
            "CREATE TRIGGER refuse_the_rename BEFORE UPDATE OF kind ON profile_documents "
            "BEGIN SELECT RAISE(ABORT, 'refused for the test'); END"
        )
    before = documents(db)
    with pytest.raises(StoreIntegrityError, match="refused for the test"):
        split_resume(db, write=True)
    assert documents(db) == before


def test_a_split_that_would_change_the_bundle_writes_nothing(
    db: Connection, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The equality guard: a split that loses a field is refused, whatever
    the table says (review of PR #212)."""
    store_old_shape(db, example_bundle_raw())
    real = split_module.split_document

    def lossy(raw: dict[str, object]) -> tuple[dict[str, object], dict[str, object], list[str]]:
        facts, framing, unplaced = real(raw)
        framing.pop("profile_summary")
        return facts, framing, unplaced

    monkeypatch.setattr(split_module, "split_document", lossy)
    before = documents(db)
    assert main(["profile", "split-resume", "--write"]) == 1
    assert "split would change the bundle: profile_summary" in capsys.readouterr().err
    assert documents(db) == before


def test_a_role_key_in_the_wrong_document_is_refused() -> None:
    """A framing role cannot rewrite a facts role's title, and a facts role
    cannot carry a framing field (review of PR #212)."""
    facts, framing = example_documents()
    framing["roles"][0]["title"] = "Invented Title"
    facts["roles"][0]["competencies"] = ["invented"]
    with pytest.raises(ResumeBundleError) as refused:
        bundle_from_documents(facts, framing)
    message = str(refused.value)
    assert "resume_framing: roles[0].title belongs in resume_facts" in message
    assert "resume_facts: roles[0].competencies belongs in resume_framing" in message


def test_the_old_document_beside_a_framing_alone_is_refused(db: Connection) -> None:
    """`resume_data` with no facts but a track's framing is still two shapes."""
    store_old_shape(db, example_bundle_raw())
    put_document(db, FRAMING_KIND, "industry.json", "json", "{}", track_id=1)
    with pytest.raises(ResumeBundleError, match="keep one shape"):
        load_bundle(db, default_scope(db))


def test_a_rename_with_nothing_to_move_writes_nothing(db: Connection) -> None:
    before = documents(db)
    with pytest.raises(ProfileDocumentError, match=r"no shared resume_data/absent\.json"):
        put_documents_and_rekind(
            db,
            [(FACTS_KIND, "resume-facts.json", "json", "{}", None)],
            ("resume_data", "absent.json", PRESPLIT_KIND),
        )
    assert documents(db) == before


def test_split_resume_is_refused_on_another_track(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "add", ACADEMIC, "--kind", "academic", "--label", "Lab search"]) == 0
    capsys.readouterr()
    assert main(["--track", ACADEMIC, "profile", "split-resume"]) == 2


def test_profile_lead_belongs_to_the_framing() -> None:
    """A track's opening sentence is how it presents the facts (spec 130)."""
    raw = example_bundle_raw()
    raw["candidate"]["profile_lead"] = "Doctoral applicant in computational science"
    facts, framing, unplaced = split_document(raw)
    assert unplaced == []
    fact_candidate = cast("dict[str, Any]", facts["candidate"])
    frame_candidate = cast("dict[str, Any]", framing["candidate"])
    assert frame_candidate["profile_lead"] == "Doctoral applicant in computational science"
    assert "profile_lead" not in fact_candidate
    fact_candidate["profile_lead"] = "Doctoral applicant in computational science"
    refused = merge_documents(facts, framing)
    assert any("profile_lead" in error for error in refused.errors), refused.errors
