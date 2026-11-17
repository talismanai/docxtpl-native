"""The known-failure map and the checker that compares suite logs with it."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_known_failures",
                                                  ROOT / "scripts" / "check_known_failures.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log_with(failures: dict[str, str], summary: bool = True) -> str:
    lines = ["test_a (test_x.T.test_a) ... ok"]
    for identifier, kind in failures.items():
        lines += ["=" * 70, f"{kind.upper()}: {identifier}", "-" * 70, "Traceback (most recent call last):",
                  "AssertionError: x"]
    if summary:
        lines += ["-" * 70, f"Ran {len(failures) + 1} tests in 0.010s", "", "FAILED (failures=1)"]
    return "\n".join(lines) + "\n"


def with_pending(known: dict[str, Any]) -> dict[str, Any]:
    """A copy of `known` with one synthetic pending failure, so the rules for
    pending entries are exercised whether or not the map still has any."""
    mutated = copy.deepcopy(known)
    mutated["causes"]["synthetic_pending"] = {"contracts": ["jinja.async.callback"],
                                              "description": "A synthetic pending failure."}
    mutated["failures"].append({"id": "test_pending (test_x.T.test_pending)", "kind": "fail",
                                "group": "async_lifecycle", "cause": "synthetic_pending"})
    return mutated


class KnownFailureMapTests(unittest.TestCase):
    checker: ModuleType
    known: dict[str, Any]
    pending: dict[str, Any]
    contracts: set[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.checker = load_checker()
        cls.known = json.loads((ROOT / "compatibility" / "known_failures.json").read_text())
        cls.pending = with_pending(cls.known)
        catalog = json.loads((ROOT / "compatibility" / "spec_contracts.json").read_text())
        cls.contracts = {contract["id"] for contract in catalog["contracts"]}

    def test_synthetic_pending_map_is_valid(self) -> None:
        self.assertEqual(self.checker.validate(self.pending, self.contracts), [])

    def test_repository_map_is_valid(self) -> None:
        self.assertEqual(self.checker.validate(self.known, self.contracts), [])

    def test_every_failure_maps_to_a_preserved_contract(self) -> None:
        catalog = json.loads((ROOT / "compatibility" / "spec_contracts.json").read_text())
        scope = {contract["id"]: contract["scope"] for contract in catalog["contracts"]}
        for name, cause in self.known["causes"].items():
            for contract in cause["contracts"]:
                with self.subTest(cause=name, contract=contract):
                    self.assertEqual(scope[contract], "preserved")

    def test_validation_rejects_each_broken_rule(self) -> None:
        mutations = {
            "accepted without decision": lambda m: m["causes"]["tojson_python_algorithm"]["accepted"].update(decision=""),
            "unknown contract": lambda m: m["causes"]["tojson_python_algorithm"].update(contracts=["jinja.nope"]),
            "no contract": lambda m: m["causes"]["tojson_python_algorithm"].update(contracts=[]),
            "missing description": lambda m: m["causes"]["tojson_python_algorithm"].update(description=""),
            "unknown cause": lambda m: m["failures"][0].update(cause="unknown"),
            "unknown group": lambda m: m["failures"][0].update(group="other"),
            "unknown kind": lambda m: m["failures"][0].update(kind="skip"),
            "duplicate": lambda m: m["failures"].append(dict(m["failures"][0])),
            "unused cause": lambda m: m["causes"].update(extra={"description": "x",
                                                                "contracts": ["jinja.async.callback"]}),
        }
        for label, mutate in mutations.items():
            mutated = copy.deepcopy(self.pending)
            mutate(mutated)
            with self.subTest(label):
                self.assertTrue(self.checker.validate(mutated, self.contracts))

    def run_checker(self, log: str, known: dict[str, Any] | None = None) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "suite.log"
            map_path = Path(directory) / "known.json"
            log_path.write_text(log)
            map_path.write_text(json.dumps(known if known is not None else self.known))
            output = io.StringIO()
            with redirect_stdout(output), redirect_stderr(output):
                status = self.checker.main([str(log_path), "--map", str(map_path)])
            return status, output.getvalue()

    def all_entries(self) -> dict[str, str]:
        return {failure["id"]: failure["kind"]
                for failure in [*self.known["failures"], *self.known["divergences"]]}

    def test_matching_log_passes(self) -> None:
        self.assertEqual(self.run_checker(log_with(self.all_entries()))[0], 0)

    def test_accepted_divergence_must_keep_failing(self) -> None:
        divergence = self.known["divergences"][0]
        passing = {key: value for key, value in self.all_entries().items() if key != divergence["id"]}
        status, output = self.run_checker(log_with(passing))
        self.assertEqual(status, 1)
        self.assertIn("fixed: " + divergence["id"], output)
        changed = dict(self.all_entries(), **{divergence["id"]: "error" if divergence["kind"] == "fail" else "fail"})
        status, output = self.run_checker(log_with(changed))
        self.assertEqual(status, 1)
        self.assertIn("kind_changed: " + divergence["id"], output)

    def test_divergences_and_failures_stay_apart(self) -> None:
        accepted = self.pending["divergences"][0]["cause"]
        pending = self.pending["failures"][0]["cause"]
        mutations = {
            "accepted without decision": lambda m: m["causes"][accepted].update(accepted={"date": "2026-11-14"}),
            "divergence of a pending cause": lambda m: m["divergences"].append(
                dict(m["failures"][0], id="test_y (test_x.T.test_y)")),
            "failure of an accepted cause": lambda m: m["failures"].append(
                dict(m["divergences"][0], id="test_z (test_x.T.test_z)")),
        }
        self.assertNotIn("accepted", self.pending["causes"][pending])
        for label, mutate in mutations.items():
            mutated = copy.deepcopy(self.pending)
            mutate(mutated)
            with self.subTest(label):
                self.assertTrue(self.checker.validate(mutated, self.contracts))

    def test_new_fixed_and_changed_failures_fail(self) -> None:
        failures = self.all_entries()
        first, second = list(failures)[:2]
        new = dict(failures, **{"test_new (test_x.T.test_new)": "fail"})
        fixed = {key: value for key, value in failures.items() if key != first}
        changed = dict(failures, **{second: "error" if failures[second] == "fail" else "fail"})
        for label, observed in {"new": new, "fixed": fixed, "kind_changed": changed}.items():
            with self.subTest(label):
                status, output = self.run_checker(log_with(observed))
                self.assertEqual(status, 1)
                self.assertIn(label + ":", output)

    def test_truncated_log_fails(self) -> None:
        failures = self.all_entries()
        status, output = self.run_checker(log_with(failures, summary=False))
        self.assertEqual(status, 1)
        self.assertIn("did not complete", output)

    def test_invalid_map_stops_before_the_log(self) -> None:
        mutated = copy.deepcopy(self.pending)
        mutated["failures"][0]["cause"] = "unknown"
        self.assertEqual(self.run_checker(log_with({}), mutated)[0], 2)


if __name__ == "__main__":
    unittest.main()
