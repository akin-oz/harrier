"""A spec that loses a section fails rather than merging (spec 041).

Spec 006 shipped with its `## Problem` gone, and both of its claims went with
it. The spec gate reads frontmatter and the artifact check reads compiled
output; neither reads structure, so the damage merged unnoticed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK = REPO_ROOT / "scripts" / "check_spec_structure.py"
SPECS = Path(__file__).resolve().parents[3] / "specs"

WHOLE = """---
spec: 099
title: A whole spec
approved: yes
---

# Spec 099

## Problem

Something.

## Scope

Something.

## Acceptance criteria

- [ ] something

## Proof / origin

Somewhere.

## Out of scope

Something else.
"""


def run(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), str(directory)], capture_output=True, text=True, check=False
    )


def test_a_damaged_heading_fails(tmp_path: Path) -> None:
    """The fixture is spec 006's actual damage: the heading collapsed into
    the prose below it, so the section is gone but the words remain."""
    (tmp_path / "099-damaged.md").write_text(
        WHOLE.replace("## Problem\n\nSomething.", "Something."), encoding="utf-8"
    )
    result = run(tmp_path)
    assert result.returncode == 1
    assert "missing '## Problem'" in result.stdout


def test_a_whole_spec_passes(tmp_path: Path) -> None:
    (tmp_path / "099-whole.md").write_text(WHOLE, encoding="utf-8")
    assert run(tmp_path).returncode == 0


# Declared here rather than imported from the implementation. The previous
# version looped over `module.REQUIRED`, so removing a heading from the
# required set removed it from the test too and the suite stayed green: a test
# that could not fail, whose docstring claimed the opposite (spec 045).
EXPECTED_HEADINGS = (
    "## Problem",
    "## Scope",
    "## Acceptance criteria",
    "## Proof / origin",
    "## Out of scope",
)


def test_the_required_set_is_the_one_this_suite_expects() -> None:
    """Shrinking REQUIRED is now a failure here rather than a silently
    smaller test.

    Read out of the source rather than imported, because importing it is what
    made the old test unable to fail.
    """
    source = CHECK.read_text(encoding="utf-8")
    block = re.search(r"REQUIRED[^=]*=\s*\((.*?)\)", source, re.DOTALL)
    assert block is not None, "check_spec_structure.py no longer declares REQUIRED"
    declared = tuple(re.findall(r'"([^"]+)"', block.group(1)))
    assert declared == EXPECTED_HEADINGS


@pytest.mark.parametrize("heading", EXPECTED_HEADINGS)
def test_every_required_heading_is_checked(heading: str, tmp_path: Path) -> None:
    """Each one individually, so a heading cannot be quietly dropped from the
    required set without a test noticing."""
    damaged = WHOLE.replace(f"{heading}\n", "")
    (tmp_path / "099-one.md").write_text(damaged, encoding="utf-8")
    result = run(tmp_path)
    assert result.returncode == 1, f"removing {heading} did not fail the check"
    assert f"missing '{heading}'" in result.stdout


def test_a_spec_may_add_sections(tmp_path: Path) -> None:
    """The check asserts the required headings are present, not that no
    others are. A rigid version would forbid the amendment sections this
    repository uses to record decisions."""
    (tmp_path / "099-extra.md").write_text(
        WHOLE + "\n## What the implementation decided\n\nThings.\n", encoding="utf-8"
    )
    assert run(tmp_path).returncode == 0


def test_a_heading_inside_prose_does_not_count(tmp_path: Path) -> None:
    """Matched at the start of a line. Mentioning `## Problem` in a sentence
    is not having the section."""
    (tmp_path / "099-mention.md").write_text(
        WHOLE.replace("## Problem\n\nSomething.", "The spec had no ## Problem section."),
        encoding="utf-8",
    )
    assert run(tmp_path).returncode == 1


def test_the_committed_specs_all_carry_their_headings() -> None:
    """Including spec 006, which is what this spec repaired."""
    result = run(SPECS)
    assert result.returncode == 0, result.stdout


# --- citations ---------------------------------------------------------------

# A Python test cited in a code span: a bare `test_x`, a qualified
# `path/to/file.py::test_x`, or a continuation `::test_x`, which names its file
# only by where it stands. Qualifying the references in specs 044 and 045 made
# them invisible to the bare-symbol pattern (review of PR #49), and
# continuations stayed unread until spec 045's amendment of 2026-10-06, by
# which time spec 047 cited a renamed test that way.
PYTHON_CITATION = re.compile(
    r"`(?:(?P<path>[A-Za-z0-9_./-]+\.py)?::)?(?P<symbol>test_[A-Za-z0-9_]+)`"
)

# A web test's name runs to the quote that closes the one it opened with.
# Stopping at any quote mark cut "a gate's refusal" short at its apostrophe.
WEB_TEST = re.compile(r"""(?<![\w.$])(?:it|test)\(\s*(["'`])((?:(?!\1)[^\\]|\\.)*)\1""")

# A web test file, as a path from the repository root or as a bare file name.
WEB_FILE = r"[A-Za-z0-9_./-]*[A-Za-z0-9_]\.test\.tsx?(?![A-Za-z0-9_])"

# A code span. Spans wrap across lines but never across a blank line, as in
# markdown.
CODE_SPAN = r"(?P<ticks>`+)(?P<code>(?:[^\n]|\n(?![ \t]*\n))+?)(?<!`)(?P=ticks)(?!`)"

# A code span, a double-quoted string, a web test file joined to `::`, or `::`
# joined to a double-quoted name. A quoted string wraps the way a span does.
SPEC_TOKEN = re.compile(
    CODE_SPAN
    + r'|"(?P<quoted>(?:[^"\n]|\n(?![ \t]*\n))*)"'
    + rf"|(?P<bare>{WEB_FILE})(?=::)"
    + r'|(?P<colons>::)(?=")'
)
FENCED = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
ONLY_FILE = re.compile(WEB_FILE)
JOINED = re.compile(rf"(?P<file>{WEB_FILE})::(?P<name>.+)", re.DOTALL)
CONTINUED = re.compile(r"::(?P<name>.+)", re.DOTALL)
LIST_OPENS = re.compile(r"\s*[:,]\s*(?:planned\s+)?")
LIST_GOES_ON = re.compile(r"\s*(?:,\s*(?:and\s+)?|and\s+)(?:planned\s+)?")
NAME_IN = re.compile(r"\s+in\s+")
PLANNED = re.compile(r"\bplanned\s+\Z")

# A Python test named outside a code span is a whole word, and planned may
# stand before it or before the path and `::` joined to it.
WORD = re.compile(r"[A-Za-z0-9_]+")
PLANNED_PYTHON = re.compile(r"\bplanned\s+(?:(?:[A-Za-z0-9_./-]+\.py)?::)?\Z")


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int


@dataclass(frozen=True)
class WebCitation:
    file: str | None  # None: a continuation with no file before it
    name: str
    planned: bool
    at: int


def one_line(text: str) -> str:
    """A name as it compares: a wrapped line and its indent are one space."""
    return " ".join(text.split())


def is_name(token: Token) -> bool:
    """A double-quoted string, or a code span holding a space. A Python
    symbol and a path never hold one, so neither reads as a web test."""
    if token.kind == "quoted":
        return token.text != ""
    return (
        token.kind == "code"
        and " " in token.text
        and JOINED.fullmatch(token.text) is None
        and CONTINUED.fullmatch(token.text) is None
    )


def web_citations(text: str) -> tuple[list[WebCitation], list[Token]]:
    """Every web test a spec cites, in the five shapes specs use, and every
    code span or quoted string that no citation read.

    The shapes are listed in spec 045's amendment of 2026-10-06. Fenced code
    blocks are blanked first, keeping their lines, so an example is not a
    citation and line numbers still match the file.
    """
    text = FENCED.sub(lambda block: re.sub(r"[^\n]", " ", block.group(0)), text)
    tokens: list[Token] = []
    for match in SPEC_TOKEN.finditer(text):
        kind = next(k for k in ("code", "quoted", "bare", "colons") if match[k] is not None)
        tokens.append(Token(kind, one_line(match[kind]), match.start(), match.end()))

    def planned(at: int) -> bool:
        return PLANNED.search(text[max(0, at - 40) : at]) is not None

    def gap(before: int, after: int) -> str:
        return text[tokens[before].end : tokens[after].start]

    found: list[WebCitation] = []
    read: set[int] = set()
    last: str | None = None
    for i, token in enumerate(tokens):
        if token.kind == "bare":
            last = token.text
        elif token.kind == "colons":
            # ::"name", joined to the bare file before it or continuing one.
            follows = i + 1 < len(tokens) and tokens[i + 1].kind == "quoted"
            if follows and gap(i, i + 1) == "":
                after_file = i > 0 and tokens[i - 1].kind == "bare" and gap(i - 1, i) == ""
                begins = tokens[i - 1].start if after_file else token.start
                name = tokens[i + 1]
                found.append(WebCitation(last, name.text, planned(begins), name.start))
                read.add(i + 1)
        elif token.kind != "code":
            continue
        elif joined := JOINED.fullmatch(token.text):
            last = joined["file"]
            found.append(WebCitation(last, joined["name"], planned(token.start), token.start))
        elif (continued := CONTINUED.fullmatch(token.text)) and " " in continued["name"]:
            found.append(WebCitation(last, continued["name"], planned(token.start), token.start))
        elif ONLY_FILE.fullmatch(token.text):
            last = token.text
            # `file`: "name", "name" and "name"
            j, separator = i + 1, LIST_OPENS
            while j < len(tokens) and is_name(tokens[j]) and separator.fullmatch(gap(j - 1, j)):
                name = tokens[j]
                found.append(WebCitation(last, name.text, planned(name.start), name.start))
                read.add(j)
                j, separator = j + 1, LIST_GOES_ON
            # "name" in `file`
            before = i - 1
            if (
                before >= 0
                and before not in read
                and is_name(tokens[before])
                and NAME_IN.fullmatch(gap(before, i))
            ):
                name = tokens[before]
                found.append(WebCitation(last, name.text, planned(name.start), name.start))
                read.add(before)
    unread = [t for k, t in enumerate(tokens) if k not in read and t.kind in ("code", "quoted")]
    return found, unread


def web_test_names(source: Path) -> dict[Path, set[str]]:
    """Every web test's name, by the file that defines it."""
    names: dict[Path, set[str]] = {}
    for path in sorted(source.rglob("*.test.ts*")):
        if path.suffix not in (".ts", ".tsx"):
            continue
        found: set[str] = set()
        for match in WEB_TEST.finditer(path.read_text(encoding="utf-8")):
            quote, raw = match.group(1), match.group(2)
            if quote == "`" and "${" in raw:
                continue  # built at run time: there is no fixed name to cite
            found.add(re.sub(r"\\(.)", r"\1", raw))
        names[path.resolve()] = found
    return names


def web_files(root: Path, cited: str, defined: dict[Path, set[str]]) -> list[Path]:
    """The web test files a citation's file can mean: a path from the
    repository root names one, and a bare file name may match several."""
    if "/" in cited:
        path = (root / cited).resolve()
        return [path] if path in defined else []
    return [path for path in defined if path.name == cited]


def python_problems(spec: str, text: str, root: Path, defined: set[str]) -> list[str]:
    seen = {(match["path"] or "", match["symbol"]) for match in PYTHON_CITATION.finditer(text)}
    problems: list[str] = []
    for path, symbol in sorted(seen):
        if symbol not in defined:
            problems.append(f"{spec}: {symbol}")
            continue
        if not path:
            continue
        # Specs write these both ways: from the repository root, and from
        # services/api, which is where pytest runs. Both resolve.
        candidates = [root / path, root / "services" / "api" / path]
        target = next((c for c in candidates if c.is_file()), None)
        if target is None:
            problems.append(f"{spec}: {path} does not exist")
        elif f"def {symbol}" not in target.read_text(encoding="utf-8"):
            problems.append(f"{spec}: {symbol} is not in {path}")
    return problems


def joined_to_a_path(text: str, word: re.Match[str]) -> bool:
    """A `/` or a `.` joins a word to a path or a file name.

    A full stop after a word joins it only when a character follows, as in an
    extension, so one that ends a sentence joins nothing.
    """
    before = text[word.start() - 1 : word.start()]
    after = text[word.end() : word.end() + 2]
    return (
        before in ("/", ".")
        or after.startswith("/")
        or re.fullmatch(r"\.[A-Za-z0-9_]", after) is not None
    )


def python_names_outside_code_spans(spec: str, text: str, defined: set[str]) -> list[str]:
    """Every existing Python test a spec names outside a code span, and every
    one still marked planned.

    Only a code span is read as a Python citation, so a name anywhere else
    would go unread until its test was renamed (spec 045's amendment on Python
    tests named outside a code span). Fenced code blocks and code spans are
    blanked, keeping their lines, so an example is not a citation and line
    numbers still match the file.
    """

    def blank(found: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", found.group(0))

    text = FENCED.sub(blank, text)
    prose = re.sub(CODE_SPAN, blank, text)

    def where(at: int) -> str:
        line = text.count("\n", 0, at) + 1
        return f"{spec}:{line}"

    def planned(at: int) -> bool:
        # The word, then whitespace and perhaps a path: inside 200 characters.
        return PLANNED_PYTHON.search(text[max(0, at - 200) : at]) is not None

    problems: list[str] = []
    for word in WORD.finditer(prose):
        name = word.group(0)
        if name not in defined or joined_to_a_path(prose, word):
            continue
        if planned(word.start()):
            reason = "is marked planned but exists"
        else:
            reason = "is a Python test named outside a code span"
        problems.append(f"{where(word.start())}: {name} {reason}")
    # Planned before a code span: the span is read as a citation, so once its
    # test exists the word has outlived its reason.
    for citation in PYTHON_CITATION.finditer(text):
        name = citation["symbol"]
        if name in defined and planned(citation.start()):
            problems.append(f"{where(citation.start())}: {name} is marked planned but exists")
    return problems


def web_problems(spec: str, text: str, root: Path, defined: dict[Path, set[str]]) -> list[str]:
    anywhere = {name for names in defined.values() for name in names}
    found, unread = web_citations(text)

    def where(at: int) -> str:
        line = text.count("\n", 0, at) + 1
        return f"{spec}:{line}"

    problems: list[str] = []
    for citation in found:
        name = f'"{citation.name}"'
        if citation.planned:
            # Exempt while no test has the name. Once one does, the marker
            # would exempt the citation from that test's next rename.
            if citation.name in anywhere:
                problems.append(f"{where(citation.at)}: {name} is marked planned but exists")
            continue
        if citation.file is None:
            problems.append(f"{where(citation.at)}: {name} follows no web test file")
            continue
        files = web_files(root, citation.file, defined)
        if not files:
            problems.append(f"{where(citation.at)}: {citation.file} is not a web test file")
        elif len(files) > 1:
            problems.append(f"{where(citation.at)}: {citation.file} is more than one web test file")
        elif citation.name not in defined[files[0]]:
            problems.append(f"{where(citation.at)}: {name} is not a test in {citation.file}")
    for token in unread:
        if token.text in anywhere:
            name = f'"{token.text}"'
            problems.append(f"{where(token.start)}: {name} is a web test quoted with no file")
    return problems


def unproven_citations(root: Path) -> list[str]:
    """Every test citation in `root/specs` that proves nothing, and why.

    `root` is a repository: specs, Python tests under `services/api/tests`,
    web tests under `apps/web/src`. The suite runs this over this repository
    and over small invented ones.
    """
    python: set[str] = set()
    for path in (root / "services" / "api" / "tests").rglob("*.py"):
        python.update(re.findall(r"def (test_[A-Za-z0-9_]+)", path.read_text(encoding="utf-8")))
    web = web_test_names(root / "apps" / "web" / "src")
    problems: list[str] = []
    for spec in sorted((root / "specs").rglob("*.md")):
        text = spec.read_text(encoding="utf-8")
        problems += python_problems(spec.name, text, root, python)
        problems += python_names_outside_code_spans(spec.name, text, python)
        problems += web_problems(spec.name, text, root, web)
    return problems


def test_every_test_a_spec_names_actually_exists() -> None:
    """A spec's acceptance table is only worth what its proofs are worth.

    Three specs cited five test symbols that did not exist, and one of them
    was the sole support for a criterion whose whole argument was "asserted by
    the two sink tests above" (spec 045). A renamed test breaks the proof
    silently, because nothing reads these names but a person. Web tests too:
    until spec 045's amendment of 2026-10-06 no web citation was read, and
    spec 047 cited a renamed one.
    """
    problems = unproven_citations(REPO_ROOT)
    assert not problems, (
        "spec citations that prove nothing (the shapes a web citation takes are in"
        " spec 045's amendment of 2026-10-06):\n" + "\n".join(problems)
    )


def repository(tmp_path: Path, spec: str) -> Path:
    """An invented repository: one spec, and the tests it may cite.

    Two web test files, so a name can sit in the wrong one, and a name holding
    an apostrophe, which the old name collection cut short.
    """
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "099-cites.md").write_text(spec, encoding="utf-8")
    pages = tmp_path / "apps" / "web" / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "Thing.test.tsx").write_text(
        'test("a thing happens", () => {});\ntest("the page\'s title is shown", () => {});\n',
        encoding="utf-8",
    )
    (pages / "Other.test.tsx").write_text(
        'test("only the other page has this", () => {});\n', encoding="utf-8"
    )
    python = tmp_path / "services" / "api" / "tests"
    python.mkdir(parents=True)
    (python / "test_here.py").write_text("def test_here() -> None:\n    pass\n", encoding="utf-8")
    return tmp_path


# The five shapes a spec cites a web test in, each with a slot for the name.
# The continuations follow a citation of a test that exists, so only the slot
# can fail.
WEB_SHAPES = {
    "file and name in a code span": "`apps/web/src/pages/Thing.test.tsx::{name}`",
    "file and name outside a code span": 'Thing.test.tsx::"{name}"',
    "continuation in a code span": "`Thing.test.tsx::a thing happens`, `::{name}`",
    "continuation outside a code span": 'Thing.test.tsx::"a thing happens", ::"{name}"',
    "names after the file": '`Thing.test.tsx`: "a thing happens", "{name}"',
    "names in code spans after the file": "`Thing.test.tsx`, `a thing happens` and `{name}`",
    "a name before the file": '"{name}" in `Thing.test.tsx`',
}


@pytest.mark.parametrize("shape", WEB_SHAPES.values(), ids=list(WEB_SHAPES))
def test_a_web_citation_naming_no_test_fails(shape: str, tmp_path: Path) -> None:
    """Every shape is read. Before spec 045's amendment of 2026-10-06 none
    was, and spec 047 went on citing a web test an earlier commit renamed."""
    spec = f"- [x] it works ({shape.format(name='a test that was renamed')})\n"
    problems = unproven_citations(repository(tmp_path, spec))
    assert len(problems) == 1, problems
    assert '"a test that was renamed" is not a test in' in problems[0]


@pytest.mark.parametrize(
    "name",
    ["the page's title is shown", "the page's title\n      is shown"],
    ids=["one line", "wrapped"],
)
@pytest.mark.parametrize("shape", WEB_SHAPES.values(), ids=list(WEB_SHAPES))
def test_a_web_citation_naming_a_real_test_passes(shape: str, name: str, tmp_path: Path) -> None:
    """A name is read whole through its apostrophe, and a name wrapped across
    lines compares as one line, in every shape."""
    spec = f"- [x] it works ({shape.format(name=name)})\n"
    assert unproven_citations(repository(tmp_path, spec)) == []


def test_a_web_name_must_be_in_the_file_it_names(tmp_path: Path) -> None:
    """Two files can hold tests of the same name, so a test elsewhere does
    not prove a citation of this file."""
    spec = (
        'Wrong: "only the other page has this" in `Thing.test.tsx`.\n'
        'Right: "only the other page has this" in `Other.test.tsx`.\n'
    )
    problems = unproven_citations(repository(tmp_path, spec))
    assert problems == [
        '099-cites.md:1: "only the other page has this" is not a test in Thing.test.tsx'
    ]


@pytest.mark.parametrize(
    ("citation", "reason"),
    [
        ('"a thing happens" in `apps/web/src/pages/Gone.test.tsx`', "is not a web test file"),
        ('"a thing happens" in `Gone.test.tsx`', "is not a web test file"),
        ('"a thing happens" in `Thing.test.tsx`', "is more than one web test file"),
        ("`::a thing happens`", "follows no web test file"),
    ],
    ids=["unknown path", "unknown file name", "file name held twice", "continuation first"],
)
def test_a_web_citation_resolves_to_exactly_one_file(
    citation: str, reason: str, tmp_path: Path
) -> None:
    root = repository(tmp_path, f"Proof: {citation}.\n")
    # A second Thing.test.tsx, so the bare file name matches two files.
    elsewhere = root / "apps" / "web" / "src" / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Thing.test.tsx").write_text(
        'test("a thing happens", () => {});\n', encoding="utf-8"
    )
    problems = unproven_citations(root)
    assert len(problems) == 1, problems
    assert reason in problems[0]


