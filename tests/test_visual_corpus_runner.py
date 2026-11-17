"""A visual runner error must not count as a successful comparison."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import check_visual_corpus


class VisualCorpusRunnerTests(unittest.TestCase):
    def test_error_status_with_equal_flag_is_not_counted_as_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case_root = root / 'cases' / 'synthetic' / 'case'
            for role in ('reference', 'candidate'):
                folder = case_root / role / 'documents'
                folder.mkdir(parents=True)
                (folder / 'document.docx').write_bytes(b'synthetic input')
            digest = hashlib.sha256(b'synthetic input').hexdigest()
            report = {
                'mode': 'differential', 'candidate': 'docxtpl_native',
                'capabilities': {'native_available': True, 'reference_fallback': False},
                'cases': [{
                    'id': 'synthetic/case', 'status': 'pass',
                    'reference': {'reference_source': {'matched': True}},
                    'candidate': {'reference_source': {'matched': True}},
                    'documents': [{'file': 'document.docx', 'equivalent': True,
                                   'reference_sha256': digest, 'candidate_sha256': digest}],
                }],
            }
            report_path = root / 'report.json'
            report_path.write_text(json.dumps(report))
            report_hash = hashlib.sha256(report_path.read_bytes()).hexdigest()
            output = root / 'output'
            argv = ['check_visual_corpus', '--report', str(report_path), '--report-sha256', report_hash,
                    '--out', str(output)]
            with patch.object(sys, 'argv', argv), patch.object(check_visual_corpus.visual, 'available', return_value=True), \
                 patch.object(check_visual_corpus.visual, 'compare_visual', return_value={'status': 'error', 'equal': True}), \
                 self.assertRaises(SystemExit):
                check_visual_corpus.main()
            summary = json.loads((output / 'summary.json').read_text())
            self.assertEqual((summary['pairs'], summary['equal'], summary['errors']), (1, 0, 1))


if __name__ == '__main__':
    unittest.main()
