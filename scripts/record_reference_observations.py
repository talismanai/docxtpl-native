"""Record the reference observations of generated differential probes.

    PYTHONPATH=python .venv/bin/python scripts/record_reference_observations.py [module ...]

Each module (default: all below) defines EXECUTIONS (probe name -> execute)
and EXPECTED_PATH. Every scenario runs with the reference engine (docxtpl)
and its observation is written to the module's EXPECTED_PATH.
"""
import importlib
import json
import sys
import warnings

import docxtpl

MODULES = ('builtin_partition_probes', 'internal_error_probes', 'tag_partition_probes', 'api_partition_probes',
           'scenario_partition_probes', 'language_partition_probes')


def record(name: str) -> int:
    module = importlib.import_module('paritylab.' + name)
    expected = {}
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        for probe, execute in module.EXECUTIONS.items():
            expected[probe] = execute(docxtpl).observation
    module.EXPECTED_PATH.write_text(json.dumps(expected, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    return len(expected)


def main(names: list[str]) -> int:
    for name in names or MODULES:
        print(name, record(name), 'probes recorded')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
