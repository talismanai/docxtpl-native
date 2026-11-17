"""The regression budget check compares paired ratios with the baseline."""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('check_performance_budget', ROOT / 'scripts' / 'check_performance_budget.py')
assert SPEC is not None and SPEC.loader is not None
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)


def report(**ratios: float) -> dict:
    return {'cases': [{'id': name, 'summary': {'paired_reference_over_candidate': {'p50': ratio}}}
                      for name, ratio in ratios.items()]}


BASELINE = {'benchmark': {'plain': {'paired_ratio_p50': 2.0}, 'loop': {'paired_ratio_p50': 0.8}}}


class PerformanceBudgetTests(unittest.TestCase):

    def test_within_tolerance_passes(self) -> None:
        _, failures = budget.check(BASELINE, report(plain=1.85, loop=0.79), 0.10)
        self.assertEqual(failures, [])

    def test_regression_and_missing_workload_fail(self) -> None:
        _, failures = budget.check(BASELINE, report(plain=1.7), 0.10)
        self.assertEqual(len(failures), 2)
        self.assertTrue(any(failure.startswith('plain: paired ratio 1.700') for failure in failures), failures)
        self.assertIn('loop: missing from the report', failures)

    def test_recorded_baseline_has_a_ratio_for_every_workload(self) -> None:
        # The baseline is the gate's comparison target: a workload without a
        # usable ratio would let every report through unnoticed.
        baseline = json.loads((ROOT / 'compatibility' / 'performance-budget-baseline.json').read_text())
        expected = {name: values['paired_ratio_p50'] for name, values in baseline['benchmark'].items()}
        self.assertTrue(expected)
        for name, ratio in expected.items():
            self.assertGreater(ratio, 0, name)
        own = {'cases': [{'id': name, 'summary': {'paired_reference_over_candidate': {'p50': ratio}}}
                         for name, ratio in expected.items()]}
        self.assertEqual(budget.check(baseline, own, 0.0)[1], [])


if __name__ == '__main__':
    unittest.main()