@pytest.mark.parametrize(
    ("citation", "reason"),
    [
        ('planned "a test not written yet" in `Thing.test.tsx`', None),
        ('planned "a test not written yet" in `apps/web/src/pages/New.test.tsx`', None),
        ("planned `Thing.test.tsx::a test not written yet`", None),
        ('`Thing.test.tsx`: "a thing happens", planned "a test not written yet"', None),
        ('`Thing.test.tsx`: planned "a thing happens"', "is marked planned but exists"),
    ],
    ids=["before a name", "in a file not written yet", "before a code span", "in a list", "stale"],
)
def test_planned_exempts_a_web_test_only_until_it_exists(
    citation: str, reason: str | None, tmp_path: Path
) -> None:
    """Planned marks a test the implementing change will write (commit
    851c7fa). Once the test exists the word must go: left in place it would
    exempt the citation from that test's next rename."""
    problems = unproven_citations(repository(tmp_path, f"- [ ] it will work ({citation})\n"))
    if reason is None:
        assert problems == []
    else:
        assert len(problems) == 1, problems
        assert reason in problems[0]


@pytest.mark.parametrize(
    ("text", "fails"),
    [
        ('The proof is "a thing happens", which fails without the fix.', True),
        ("The proof is `a thing happens`.", True),
        ('Still planned "a thing happens", which fails without the fix.', True),
        ('The page says "a phrase no test has".', False),
        ('```text\n"a thing happens"\n```', False),
    ],
    ids=["double quotes", "code span", "planned", "not a test name", "fenced example"],
)
def test_a_quoted_web_test_name_with_no_file_fails(text: str, fails: bool, tmp_path: Path) -> None:
    """A citation in no shape the check reads would stay unread until its
    test was renamed. Only the exact name of a test that exists counts, so a
    quoted phrase never fails."""
    problems = unproven_citations(repository(tmp_path, text + "\n"))
    if fails:
        assert problems == ['099-cites.md:1: "a thing happens" is a web test quoted with no file']
    else:
        assert problems == []


