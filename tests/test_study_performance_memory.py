"""The performance study's memory child reports peaks for one generation and repeats."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StudyPerformanceMemoryTests(unittest.TestCase):
    def test_memory_child_reports_rss_and_python_peaks_and_keeps_the_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for role in ('reference', 'candidate'):
                with self.subTest(role=role):
                    completed = subprocess.run(
                        [sys.executable, str(ROOT / 'scripts' / 'study_performance.py'), '--mode', 'memory-child',
                         '--case', 'plain', '--role', role, '--revision', 'unused', '--out', directory,
                         '--repeat', '2'],
                        check=True, capture_output=True, text=True,
                        env={**os.environ, 'PYTHONPATH': os.pathsep.join([str(ROOT / 'tests'), str(ROOT / 'python')])})
                    result = json.loads(completed.stdout.strip().splitlines()[-1])
                    self.assertEqual((result['case'], result['role']), ('plain', role))
                    self.assertLessEqual(result['baseline_rss_kib'], result['first_rss_kib'])
                    self.assertLessEqual(result['first_rss_kib'], result['repeated_rss_kib'])
                    self.assertGreater(result['first_python_peak_bytes'], 0)
                    self.assertGreater(result['repeated_python_peak_bytes'], 0)
                    self.assertTrue((Path(directory) / f'{role}.docx').read_bytes().startswith(b'PK'))


if __name__ == '__main__':
    unittest.main()
