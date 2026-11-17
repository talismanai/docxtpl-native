"""Join the reports of one run split with `--shard K/N` back into one report.

A shard measures part of the corpus, so its selection is never complete and it
qualifies nothing. The merge is complete only when the shards come from one
run of one revision and cover the corpus they were cut from exactly once, so
the joined report claims nothing the shards did not measure.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .cli import corpus_selection

# What every shard of one run measures alike; anything else differs per shard.
PER_SHARD = {'cases', 'counts', 'status', 'selection', 'platform', 'notes'}


class ShardError(ValueError):
    """The reports are not the complete set of shards of one run."""


def merge(reports: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ShardError('no shard reports')
    shards = [report.get('selection', {}).get('shard') for report in reports]
    if any(not shard for shard in shards):
        raise ShardError('a report is not a shard')
    count = shards[0]['count']
    if sorted(shard['index'] for shard in shards) != list(range(1, count + 1)) \
            or any(shard['count'] != count for shard in shards):
        raise ShardError(f'expected shards 1..{count} once each, got '
                         + ', '.join(f"{shard['index']}/{shard['count']}" for shard in shards))
    ordered = sorted(reports, key=lambda report: report['selection']['shard']['index'])
    first = ordered[0]
    shared = {key: value for key, value in first.items() if key not in PER_SHARD}
    chosen = {key: value for key, value in first['selection'].items() if key not in ('shard', 'complete')}
    for report in ordered:
        index = report['selection']['shard']['index']
        if report.get('status') not in ('pass', 'gaps'):
            raise ShardError(f'shard {index}/{count} did not finish')
        if {key: value for key, value in report.items() if key not in PER_SHARD} != shared:
            raise ShardError(f'shard {index}/{count} is not from the same run as shard 1/{count}')
        if {key: value for key, value in report['selection'].items() if key not in ('shard', 'complete')} != chosen:
            raise ShardError(f'shard {index}/{count} selected another corpus')
        expected = first['corpus'][index - 1::count]
        if [case['id'] for case in report['cases']] != expected:
            raise ShardError(f'shard {index}/{count} does not hold its part of the corpus')
    position = {case_id: number for number, case_id in enumerate(first['corpus'])}
    cases = sorted((case for report in ordered for case in report['cases']), key=lambda case: position[case['id']])
    failures = sum(case['status'] not in ('pass', 'divergence') for case in cases)
    diverged = sum(case['status'] == 'divergence' for case in cases)
    counts = {'total': len(cases), 'passed': len(cases) - failures - diverged, 'gaps_or_errors': failures}
    if diverged:
        counts['divergences'] = diverged
    platforms = sorted({report['platform'] for report in ordered})
    merged = {**shared, 'platform': platforms[0] if len(platforms) == 1 else platforms,
              'selection': {**chosen, 'shard': None, 'complete': corpus_selection(chosen)},
              'shards': count, 'cases': cases, 'counts': counts,
              'status': 'pass' if not failures else 'gaps',
              'notes': [*first['notes'], f'Merged from {count} shards of one run, each on a runner of its own.']}
    del merged['corpus']
    return merged
