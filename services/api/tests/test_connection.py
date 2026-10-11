"""The connection seam (spec 112).

The store errors, the store choice and the gate text belong to no one
driver, so they live in `harrier.connection` and `harrier.pgstore` keeps
only driver code. Where a class is defined is read from the class. Where a
caller imports it from is not visible at run time, since both modules hand
out the same object, so that half reads the import statements. It parses
them rather than matching text, and a fixture proves it finds a stale import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import harrier.pgstore as pgstore
from harrier.connection import StoreError, StoreTarget, store_target

API_ROOT = Path(__file__).resolve().parents[1]

# Every name spec 112 moves out of pgstore (Scope item 13).
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
    }
)


def _store_errors() -> set[type[StoreError]]:
    found: set[type[StoreError]] = set()
    pending: list[type[StoreError]] = [StoreError]
    while pending:
        current = pending.pop()
        found.add(current)
        pending.extend(current.__subclasses__())
    return found


def pgstore_imports_of_moved_names(tree: ast.Module) -> list[str]:
    """Each moved name this module reaches through `harrier.pgstore`."""
    found: list[str] = []
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "harrier.pgstore":
            found += [alias.name for alias in node.names if alias.name in MOVED]
        elif isinstance(node, ast.ImportFrom) and node.module == "harrier":
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


def test_store_neutral_names_live_in_connection() -> None:
    for error in _store_errors():
        assert error.__module__ == "harrier.connection", error
    assert StoreTarget.__module__ == "harrier.connection"
    assert store_target.__module__ == "harrier.connection"
    # Names pgstore has no use for are not in it at all.
    for name in ("StoreError", "StoreUrlError", "StoreTarget", "store_target", "POSTGRES_NOT_YET"):
        assert not hasattr(pgstore, name), name

    stale: list[str] = []
    for root in ("src", "tests"):
        for path in sorted((API_ROOT / root).rglob("*.py")):
            names = pgstore_imports_of_moved_names(ast.parse(path.read_text(encoding="utf-8")))
            stale += [f"{path.relative_to(API_ROOT)}: {name}" for name in names]
    assert stale == []


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
