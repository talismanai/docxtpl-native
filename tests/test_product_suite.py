"""The split of the test suite into product and differential modules."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_product_tests", ROOT / "scripts" / "run_product_tests.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProductSuiteClassificationTests(unittest.TestCase):
    def test_modules_importing_docxtpl_are_differential(self) -> None:
        runner = load_runner()
        sources = {
            "test_plain": "import docxtpl_native\nfrom docxtpl_native import RichText\n",
            "test_import": "import docxtpl\n",
            "test_submodule": "import docxtpl.inline_image\n",
            "test_from": "from docxtpl import DocxTemplate\n",
            "test_from_submodule": "from docxtpl.subdoc import Subdoc\n",
            "test_nested": "def load():\n    import docxtpl as reference\n",
            "test_relative": "from . import docxtpl\n",
            "test_patch_target": "from unittest.mock import patch\npatch('docxtpl.DocxTemplate.render')\n",
            "test_other_string": "name = 'docxtplx'\nmessage = 'docxtpl is absent'\n",
            "test_import_module": "import importlib\nimportlib.import_module('docxtpl.subdoc')\n",
            "test_module_names": "MODULES = ['docx', 'docxtpl.template']\n",
            "test_helper": "import helper\n",
            "test_package": "from lab.tools import run\n",
            "test_clean_package": "from lab import clean\n",
            "helper": "import second\n",
            "second": "import docxtpl\n",
        }
        packages = {"lab/__init__.py": "", "lab/clean.py": "", "lab/tools.py": "def run():\n    import docxtpl\n"}
        with tempfile.TemporaryDirectory() as name:
            for module, source in sources.items():
                (Path(name) / f"{module}.py").write_text(source)
            for relative, source in packages.items():
                (Path(name) / relative).parent.mkdir(exist_ok=True)
                (Path(name) / relative).write_text(source)
            tiers = runner.classify(Path(name), [Path(name)])
            self.assertEqual(tiers["product"], ["test_clean_package", "test_other_string", "test_plain",
                                                "test_relative"])
            self.assertEqual(tiers["differential"], ["test_from", "test_from_submodule", "test_helper", "test_import",
                                                     "test_import_module", "test_module_names", "test_nested",
                                                     "test_package", "test_patch_target", "test_submodule"])

    def test_audit_tier_and_controls(self) -> None:
        runner = load_runner()
        sources = {
            "test_audit": "from native_audit import forbid\nforbid('jinja2.Template.render')\n",
            "test_audit_module": "import native_audit\n",
            "test_audit_reference": "from native_audit import forbid\nimport docxtpl\n",
            "test_profiler": "import sys\nsys.setprofile(None)\n",
            "test_tracer": "import sys\nsys.settrace(None)\n",
            "test_patch_guard": "from unittest.mock import patch\npatch('a.b', side_effect=AssertionError('no'))\n",
            "test_patch_value": "from unittest.mock import patch\npatch('a.b', side_effect=ValueError('no'))\n",
            "test_audit_profiler": "import sys\nimport native_audit\nsys.setprofile(None)\n",
            "native_audit": "",
        }
        with tempfile.TemporaryDirectory() as name:
            for module, source in sources.items():
                (Path(name) / f"{module}.py").write_text(source)
            tiers = runner.classify(Path(name), [Path(name)])
        self.assertEqual(tiers["audit"], ["test_audit", "test_audit_module", "test_audit_profiler"])
        self.assertEqual(tiers["product"], ["test_patch_guard", "test_patch_value", "test_profiler", "test_tracer"])
        self.assertEqual(tiers["differential"], ["test_audit_reference"])
        self.assertEqual(tiers["audit_with_reference"], ["test_audit_reference"])
        self.assertEqual(tiers["instrumented_outside_audit"], ["test_patch_guard", "test_profiler", "test_tracer"])

    def test_repository_audits_do_not_reach_docxtpl(self) -> None:
        tiers = load_runner().classify()
        self.assertEqual(tiers["audit_with_reference"], [])
        self.assertIn("test_native_lookup", tiers["audit"])
        self.assertIn("test_audited_documents_reference", tiers["differential"])

    def test_blocker_records_reference_names_only(self) -> None:
        blocker = load_runner().BlockReference()
        self.assertIsNone(blocker.find_spec("docxtpl_native"))
        for name in ("docxtpl", "docxtpl.richtext"):
            with self.assertRaises(ImportError):
                blocker.find_spec(name)
        self.assertEqual(blocker.attempts, ["docxtpl", "docxtpl.richtext"])



if __name__ == "__main__":
    unittest.main()
