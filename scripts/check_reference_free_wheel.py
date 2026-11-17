"""Install a wheel in a new environment without docxtpl and run the user workload.

    python scripts/check_reference_free_wheel.py dist/docxtpl_native-*.whl \\
        --out artifacts/reference-free-wheel.json

The environment is created with uv from the package index (or uv's cache). It
installs the wheel with its `subdoc` extra and nothing else, then fails if
docxtpl is present or if the workload of `tests/reference_free.py` imports it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import reference_free  # noqa: E402  # ty: ignore[import-not-found]


def uv(*arguments: str) -> str:
    return subprocess.run(["uv", *arguments], check=True, capture_output=True, text=True).stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()
    wheel = arguments.wheel.resolve()
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        environment = directory / "venv"
        uv("venv", "--python", arguments.python, str(environment))
        python = environment / "bin" / "python"
        uv("pip", "install", "--python", str(python), f"docxtpl-native[subdoc] @ {wheel.as_uri()}")
        packages = json.loads(uv("pip", "list", "--python", str(python), "--format", "json"))
        installed = {package["name"]: package["version"] for package in packages}
        work = directory / "work"
        work.mkdir()
        result = reference_free.run(reference_free.WORKLOAD, cwd=work, python=str(python), source_tree=False)
        # Evidence files must not carry local paths.
        errors = [line.replace(str(directory), "<tmp>") for line in result.stderr.strip().splitlines()[-5:]]
    passed = "docxtpl" not in installed and reference_free.passed(result)
    report = {
        "check": "reference-free wheel installation",
        "wheel": wheel.name,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "python": arguments.python,
        "platform": platform.platform(terse=True),
        "installed": installed,
        "docxtpl_installed": "docxtpl" in installed,
        "workload_returncode": result.returncode,
        "workload_output": result.stdout.strip().splitlines()[-1:] if result.stdout else [],
        "workload_errors": [] if passed else errors,
        "passed": passed,
    }
    text = json.dumps(report, indent=2) + "\n"
    if arguments.out:
        arguments.out.write_text(text)
    print(text, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
