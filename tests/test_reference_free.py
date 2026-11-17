"""The package runs with docxtpl absent; the guard that proves it is checked too."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import reference_free

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReferenceFreeTests(unittest.TestCase):
    def run_workload(self, workload: str) -> reference_free.subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            return reference_free.run(workload, cwd=Path(directory))

    def test_user_workload_never_imports_docxtpl(self) -> None:
        result = self.run_workload(reference_free.WORKLOAD)
        self.assertTrue(reference_free.passed(result), result.stdout + result.stderr)


class GuardMutationTests(unittest.TestCase):
    """Each mutation must make the guard fail; otherwise it proves nothing."""

    def assert_caught(self, workload: str, message: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = reference_free.run(workload, cwd=Path(directory))
        self.assertFalse(reference_free.passed(result), result.stdout)
        self.assertIn(message, result.stderr)

    def test_swallowed_import_is_caught(self) -> None:
        self.assert_caught("try:\n    import docxtpl\nexcept ImportError:\n    pass\n",
                           "docxtpl was used: attempts=['docxtpl']")

    def test_submodule_import_is_caught(self) -> None:
        # A normal import reaches the parent package first; ask the finder directly.
        self.assert_caught("try:\n    sys.meta_path[0].find_spec('docxtpl.richtext', None)\n"
                           "except ImportError:\n    pass\n", "attempts=['docxtpl.richtext']")

    def test_similar_names_are_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = reference_free.run("import docxtpl_native\nsys.meta_path[0].find_spec('docxtplx', None)\n",
                                        cwd=Path(directory))
        self.assertTrue(reference_free.passed(result), result.stderr)

    def test_module_injected_without_import_is_caught(self) -> None:
        self.assert_caught("import types\nsys.modules['docxtpl'] = types.ModuleType('docxtpl')\n",
                           "loaded=['docxtpl']")

    def test_failing_workload_is_caught(self) -> None:
        self.assert_caught("raise AssertionError('workload failed')\n", "workload failed")

    def test_clean_workload_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(reference_free.passed(reference_free.run("pass\n", cwd=Path(directory))))


class WheelMetadataTests(unittest.TestCase):
    def test_reference_requirement_is_reported(self) -> None:
        check = load_script("check_wheel").reference_requirements
        metadata = ("Name: docxtpl-native\nRequires-Dist: python-docx==1.2.0\nRequires-Dist: docxtpl-native-extra\n"
                    "Requires-Dist: Jinja2==3.1.6\n")
        self.assertEqual(check(metadata), [])
        for requirement in ("docxtpl==0.20.2", "DocxTpl>=0.20", "docxtpl[subdoc]==0.20.2",
                            "docxtpl==0.20.2 ; extra == 'lab'", "docxtpl"):
            with self.subTest(requirement=requirement):
                self.assertEqual(check(metadata + f"Requires-Dist: {requirement}\n"), [requirement])


if __name__ == "__main__":
    unittest.main()
