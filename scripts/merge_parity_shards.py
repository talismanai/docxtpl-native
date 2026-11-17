"""Join the parity reports of the shards of one run into one report.

    python scripts/merge_parity_shards.py --out artifacts/parity shard-*/report.json

Exit status: 0 the merged corpus passes, 1 it has gaps, 2 the reports are not
the complete set of shards of one run (nothing is written then).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from paritylab.cli import _finish
from paritylab.shards import ShardError, merge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('reports', nargs='+', type=Path, help='report.json of each shard')
    parser.add_argument('--out', type=Path, required=True, help='directory of the merged report')
    arguments = parser.parse_args(argv)
    try:
        merged = merge([json.loads(path.read_text()) for path in arguments.reports])
    except ShardError as error:
        print(f'cannot merge the shards: {error}', file=sys.stderr)
        return 2
    arguments.out.mkdir(parents=True, exist_ok=True)
    _finish(merged, arguments.out)
    print(f"merged {merged['shards']} shards: {json.dumps(merged['counts'])} status={merged['status']}")
    return 0 if merged['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
