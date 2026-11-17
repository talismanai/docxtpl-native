"""Run the product test suite with docxtpl blocked.

Test modules that reach docxtpl (by importing it, naming it in a string such
as a patch target, or importing a local helper that does) are differential
tests: they compare this package with the pinned reference and need the
laboratory environment. Modules that import `tests/native_audit.py` and do not
reach docxtpl are native execution audits: they fail when a standard Python
algorithm runs instead of the engine. All other modules are product tests.
Product tests and audits must pass without docxtpl. Each import of docxtpl
during the run is recorded and fails the run, even when the code that
attempted it caught the ImportError. Subprocesses started by a test are not
covered; `tests/test_reference_free.py` checks those paths.

    python scripts/run_product_tests.py                 # product tests and audits
    python scripts/run_product_tests.py --tier audit    # audits only
    python scripts/run_product_tests.py --list          # print the classification

`--list` also reports two controls: audit modules that reach docxtpl (must be
empty: split the comparison into a differential module) and modules that
instrument execution (`sys.setprofile`, `sys.settrace` or a patch that raises
AssertionError) without the audit helper, which are audits not yet in the tier.

Exit status: 0 all passed, 1 test failures or errors, 2 docxtpl was already
loaded, 3 docxtpl was imported or loaded during the run.
"""
from __future__ import annotations

import argparse
import ast
import importlib.abc
import json
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"


def is_reference(name: str) -> bool:
    return name == "docxtpl" or name.startswith("docxtpl.")


# A string that names docxtpl or one of its modules, e.g. a patch target or a
# name handed to importlib.
REFERENCE_NAME = re.compile(r"docxtpl(\.[A-Za-z_][A-Za-z0-9_]*)*")
# Modules whose docxtpl names are only data for the import blocker they test.
NAMES_AS_DATA = {"test_product_suite", "test_reference_free"}
AUDIT_HELPER = "native_audit"
INSTRUMENTATION = {"setprofile", "settrace"}


def instruments(tree: ast.AST) -> bool:
    """Whether a module installs a profiler or tracer, or patches something to raise AssertionError."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
        if name in INSTRUMENTATION:
            return True
        for keyword in node.keywords:
            value = keyword.value
            raised = value.func if isinstance(value, ast.Call) else value
            if keyword.arg == "side_effect" and getattr(raised, "id", "") == "AssertionError":
                return True
    return False


def _names(tree: ast.AST, names_as_data: bool = False) -> tuple[bool, set[str]]:
    """Whether a module names docxtpl directly, and the modules it imports."""
    direct = False
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            direct = direct or REFERENCE_NAME.fullmatch(node.value) is not None
    return (direct and not names_as_data) or any(is_reference(name) for name in imported), imported


class Modules:
    """Local modules under `roots`, resolved by name, with the reference test cached."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = roots
        self.cache: dict[Path, bool] = {}

    def files(self, name: str) -> list[Path]:
        # Importing a.b.c runs a/__init__.py, a/b/__init__.py and a/b/c.py.
        found = []
        parts = name.split(".")
        for root in self.roots:
            for index in range(1, len(parts) + 1):
                base = root.joinpath(*parts[:index])
                for candidate in (base / "__init__.py", base.with_suffix(".py")):
                    if candidate.is_file():
                        found.append(candidate)
        return found

    def uses_reference(self, path: Path) -> bool:
        if path in self.cache:
            return self.cache[path]
        self.cache[path] = False  # import cycles
        direct, imported = _names(ast.parse(path.read_text(), str(path)), path.stem in NAMES_AS_DATA)
        result = direct or any(self.uses_reference(file) for name in sorted(imported)
                               if not name.startswith("docxtpl_native") for file in self.files(name))
        self.cache[path] = result
        return result


def classify(directory: Path = TESTS, roots: list[Path] | None = None) -> dict[str, list[str]]:
    """Split test modules into product, audit and differential tiers, plus the two controls.

    The product package itself is not followed: it must never import docxtpl,
    which `tests/test_reference_free.py` checks at run time.
    """
    modules = Modules(roots if roots is not None else [directory, ROOT / "python", ROOT])
    tiers: dict[str, list[str]] = {"product": [], "audit": [], "differential": [],
                                   "audit_with_reference": [], "instrumented_outside_audit": []}
    for path in sorted(directory.glob("test_*.py")):
        tree = ast.parse(path.read_text(), str(path))
        audit = AUDIT_HELPER in _names(tree)[1]
        if modules.uses_reference(path):
            tiers["differential"].append(path.stem)
            if audit:
                tiers["audit_with_reference"].append(path.stem)
        else:
            tiers["audit" if audit else "product"].append(path.stem)
        if not audit and instruments(tree):
            tiers["instrumented_outside_audit"].append(path.stem)
    return tiers


class BlockReference(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def find_spec(self, name: str, path: object = None, target: ModuleType | None = None) -> None:
        if is_reference(name):
            self.attempts.append(name)
            raise ImportError("docxtpl is not available to the product suite: " + name)
        return None


def run(modules: list[str], verbosity: int) -> int:
    loaded = sorted(name for name in sys.modules if is_reference(name))
    if loaded:
        print("docxtpl was loaded before the product suite: " + ", ".join(loaded), file=sys.stderr)
        return 2
    blocker = BlockReference()
    sys.meta_path.insert(0, blocker)
    sys.path[:0] = [str(TESTS), str(ROOT / "python"), str(ROOT)]
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    loaded = sorted(name for name in sys.modules if is_reference(name))
    if blocker.attempts or loaded:
        print(f"docxtpl was used by the product suite: attempts={blocker.attempts} loaded={loaded}",
              file=sys.stderr)
        return 3
    return 0 if result.wasSuccessful() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print the classification as JSON and exit")
    parser.add_argument("--tier", choices=("all", "product", "audit"), default="all",
                        help="run product tests and audits (default), or one tier")
    parser.add_argument("-v", "--verbose", action="count", default=1)
    arguments = parser.parse_args()
    modules = classify()
    if arguments.list:
        print(json.dumps({key: value for key, value in modules.items()}, indent=2))
        return 0
    selected = ["product", "audit"] if arguments.tier == "all" else [arguments.tier]
    return run(sorted(name for tier in selected for name in modules[tier]), arguments.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
