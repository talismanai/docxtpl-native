"""Machine-checked evidence for the declared deployment targets.

`deployment.python_platform_matrix` claims the engine installs and renders on
every target the contract declares. The claim qualifies only from wheel
installation reports measured **at the revision under test**, one per declared
target: `python/paritylab/check_wheel_install.py` produces them inside the
target's container, and `standalone_evidence.valid_wheel_evidence` validates
each one field by field (checker hash, pinned reference, wheel tag, native
extension inside the fresh environment, rendered text).

An absent report leaves its target unverified, because a workstation has no
containers. A report that does not validate, that names a target the contract
does not declare, or that repeats one is an issue: it claims evidence that is
not there.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .standalone_evidence import CASES, valid_wheel_evidence

PREFIX = 'deployment/'


def target_id(platform: str, machine: str, python: str) -> str:
    """Stable id for one (platform, machine, Python) target."""
    return PREFIX + platform + '_' + machine + '_cp' + ''.join(python.split('.')[:2])


def declared_targets(contract: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    return {target_id(target['platform'], target['machine'], target['python']): target
            for target in contract.get('deployment_targets', [])}


def deployment_statuses(contract: Mapping[str, Any], reports: Iterable[Any],
                        revision: str | None = None,
                        expected_versions: Mapping[str, str] | None = None
                        ) -> tuple[dict[str, str], list[str]]:
    """Status per declared target, plus the issues the reports themselves raise."""
    targets = declared_targets(contract)
    statuses: dict[str, str] = {name: 'unverified' for name in targets}
    issues: list[str] = []
    seen: set[str] = set()
    for position, report in enumerate(reports, start=1):
        case = report.get('case') if isinstance(report, Mapping) else None
        if case not in CASES or not valid_wheel_evidence(case, report, revision, expected_versions):
            issues.append('deployment report ' + str(position) + ' is not valid same-revision wheel evidence')
            continue
        name = target_id(report['platform'], report['machine'], report['python'])
        if name not in targets:
            issues.append('deployment report names an undeclared target: ' + name)
            continue
        if name in seen:
            # Two reports for one target cannot both be the measurement; the
            # target goes back to unverified instead of taking the first.
            issues.append('more than one deployment report for ' + name)
            statuses[name] = 'unverified'
            continue
        seen.add(name)
        statuses[name] = 'pass'
    return statuses, issues