def test_a_python_continuation_is_checked(tmp_path: Path) -> None:
    """`::test_x` cites a test in a file named before it. The pattern skipped
    it, and spec 047 cited a renamed test that way."""
    spec = "(`services/api/tests/test_here.py::test_here`, `::test_here`, `::test_gone`)\n"
    assert unproven_citations(repository(tmp_path, spec)) == ["099-cites.md: test_gone"]


OUTSIDE = "test_here is a Python test named outside a code span"


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("The proof is test_here, which fails without the fix.", f"099-cites.md:1: {OUTSIDE}"),
        ("The proof is test_here.", f"099-cites.md:1: {OUTSIDE}"),
        ("The proof is services/api/tests/test_here.py::test_here.", f"099-cites.md:1: {OUTSIDE}"),
        ("A first line.\n\nThe proof is test_here.", f"099-cites.md:3: {OUTSIDE}"),
        ("The proof is `test_here`.", None),
        ("The proof is in test_here.py.", None),
        ("The proof is under services/api/tests/test_here", None),
        ("Its fixtures are in test_here/fixtures.json.", None),
        ("The module is tests.test_here.", None),
        ("The value is latest_here.", None),
        ("The proof was test_here_and_more.", None),
        ("```text\nThe proof is test_here.\n\nA code span stops at a blank line.\n```", None),
        ("The proof was test_gone, removed with its rule.", None),
    ],
    ids=[
        "prose",
        "before a full stop",
        "after its path",
        "on a later line",
        "in a code span",
        "a file name",
        "the end of a path",
        "a directory",
        "a dotted path",
        "inside a longer word",
        "a longer name",
        "a fenced example",
        "a removed test",
    ],
)
def test_a_python_test_named_outside_a_code_span_fails(
    text: str, problem: str | None, tmp_path: Path
) -> None:
    """A Python citation outside a code span would stay unread until its test
    was renamed. Only the whole name of a test that exists counts, and never
    as part of a path, a file name or an example. The fixture's test shares
    its file's name, so a file name that tripped the rule would fail here."""
    problems = unproven_citations(repository(tmp_path, text + "\n"))
    assert problems == ([] if problem is None else [problem])


