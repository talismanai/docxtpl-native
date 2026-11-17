"""A run split with --shard joins back into the report one run would write."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from paritylab.inventory import matrix
from paritylab.shards import ShardError, merge

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ['synthetic/plain', 'upstream/embedded', 'protocol/callback_identity', 'protocol/other', 'protocol/last']


def shard(index: int, count: int = 2, status: str = 'pass') -> dict[str, Any]:
    cases = [{'id': case_id, 'status': status} for case_id in CORPUS[index - 1::count]]
    failures = sum(case['status'] != 'pass' for case in cases)
    return {'schema_version': 1, 'mode': 'differential', 'candidate': 'docxtpl_native', 'reference': 'docxtpl',
            'python': '3.12.14', 'platform': 'Linux-x86_64', 'source_revision': 'current',
            'source_tree': 'clean_checkout', 'visual_policy': 'required', 'repeat': 7, 'warmup': 1, 'jobs': 1,
            'capabilities': {'native_available': True, 'reference_fallback': False},
            'selection': {'suite': 'all', 'cases': [], 'protocol_cases': [], 'real_manifest': False,
                          'rerun_from': None, 'shard': {'index': index, 'count': count}, 'complete': False},
            'notes': ['shared note'], 'corpus': list(CORPUS), 'cases': cases,
            'counts': {'total': len(cases), 'passed': len(cases) - failures, 'gaps_or_errors': failures},
            'status': 'gaps' if failures else 'pass'}


class MergeTests(unittest.TestCase):
    def test_the_shards_of_one_run_make_the_complete_corpus(self) -> None:
        merged = merge([shard(2), shard(1)])
        self.assertEqual([case['id'] for case in merged['cases']], CORPUS)
        self.assertTrue(merged['selection']['complete'])
        self.assertEqual(merged['counts'], {'total': 5, 'passed': 5, 'gaps_or_errors': 0})
        self.assertEqual(merged['status'], 'pass')
        self.assertNotIn('corpus', merged)

    def test_the_merge_qualifies_like_a_single_run(self) -> None:
        contract = {'notice': 'test contract', 'reference_commit': 'fixed', 'versions': {'docxtpl': '0.20.2'},
                    'qualification': {'visual': 'required'}, 'python_targets': ['3.11', '3.12'],
                    'requirements': [{'id': 'identity', 'symbols': ['render'],
                                      'tests': ['protocol/callback_identity'], 'reviewed': True}]}
        api = {'errors': [], 'versions': {'docxtpl': '0.20.2'}, 'symbols': [{'id': 'render', 'scope': 'docxtpl'}]}
        self.assertTrue(matrix(contract, api, merge([shard(1), shard(2)]), required_revision='current')['fully_compatible'])
        self.assertFalse(matrix(contract, api, shard(1), required_revision='current')['fully_compatible'])

    def test_a_gap_in_any_shard_is_a_gap_of_the_merge(self) -> None:
        merged = merge([shard(1), shard(2, status='gap')])
        self.assertEqual(merged['status'], 'gaps')
        self.assertEqual(merged['counts']['gaps_or_errors'], 2)

    def test_anything_but_the_complete_set_of_one_run_is_refused(self) -> None:
        other_revision = shard(2)
        other_revision['source_revision'] = 'other'
        wrong_cases = shard(2)
        wrong_cases['cases'] = wrong_cases['cases'][:1]
        unfinished = shard(2)
        del unfinished['status']
        other_corpus = shard(2)
        other_corpus['selection']['protocol_cases'] = ['protocol/other']
        not_a_shard = shard(2)
        not_a_shard['selection']['shard'] = None
        for label, reports in {'missing shard': [shard(1)], 'repeated shard': [shard(1), shard(1)],
                               'other count': [shard(1), shard(2, count=3)],
                               'other revision': [shard(1), other_revision],
                               'wrong cases': [shard(1), wrong_cases], 'unfinished': [shard(1), unfinished],
                               'other corpus': [shard(1), other_corpus], 'not a shard': [shard(1), not_a_shard],
                               'nothing': []}.items():
            with self.subTest(label), self.assertRaises(ShardError):
                merge(copy.deepcopy(reports))


class ShardedRunTests(unittest.TestCase):
    def test_a_sharded_self_check_merges_into_the_unsharded_selection(self) -> None:
        cases = ['plain', 'unicode', 'if']
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in (1, 2):
                command = [sys.executable, '-m', 'paritylab.cli', '--candidate', 'reference', '--suite', 'synthetic',
                           '--repeat', '1', '--warmup', '0', '--visual', 'off', '--shard', f'{index}/2',
                           '--out', str(root / f'shard-{index}')]
                for case in cases:
                    command += ['--case', case]
                subprocess.run(command, check=True, capture_output=True, text=True)
                report = json.loads((root / f'shard-{index}' / 'report.json').read_text())
                self.assertFalse(report['selection']['complete'])
            subprocess.run([sys.executable, str(ROOT / 'scripts' / 'merge_parity_shards.py'), '--out',
                            str(root / 'merged'), str(root / 'shard-2' / 'report.json'),
                            str(root / 'shard-1' / 'report.json')], check=True, capture_output=True, text=True)
            merged = json.loads((root / 'merged' / 'report.json').read_text())
            self.assertEqual([case['id'] for case in merged['cases']], ['synthetic/' + case for case in cases])
            self.assertEqual(merged['counts']['passed'], 3)
            # Three chosen cases are a selection, not the corpus, merged or not.
            self.assertFalse(merged['selection']['complete'])

    def test_a_shard_names_one_of_its_count(self) -> None:
        for value in ('0/2', '3/2', 'x/2', '2'):
            with self.subTest(value):
                completed = subprocess.run([sys.executable, '-m', 'paritylab.cli', '--shard', value],
                                           check=False, capture_output=True, text=True)
                self.assertEqual(completed.returncode, 2)


if __name__ == '__main__':
    unittest.main()
