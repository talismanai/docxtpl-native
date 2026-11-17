#!/usr/bin/env python3
"""Check a performance study benchmark report against the regression budget.

The budget is relative to the recorded baseline: for each workload, the
median of the paired reference/native time ratios (both engines measured in
the same rounds on the same host) may not fall more than --tolerance below
the baseline's. Absolute times are reported but not budgeted: they depend on
the host. Exit status 1 lists the workloads over budget.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / 'compatibility' / 'performance-budget-baseline.json'


def ratios_from_report(report: dict) -> dict[str, float]:
    return {case['id']: case['summary']['paired_reference_over_candidate']['p50'] for case in report['cases']}


def check(baseline: dict, report: dict, tolerance: float) -> tuple[list[dict], list[str]]:
    expected = {name: values['paired_ratio_p50'] for name, values in baseline['benchmark'].items()}
    measured = ratios_from_report(report)
    rows, failures = [], []
    for name, reference in sorted(expected.items()):
        if name not in measured:
            failures.append(f'{name}: missing from the report')
            continue
        floor = reference * (1 - tolerance)
        row = {'workload': name, 'baseline_ratio': reference, 'measured_ratio': measured[name], 'floor': floor}
        rows.append(row)
        if measured[name] < floor:
            failures.append(f'{name}: paired ratio {measured[name]:.3f} below {floor:.3f} '
                            f'(baseline {reference:.3f}, tolerance {tolerance:.0%})')
    return rows, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path, help='report.json of study_performance.py --mode benchmark')
    parser.add_argument('--baseline', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--tolerance', type=float, default=0.10)
    args = parser.parse_args()
    if not 0 <= args.tolerance < 1:
        parser.error('tolerance must be in [0, 1)')
    rows, failures = check(json.loads(args.baseline.read_text()), json.loads(args.report.read_text()), args.tolerance)
    print(json.dumps({'tolerance': args.tolerance, 'workloads': rows, 'over_budget': failures}, indent=1))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