@pytest.mark.parametrize(
    ("citation", "problem"),
    [
        ("planned test_not_written_yet", None),
        ("planned services/api/tests/test_new.py::test_not_written_yet", None),
        ("planned test_here", "099-cites.md:1: test_here is marked planned but exists"),
        (
            "planned services/api/tests/test_here.py::test_here",
            "099-cites.md:1: test_here is marked planned but exists",
        ),
        ("planned\n      test_here", "099-cites.md:2: test_here is marked planned but exists"),
        ("planned `test_here`", "099-cites.md:1: test_here is marked planned but exists"),
        ("planned `test_not_written_yet`", "099-cites.md: test_not_written_yet"),
    ],
    ids=[
        "before a name",
        "before a path",
        "stale before a name",
        "stale before a path",
        "stale across a line",
        "stale before a code span",
        "in a code span before its test",
    ],
)
def test_planned_exempts_a_python_test_only_until_it_exists(
    citation: str, problem: str | None, tmp_path: Path
) -> None:
    """Planned keeps a test's name out of a code span until the change that
    writes the test, as spec 082 did. Once the test exists the word has
    outlived its reason wherever it stands, and a name it kept out of a code
    span would go unread."""
    problems = unproven_citations(repository(tmp_path, f"- [ ] it will work ({citation})\n"))
    assert problems == ([] if problem is None else [problem])
