"""Compare a full-suite unittest log with compatibility/known_failures.json.

    PYTHONPATH=tests:python .venv/bin/python -m unittest discover -s tests -v > suite.log 2>&1
    python scripts/check_known_failures.py suite.log

Every FAIL or ERROR in the log must be a known failure or an accepted
divergence, and each of those must still fail with the same kind: a test that
now passes is removed from the map in the same change, and an accepted
divergence that changes is a change to its documented difference. The log must end with the unittest summary, so a crashed or truncated
run is not read as a pass. The map is also validated against the contract
catalog.

Exit status: 0 the log matches the map, 1 it does not, 2 the map is invalid.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "compatibility" / "known_failures.json"
CATALOG = ROOT / "compatibility" / "spec_contracts.json"
GROUPS = {"loops", "blocks", "async_lifecycle", "modules", "filters", "context"}
HEADER = re.compile(r"^(FAIL|ERROR): (.+)$", re.MULTILINE)
SUMMARY = re.compile(r"^Ran (\d+) tests? in [\d.]+s$", re.MULTILINE)


def validate(known: dict, contracts: set[str]) -> list[str]:
    """Problems in the map itself: unknown causes, groups or contracts."""
    problems: list[str] = []
    causes = known.get("causes", {})
    for name, cause in causes.items():
        if not cause.get("description"):
            problems.append(f"cause {name}: missing description")
        if not cause.get("contracts"):
            problems.append(f"cause {name}: no contract")
        problems.extend(f"cause {name}: unknown contract {contract}"
                        for contract in cause.get("contracts", []) if contract not in contracts)
        accepted = cause.get("accepted")
        if accepted is not None and not (isinstance(accepted, dict) and accepted.get("decision")):
            problems.append(f"cause {name}: an accepted divergence needs a decision")
    seen: set[str] = set()
    for section, accepted in (("failures", False), ("divergences", True)):
        for failure in known.get(section, []):
            _check_entry(problems, seen, causes, section, accepted, failure)
    unused = set(causes) - {failure.get("cause") for failure in entries(known)}
    problems.extend(f"cause {name}: no failure uses it" for name in sorted(unused))
    return problems


def entries(known: dict) -> list[dict]:
    """Known failures and accepted divergences: both must keep failing."""
    return [*known.get("failures", []), *known.get("divergences", [])]


def _check_entry(problems: list[str], seen: set[str], causes: dict, section: str, accepted: bool,
                 failure: dict) -> None:
    identifier = failure.get("id", "")
    if identifier in seen:
        problems.append(f"duplicate failure {identifier}")
    seen.add(identifier)
    if failure.get("cause") not in causes:
        problems.append(f"{identifier}: unknown cause {failure.get('cause')!r}")
    elif ("accepted" in causes[failure["cause"]]) != accepted:
        problems.append(f"{identifier}: listed in {section} but its cause is "
                        + ("not accepted" if accepted else "an accepted divergence"))
    if failure.get("group") not in GROUPS:
        problems.append(f"{identifier}: unknown group {failure.get('group')!r}")
    if failure.get("kind") not in {"fail", "error"}:
        problems.append(f"{identifier}: unknown kind {failure.get('kind')!r}")


def observed(log: str) -> dict[str, str] | None:
    """FAIL/ERROR identifiers of a complete log, or None when the summary is missing."""
    if not SUMMARY.search(log):
        return None
    return {identifier: kind.lower() for kind, identifier in HEADER.findall(log)}


def compare(known: dict, failures: dict[str, str]) -> dict[str, list[str]]:
    expected = {failure["id"]: failure["kind"] for failure in entries(known)}
    return {
        "new": sorted(set(failures) - set(expected)),
        "fixed": sorted(set(expected) - set(failures)),
        "kind_changed": sorted(identifier for identifier in set(expected) & set(failures)
                               if expected[identifier] != failures[identifier]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="output of unittest discover -v")
    parser.add_argument("--map", type=Path, default=MAP)
    parser.add_argument("--catalog", type=Path, default=CATALOG)
    arguments = parser.parse_args(argv)
    known = json.loads(arguments.map.read_text())
    contracts = {contract["id"] for contract in json.loads(arguments.catalog.read_text())["contracts"]}
    problems = validate(known, contracts)
    if problems:
        print("invalid known-failure map:", *problems, sep="\n  ", file=sys.stderr)
        return 2
    failures = observed(arguments.log.read_text(errors="replace"))
    if failures is None:
        print("the log has no unittest summary; the run did not complete", file=sys.stderr)
        return 1
    result = compare(known, failures)
    for label, identifiers in result.items():
        for identifier in identifiers:
            print(f"{label}: {identifier}")
    print(json.dumps({"known": len(known["failures"]), "accepted_divergences": len(known.get("divergences", [])),
                      "observed": len(failures),
                      **{label: len(identifiers) for label, identifiers in result.items()}}))
    return 1 if any(result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
