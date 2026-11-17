"""Mutation tests for the repository's Python-free build and version guards."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        for name in ("check_engine_boundary.sh", "check_versions.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.root / "scripts" / name)
        for directory in (
            "crates/docxtpl-core",
            "crates/docxtpl-pycompat",
            "crates/docxtpl-wasm",
        ):
            (self.root / directory / "src").mkdir(parents=True)
            (self.root / directory / "src/lib.rs").write_text("// Rust only\n")
            (self.root / directory / "Cargo.toml").write_text(
                "version.workspace = true\n"
            )
        (self.root / "bindings/python").mkdir(parents=True)
        (self.root / "bindings/python/Cargo.toml").write_text(
            "version.workspace = true\n"
        )
        (self.root / "python/docxtpl_native").mkdir(parents=True)
        (self.root / "Cargo.toml").write_text(
            '[workspace.package]\nversion = "0.1.0"\n'
        )
        (self.root / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
        (self.root / "python/docxtpl_native/__init__.py").write_text(
            '__version__ = "0.1.0"\n'
        )
        # Only the dependency graph is substituted. The real guard evaluates
        # its target selection, output parsing, source checks and exit status.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        cargo = self.bin / "cargo"
        cargo.write_text("""#!/usr/bin/env bash
set -eu
echo "$*" >> "$GUARD_CALLS"
case "$*" in
  *'-p docxtpl-core'*) echo "${HOST_GRAPH:-libxml v0.3.0}" ;;
  *'-p docxtpl-wasm'*'--target wasm32-unknown-unknown'*) echo "${BROWSER_GRAPH:-serde v1.0.0}" ;;
  *'-p '*) echo 'serde v1.0.0' ;;
  *) echo "${DEFAULT_GRAPH:-serde v1.0.0}" ;;
esac
""")
        cargo.chmod(0o755)

    def run_guard(self, name: str, **graphs: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.root / "scripts" / name)],
            env={
                **os.environ,
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "GUARD_CALLS": str(self.root / "calls"),
                **graphs,
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def test_native_host_library_is_allowed_and_all_targets_are_checked(self) -> None:
        result = self.run_guard(
            "check_engine_boundary.sh", HOST_GRAPH="libxml v0.3.0\nlibloading v0.8.0"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-p docxtpl-core --target all", (self.root / "calls").read_text())

    def test_python_and_c_deflate_dependencies_are_rejected(self) -> None:
        for dependency in ("pyo3 v0.29.0", "python3-sys v0.7.0", "libz-sys v1.0.0"):
            with self.subTest(dependency=dependency):
                result = self.run_guard(
                    "check_engine_boundary.sh", HOST_GRAPH=dependency
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(dependency, result.stderr)

    def test_binding_in_default_members_is_rejected(self) -> None:
        result = self.run_guard(
            "check_engine_boundary.sh", DEFAULT_GRAPH="pyo3 v0.29.0"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("default workspace members", result.stderr)

    def test_c_in_browser_graph_is_rejected(self) -> None:
        result = self.run_guard(
            "check_engine_boundary.sh", BROWSER_GRAPH="libloading v0.8.0"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wasm32", result.stderr)

    def test_build_script_and_process_spawn_are_rejected(self) -> None:
        (self.root / "crates/docxtpl-core/build.rs").write_text("fn main() {}\n")
        result = self.run_guard("check_engine_boundary.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("build script", result.stderr)
        (self.root / "crates/docxtpl-wasm/src/lib.rs").write_text(
            'std::process::Command::new("python");\n'
        )
        result = self.run_guard("check_engine_boundary.sh")
        self.assertIn("spawns a process", result.stderr)

    def test_versions_must_agree(self) -> None:
        self.assertEqual(self.run_guard("check_versions.sh").returncode, 0)
        for path, content, diagnostic in (
            ("pyproject.toml", '[project]\nversion = "0.2.0"\n', "pyproject.toml"),
            (
                "python/docxtpl_native/__init__.py",
                '__version__ = "0.2.0"\n',
                "__init__.py",
            ),
            ("bindings/python/Cargo.toml", 'version = "0.1.0"\n', "version.workspace"),
        ):
            with self.subTest(path=path):
                target = self.root / path
                original = target.read_text()
                target.write_text(content)
                result = self.run_guard("check_versions.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
                target.write_text(original)


if __name__ == "__main__":
    unittest.main()
