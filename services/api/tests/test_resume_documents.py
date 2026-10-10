"""Resume content as two documents: shared facts and an industry framing
(spec 098).

Everything here runs on the committed synthetic examples or on synthetic
values written inline. No personal document is read.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from resume_support import (
    example_bundle_raw,
    example_documents,
    store_documents,
    store_example,
)

from harrier.db import connect
from harrier.profile.store import put_document, put_documents_and_rekind
from harrier.resume.content import (
    ResumeBundleError,
    bundle_from_documents,
    load_bundle,
    load_forbidden_phrases,
    load_skill_vocabulary,
    parse_bundle,
)
from harrier.resume.documents import FACTS_KIND, FRAMING_KIND, split_document
from harrier.resume.split import PRESPLIT_KIND
from harrier_cli.main import main

ACADEMIC = "lab-search"


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    monkeypatch.setenv("HARRIER_DATA_DIR", str(directory))
    monkeypatch.delenv("HARRIER_DEMO", raising=False)
    return directory


@pytest.fixture()
def db(data_dir: Path) -> Iterator[sqlite3.Connection]:
    conn = connect()
    yield conn
    conn.close()


def store_old_shape(conn: sqlite3.Connection, raw: dict[str, Any]) -> None:
    put_document(conn, "resume_data", "resume-content.json", "json", json.dumps(raw, indent=2))


def documents(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        "SELECT kind, name, content FROM profile_documents ORDER BY kind, name"
    ).fetchall()
    return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]


# --- the split keeps the bundle ------------------------------------------------


def test_split_bundle_equals_the_one_document_bundle() -> None:
    raw = example_bundle_raw()
    facts, framing, unplaced = split_document(raw)
    assert unplaced == []
    assert bundle_from_documents(facts, framing) == parse_bundle(raw)
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


def test_load_bundle_refuses_each_mixed_shape(db: sqlite3.Connection) -> None:
    store_old_shape(db, example_bundle_raw())
    with pytest.raises(ResumeBundleError, match="run harrier profile split-resume"):
        load_bundle(db)
    with pytest.raises(ResumeBundleError, match="run harrier profile split-resume"):
        load_skill_vocabulary(db)

    store_example(db)
    with pytest.raises(ResumeBundleError, match="keep one shape"):
        load_bundle(db)
    with pytest.raises(ResumeBundleError, match="keep one shape"):
        load_forbidden_phrases(db)

    db.execute("DELETE FROM profile_documents WHERE kind IN ('resume_data', ?)", (FRAMING_KIND,))
    db.commit()
    with pytest.raises(ResumeBundleError, match="no resume_framing document"):
        load_bundle(db)


def test_a_key_in_the_wrong_document_is_refused(db: sqlite3.Connection) -> None:
    facts, framing = example_documents()
    facts["bullet_pool"] = {"x": "Shipped a thing."}
    framing_candidate = framing["candidate"]
    framing_candidate["email"] = "someone@example.test"
    store_documents(db, facts, framing)
    with pytest.raises(ResumeBundleError) as refused:
        load_bundle(db)
    message = str(refused.value)
    assert "resume_facts: bullet_pool belongs in resume_framing" in message
    assert "resume_framing: candidate.email belongs in resume_facts" in message


def test_framing_role_must_name_a_facts_role(db: sqlite3.Connection) -> None:
    facts, framing = example_documents()
    roles = framing["roles"]
    roles.append({"id": "no-such-role", "bullet_count": 2})
    store_documents(db, facts, framing)
    with pytest.raises(ResumeBundleError, match=r"resume_framing: roles\[\d+\] names unknown role"):
        load_bundle(db)


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


def test_the_split_write_is_all_or_nothing(db: sqlite3.Connection) -> None:
    """A failure after the first document is written leaves the store as it
    was: here the rename collides with a row already holding its target."""
    put_document(db, "resume_data", "resume-content.json", "json", "{}")
    put_document(db, PRESPLIT_KIND, "resume-content.json", "json", "{}")
    before = documents(db)
    with pytest.raises(sqlite3.IntegrityError):
        put_documents_and_rekind(
            db,
            [(FACTS_KIND, "resume-facts.json", "json", "{}")],
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


def test_letters_read_the_never_claim_list_from_facts(db: sqlite3.Connection) -> None:
    store_documents(db, {"forbidden_phrases": ["world-class expert"]}, {})
    assert load_forbidden_phrases(db) == ("world-class expert",)
    db.execute("DELETE FROM profile_documents")
    db.commit()
    assert load_forbidden_phrases(db) == ()


# --- harrier profile split-resume ---------------------------------------------


def test_split_resume_dry_run_writes_nothing(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
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
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
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
    assert load_bundle(db) == parse_bundle(raw)
    assert stored[(FACTS_KIND, "resume-facts.json")].startswith('{\n  "')
    capsys.readouterr()

    assert main(["profile", "split-resume", "--write"]) == 0
    assert "already split" in capsys.readouterr().out
    assert {(kind, name) for kind, name, _ in documents(db)} == set(stored)


def test_split_resume_refuses_an_unplaced_key(
    db: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
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
    ],
)
def test_split_resume_failure_modes(
    db: sqlite3.Connection,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    code: int,
    message: str,
) -> None:
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
    before = documents(db)
    assert main(["profile", "split-resume", "--write"]) == code
    assert message in capsys.readouterr().err
    assert documents(db) == before


def test_split_resume_is_refused_on_another_track(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tracks", "add", ACADEMIC, "--kind", "academic", "--label", "Lab search"]) == 0
    capsys.readouterr()
    assert main(["--track", ACADEMIC, "profile", "split-resume"]) == 2
