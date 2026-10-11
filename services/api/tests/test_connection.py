"""The connection seam (spec 112).

The store errors, the store choice and the gate text belong to no one
driver, so they live in `harrier.connection` and `harrier.pgstore` keeps
only driver code. Where a class is defined is read from the class. Where a
caller imports it from is not visible at run time, since both modules hand
out the same object, so that half reads the import statements. It parses
them rather than matching text, and a fixture proves it finds a stale import.

`Connection` offers no `with` block over itself, and no module under `src`
reaches the sqlite3 connection underneath it. Both are read here too: the
first from the class, the second from the source, since an attribute access
leaves nothing to inspect at run time.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

import harrier.connection as connection
import harrier.pgstore as pgstore
from harrier.connection import Connection, StoreError, StoreTarget, store_target

API_ROOT = Path(__file__).resolve().parents[1]

# Every name spec 112 moves out of pgstore: Scope item 13, and what the
# amendment "what PR 1 built" adds (the URL variable, the scheme and the
# URL parser `store_target` needs).
MOVED = frozenset(
    {
        "StoreError",
        "StoreUrlError",
        "StoreDriverMissing",
        "StoreConnectionError",
        "StoreVersionError",
        "StoreMigrationRefused",
        "StoreTarget",
        "store_target",
        "POSTGRES_NOT_YET",
        "URL_VARIABLE",
        "POSTGRES_SCHEME",
        "UrlParts",
        "url_parts",
    }
)

# Files each scan must read, each importing from `harrier.connection`. A scan
# that reads nothing finds nothing stale, so a wrong root would pass without
# this floor (post-merge review of PR #241).
KNOWN_SRC_IMPORTERS = ("src/harrier_cli/main.py", "src/harrier_api/app.py")
KNOWN_TEST_IMPORTERS = ("tests/test_store_cli.py",)


def _store_errors() -> set[type[StoreError]]:
    found: set[type[StoreError]] = set()
    pending: list[type[StoreError]] = [StoreError]
    while pending:
        current = pending.pop()
        found.add(current)
        pending.extend(current.__subclasses__())
    return found


def _package_of(path: Path) -> str:
    """The package a relative import in this file resolves against."""
    for root in (API_ROOT / "src", API_ROOT / "tests"):
        if path.is_relative_to(root):
            parts = path.relative_to(root).with_suffix("").parts
            # A package's `__init__` resolves against the package itself, any
            # other module against the package that holds it: both drop the
            # last part.
            return ".".join(parts[:-1])
    raise AssertionError(f"{path} is under neither src nor tests")


def _imported_module(node: ast.ImportFrom, package: str) -> str:
    """The absolute module an import names, a relative one resolved."""
    if node.level == 0:
        return node.module or ""
    parts = package.split(".") if package else []
    base = parts[: len(parts) - (node.level - 1)] if node.level - 1 <= len(parts) else []
    return ".".join([*base, *([node.module] if node.module else [])])


def pgstore_imports_of_moved_names(tree: ast.Module, package: str = "") -> list[str]:
    """Each moved name this module reaches through `harrier.pgstore`.

    `package` is the package the module sits in, so `from .pgstore import X`
    in `harrier` and `from ..pgstore import X` in `harrier.tracker` are read
    as the imports from `harrier.pgstore` they are.
    """
    found: list[str] = []
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _imported_module(node, package)
            if module == "harrier.pgstore":
                found += [alias.name for alias in node.names if alias.name in MOVED]
            elif module == "harrier":
                aliases |= {
                    alias.asname or alias.name for alias in node.names if alias.name == "pgstore"
                }
        elif isinstance(node, ast.Import):
            aliases |= {
                alias.asname
                for alias in node.names
                if alias.name == "harrier.pgstore" and alias.asname
            }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in MOVED:
            continue
        owner = node.value
        if (isinstance(owner, ast.Name) and owner.id in aliases) or (
            isinstance(owner, ast.Attribute)
            and owner.attr == "pgstore"
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "harrier"
        ):
            found.append(node.attr)
    return found


def sqlite_driver_uses(tree: ast.Module) -> list[int]:
    """The line of every read of `sqlite_driver` in this module."""
    return sorted(
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.expr) and _reads_driver(node)
    )


def _reads_driver(node: ast.expr) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "sqlite_driver"
    # `getattr(conn, "sqlite_driver")` reads it without an attribute node.
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "sqlite_driver"
    )


def _imports_from_connection(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "harrier.connection"
        for node in ast.walk(tree)
    )


def _parsed(roots: tuple[str, ...], known: tuple[str, ...]) -> dict[Path, ast.Module]:
    """Every Python file under `roots`, parsed, and proof the scan read files.

    Each file in `known` must be among them and must import from
    `harrier.connection`, so a root that points nowhere fails here instead
    of finding nothing.
    """
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for root in roots
        for path in sorted((API_ROOT / root).rglob("*.py"))
    }
    scanned = {path.relative_to(API_ROOT).as_posix(): tree for path, tree in trees.items()}
    for name in known:
        assert name in scanned, f"the scan did not read {name}"
        assert _imports_from_connection(scanned[name]), f"{name} does not import the seam"
    return trees


def moved_names_bound_locally(tree: ast.Module) -> list[str]:
    """Each MOVED name the module binds other than by importing it, under the
    same name, from `harrier.connection`, with the form that binds it.

    Read from the module's own scope: an assignment of any kind, a `def`, a
    `class`, an `import`, an import from another module or under another
    name, a `for` or `with` target, a walrus, a match capture, an `except`
    name, and a `global` declaration in a function or class body.
    """

    def from_the_seam(node: ast.ImportFrom, alias: ast.alias) -> bool:
        seam = (node.level == 0 and node.module == "harrier.connection") or (
            node.level == 1 and node.module == "connection"
        )
        return seam and alias.asname is None

    found: list[str] = []

    def bind(name: str, form: str) -> None:
        if name in MOVED:
            found.append(f"{name}: {form}")

    def visit(node: ast.AST, at_module: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if at_module:
                bind(node.name, type(node).__name__)
            for inner in ast.walk(node):
                if isinstance(inner, ast.Global):
                    for name in inner.names:
                        bind(name, "global")
            return
        if isinstance(node, ast.Lambda):
            return
        if at_module:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if not from_the_seam(node, alias):
                        bind(bound, f"from {'.' * node.level}{node.module or ''} import")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bind(alias.asname or alias.name.split(".")[0], "import")
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bind(node.id, "assignment")
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                bind(node.name, "match capture")
            elif isinstance(node, ast.MatchMapping) and node.rest:
                bind(node.rest, "match capture")
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, "except name")
        # A comprehension has its own scope, but a walrus inside it binds in
        # the enclosing one.
        for child in ast.iter_child_nodes(node):
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for inner in ast.walk(child):
                    if isinstance(inner, ast.NamedExpr):
                        bind(inner.target.id, "walrus")
                continue
            visit(child, at_module)

    visit(tree, True)
    return found


def test_store_neutral_names_live_in_connection() -> None:
    for error in _store_errors():
        assert error.__module__ == "harrier.connection", error
    assert StoreTarget.__module__ == "harrier.connection"
    assert store_target.__module__ == "harrier.connection"
    # No moved name is pgstore's own. `is` cannot show it for an interned
    # string: a pgstore-local `URL_VARIABLE = "HARRIER_DATABASE_URL"` is the
    # very object the seam holds (post-merge review of PR #244). So the
    # source is read: pgstore binds a moved name only by importing it from
    # the seam.
    source = API_ROOT / "src" / "harrier" / "pgstore.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    assert moved_names_bound_locally(tree) == []
    # Names pgstore has no use for are not in it at all: every moved name it
    # does not import from the seam.
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "harrier.connection"
        for alias in node.names
    }
    assert {"StoreError", "POSTGRES_SCHEME"} <= MOVED - imported
    for name in sorted(MOVED - imported):
        assert not hasattr(pgstore, name), name
    # A moved name pgstore does hold is the object it imported from the seam,
    # never a pgstore-local one shadowing it.
    for name in sorted(MOVED):
        if hasattr(pgstore, name):
            assert getattr(pgstore, name) is getattr(connection, name), name

    stale: list[str] = []
    trees = _parsed(("src", "tests"), KNOWN_SRC_IMPORTERS + KNOWN_TEST_IMPORTERS)
    for path, tree in trees.items():
        names = pgstore_imports_of_moved_names(tree, _package_of(path))
        stale += [f"{path.relative_to(API_ROOT)}: {name}" for name in names]
    assert stale == []


def test_the_local_binding_check_finds_every_form() -> None:
    """The check above fails on each way a module binds a name of its own,
    and passes the one import it allows (post-merge review of PR #244)."""
    tree = ast.parse(
        "from harrier.connection import URL_VARIABLE, url_parts\n"
        "from .connection import StoreError\n"
        'POSTGRES_SCHEME = "postgresql"\n'
        "StoreTarget: type = object\n"
        "POSTGRES_NOT_YET += ''\n"
        "def store_target(): ...\n"
        "async def UrlParts(): ...\n"
        "class StoreUrlError(Exception): ...\n"
        "from harrier.other import StoreVersionError\n"
        "from harrier.connection import StoreDriverMissing as StoreConnectionError\n"
        "import StoreMigrationRefused\n"
        "for StoreDriverMissing in (): ...\n"
        "def later():\n"
        "    global URL_VARIABLE\n"
        "    URL_VARIABLE = 'x'\n"
        "[(url_parts := 1) for _ in ()]\n"
    )
    assert sorted(moved_names_bound_locally(tree)) == [
        "POSTGRES_NOT_YET: assignment",
        "POSTGRES_SCHEME: assignment",
        "StoreConnectionError: from harrier.connection import",
        "StoreDriverMissing: assignment",
        "StoreMigrationRefused: import",
        "StoreTarget: assignment",
        "StoreUrlError: ClassDef",
        "StoreVersionError: from harrier.other import",
        "URL_VARIABLE: global",
        "UrlParts: AsyncFunctionDef",
        "store_target: FunctionDef",
        "url_parts: walrus",
    ]


def test_the_import_check_finds_every_way_to_reach_pgstore() -> None:
    """The guard above fails on each form it reads."""
    tree = ast.parse(
        "from harrier.pgstore import StoreError, migrate_postgres\n"
        "from harrier import pgstore\n"
        "import harrier.pgstore as store\n"
        "import harrier.pgstore\n"
        "pgstore.StoreVersionError\n"
        "store.store_target\n"
        "harrier.pgstore.POSTGRES_NOT_YET\n"
        "pgstore.migrate_postgres\n"
    )
    assert sorted(pgstore_imports_of_moved_names(tree)) == [
        "POSTGRES_NOT_YET",
        "StoreError",
        "StoreVersionError",
        "store_target",
    ]


def test_the_import_check_resolves_relative_imports() -> None:
    """A relative import of pgstore is read against the file's package."""
    in_harrier = ast.parse(
        "from .pgstore import StoreError, migrate_postgres\n"
        "from . import pgstore as store\n"
        "store.url_parts\n"
        "from .connection import StoreVersionError\n"
    )
    assert sorted(pgstore_imports_of_moved_names(in_harrier, "harrier")) == [
        "StoreError",
        "url_parts",
    ]
    in_tracker = ast.parse(
        "from ..pgstore import store_target\nfrom .. import pgstore\npgstore.UrlParts\n"
    )
    assert sorted(pgstore_imports_of_moved_names(in_tracker, "harrier.tracker")) == [
        "UrlParts",
        "store_target",
    ]
    # The same import one package down names a module that is not pgstore.
    assert pgstore_imports_of_moved_names(ast.parse("from .pgstore import StoreError\n"), "x") == []
    assert _package_of(API_ROOT / "src" / "harrier" / "tracker" / "store.py") == "harrier.tracker"
    assert _package_of(API_ROOT / "src" / "harrier" / "tracker" / "__init__.py") == (
        "harrier.tracker"
    )


def test_a_connection_offers_no_with_block() -> None:
    """`with conn:` commits on sqlite3 and closes on psycopg (spec 112).

    pyright refuses the block only while the class has no `__enter__`, so
    the class must never gain one, nor `__exit__`.
    """
    for name in ("__enter__", "__exit__", "__aenter__", "__aexit__"):
        assert not hasattr(Connection, name), name


def test_no_module_under_src_reaches_the_sqlite_driver() -> None:
    """`sqlite_driver` is for tests whose subject is SQLite itself (spec 112).

    Domain code that reached it would bypass the seam, and a `with` over it
    would bring back what `Connection` refuses.
    """
    uses: list[str] = []
    for path, tree in _parsed(("src",), KNOWN_SRC_IMPORTERS).items():
        uses += [f"{path.relative_to(API_ROOT)}:{line}" for line in sqlite_driver_uses(tree)]
    assert uses == []


def test_the_driver_check_finds_every_read() -> None:
    tree = ast.parse(
        "with conn.sqlite_driver:\n"
        "    pass\n"
        "conn.sqlite_driver.execute('SELECT 1')\n"
        "raw = getattr(conn, 'sqlite_driver')\n"
        "conn.execute('SELECT 1')\n"
        "sqlite_driver = 1\n"
    )
    assert sqlite_driver_uses(tree) == [1, 3, 4]


def test_a_transaction_rolls_back_on_error_and_commits_on_success(tmp_path: Path) -> None:
    """`transaction()` is what `with conn:` was on sqlite3 (spec 076, 112).

    An exception inside rolls the write back and leaves the connection open;
    a clean exit commits, so another connection to the file sees the row.
    """
    path = tmp_path / "seam.db"
    setup = sqlite3.connect(path)
    setup.execute("CREATE TABLE notes (body TEXT)")
    setup.commit()
    setup.close()

    conn = Connection(sqlite3.connect(path))
    try:
        with pytest.raises(RuntimeError, match="refused"), conn.transaction():
            conn.execute("INSERT INTO notes (body) VALUES (?)", ("rolled back",))
            raise RuntimeError("refused")
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0

        with conn.transaction():
            conn.execute("INSERT INTO notes (body) VALUES (?)", ("kept",))
        assert not conn.in_transaction
        assert conn.execute("SELECT body FROM notes").fetchall() == [("kept",)]
    finally:
        conn.close()

    other = sqlite3.connect(path)
    try:
        assert other.execute("SELECT body FROM notes").fetchall() == [("kept",)]
    finally:
        other.close()
