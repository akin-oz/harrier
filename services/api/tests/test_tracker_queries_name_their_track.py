"""Every SQL literal on `jobs` or `job_events` names its track (spec 092).

A source-reading test, which `.ai/rules/review-response.md` calls a last
resort: it breaks on a wrapped line and can pass for the wrong reason. Both
are addressed. Whitespace is normalised before a literal is read, so a
wrapped literal reads as one line; and the guard is held to a fixture that
proves it fails, both on a literal that lacks the predicate and on an
exemption that names a function that does not exist. The behavioral tests
in `test_track_isolation.py` prove the readers that exist today; this one
reads the reader written next month, before it has a test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

TABLE_REFERENCE = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+(jobs|job_events)\b", re.IGNORECASE
)
EVENT_INSERT = re.compile(r"\bINSERT\s+INTO\s+job_events\b", re.IGNORECASE)
NAMES_TRACK = re.compile(r"\btrack_id\b")

# Reads that may see every track, each as (file under src, function). An
# exemption whose function is gone fails the guard, so none can outlive its
# reason.
EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        # url and external_key are unique across the whole file.
        ("harrier/tracker/store.py", "find_duplicate"),
        # The dedupe index feed, and nothing else.
        ("harrier/tracker/store.py", "all_tracks_dedupe_rows"),
        # A whole-database import; its rows land in track 1 by the column
        # default, and `--replace` empties the file.
        ("harrier/tracker/migrate_legacy.py", "migrate"),
        # Counts over the file: a check on the database, not a read of a
        # search.
        ("harrier/backup.py", "verify_database"),
        ("harrier/cutover.py", "tracker_check"),
        ("harrier/cutover.py", "verify"),
        ("harrier_api/app.py", "health"),
    }
)

# DDL, not a reader: the migrations create the tables the rule is about.
DDL_FILES: frozenset[str] = frozenset({"harrier/tracker/schema.py"})


def _literal_text(node: ast.AST) -> str | None:
    """The text of a string constant or the constant parts of an f-string,
    with whitespace normalised so a wrapped literal reads as one line."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return " ".join(node.value.split())
    if isinstance(node, ast.JoinedStr):
        parts = [
            value.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
            else " {} "
            for value in node.values
        ]
        return " ".join("".join(parts).split())
    return None


def _enclosing_function(tree: ast.Module, target: ast.AST) -> str:
    name = "<module>"
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    name = node.name
    return name


def unscoped_queries(src: Path, exempt: frozenset[tuple[str, str]]) -> list[str]:
    """Every problem: a literal on jobs or job_events that names no track and
    is not exempt, and every exemption whose function does not exist."""
    problems: list[str] = []
    defined: dict[str, set[str]] = {}
    for path in sorted(src.rglob("*.py")):
        relative = path.relative_to(src).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined[relative] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if relative in DDL_FILES:
            continue
        # The constant halves of an f-string are nodes of their own; read
        # the whole f-string once, never a half that lacks the predicate.
        halves = {
            id(value)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            for value in node.values
        }
        for node in ast.walk(tree):
            if id(node) in halves:
                continue
            text = _literal_text(node)
            if text is None or TABLE_REFERENCE.search(text) is None:
                continue
            if EVENT_INSERT.search(text) and not re.search(
                r"\b(?:FROM|JOIN|UPDATE|DELETE\s+FROM)\s+(jobs|job_events)\b", text, re.IGNORECASE
            ):
                continue  # an insert names a job_id resolved in scope
            if NAMES_TRACK.search(text):
                continue
            function = _enclosing_function(tree, node)
            if (relative, function) in exempt:
                continue
            problems.append(f"{relative}:{getattr(node, 'lineno', '?')} in {function}: {text[:80]}")
    for relative, function in sorted(exempt):
        if function not in defined.get(relative, set()):
            problems.append(f"exemption names {function} in {relative}, which does not exist")
    return problems


def test_every_tracker_query_names_its_track() -> None:
    problems = unscoped_queries(SRC, EXEMPT)
    assert not problems, "SQL on jobs or job_events that names no track:\n" + "\n".join(problems)


def test_the_guard_reads_a_known_number_of_literals() -> None:
    """A guard whose file set is empty has not run (spec 045's lesson)."""
    found = 0
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            text = _literal_text(node)
            if text is not None and TABLE_REFERENCE.search(text):
                found += 1
    assert found >= 10, found


@pytest.fixture()
def invented_source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "reader.py").write_text(
        "def scoped(conn, scope):\n"
        '    return conn.execute("SELECT * FROM jobs WHERE track_id = ?", (scope,))\n'
        "def wrapped(conn, scope):\n"
        "    return conn.execute(\n"
        '        "SELECT * FROM jobs "\n'
        '        "WHERE track_id = ? ORDER BY id",\n'
        "        (scope,),\n"
        "    )\n"
        "def halves(conn, scope, extra):\n"
        '    return conn.execute(f"SELECT * FROM jobs {extra} " "WHERE track_id = ?", (scope,))\n'
        "def forgetful(conn):\n"
        '    return conn.execute("SELECT * FROM jobs ORDER BY id")\n'
        "def records(conn, job_id):\n"
        '    conn.execute("INSERT INTO job_events (job_id) VALUES (?)", (job_id,))\n'
        "def counts(conn):\n"
        '    return conn.execute("SELECT COUNT(*) FROM jobs")\n',
        encoding="utf-8",
    )
    return src


def test_the_static_guard_fails_on_an_unscoped_query(invented_source: Path) -> None:
    exempt = frozenset({("pkg/reader.py", "counts")})
    problems = unscoped_queries(invented_source, exempt)
    assert len(problems) == 1, problems
    assert "forgetful" in problems[0]
    # The wrapped literal and the scoped one pass; the event insert is
    # exempt by shape; the count is exempt by name.
    assert "wrapped" not in problems[0] and "scoped" not in problems[0]

    # An exemption naming a function that does not exist is itself a problem.
    stale = frozenset({("pkg/reader.py", "counts"), ("pkg/reader.py", "vanished")})
    problems = unscoped_queries(invented_source, stale)
    assert any("vanished" in problem for problem in problems)
