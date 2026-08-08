"""Profile document import/export round-trip (spec 004). Synthetic content only."""

from pathlib import Path

from harrier.db import connect
from harrier.profile import export_to, get_document, import_from, list_documents

SYNTHETIC_FILES: dict[str, str] = {
    "config/candidate.json": '{"name": "Pat Placeholder", "remote_only": true}\n',
    "config/resume-truth-source.md": "# Truth\n\nPat built a synthetic thing.\n",
    "config/application-profile.json": '{"core_positioning": "synthetic"}\n',
    "interview-prep/story-bank.md": "## Story\n\nSynthetic STAR story.\n",
}


def _make_old_repo(root: Path) -> None:
    for rel, content in SYNTHETIC_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_import_reports_present_and_missing(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    _make_old_repo(old_root)
    conn = connect(tmp_path / "t.db")
    imported, missing = import_from(conn, old_root)

    assert any(line.startswith("candidate/candidate.json") for line in imported)
    assert any(line.startswith("interview_prep/story-bank.md") for line in imported)
    assert "config/resume-candidate-data.json" in missing  # not present in fixture
    assert len(list_documents(conn)) == len(SYNTHETIC_FILES)


def test_export_is_byte_identical(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    _make_old_repo(old_root)
    conn = connect(tmp_path / "t.db")
    import_from(conn, old_root)

    out = tmp_path / "export"
    written = export_to(conn, out)
    assert len(written) == len(SYNTHETIC_FILES)

    assert (out / "candidate" / "candidate.json").read_text(encoding="utf-8") == SYNTHETIC_FILES[
        "config/candidate.json"
    ]
    assert (out / "interview_prep" / "story-bank.md").read_text(
        encoding="utf-8"
    ) == SYNTHETIC_FILES["interview-prep/story-bank.md"]


def test_reimport_updates_in_place(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    _make_old_repo(old_root)
    conn = connect(tmp_path / "t.db")
    import_from(conn, old_root)

    (old_root / "config/candidate.json").write_text('{"name": "Pat Two"}\n', encoding="utf-8")
    import_from(conn, old_root)

    assert get_document(conn, "candidate", "candidate.json") == '{"name": "Pat Two"}\n'
    assert len(list_documents(conn)) == len(SYNTHETIC_FILES)


def test_crlf_content_round_trips_byte_identically(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    old_root.joinpath("config").mkdir(parents=True)
    crlf = '{"name": "Pat Placeholder"}\r\n{"line": "two"}\r\n'
    with (old_root / "config/candidate.json").open("w", encoding="utf-8", newline="") as handle:
        handle.write(crlf)
    conn = connect(tmp_path / "t.db")
    import_from(conn, old_root)

    out = tmp_path / "export"
    export_to(conn, out)
    exported = out / "candidate" / "candidate.json"
    assert exported.read_bytes() == crlf.encode("utf-8")
