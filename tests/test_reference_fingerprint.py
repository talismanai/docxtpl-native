import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import docxtpl
from paritylab.reference_fingerprint import REFERENCE_COMMIT, fingerprint, installed_reference


class ReferenceFingerprintTests(unittest.TestCase):
    def test_installed_source_matches_contract_commit(self):
        contract = json.loads((Path(__file__).resolve().parents[1] / 'compatibility' / 'contract.json').read_text())
        self.assertEqual(REFERENCE_COMMIT, contract['reference_commit'])
        self.assertTrue(installed_reference()['matched'])

    def test_changed_missing_and_extra_files_do_not_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.py'
            source.write_bytes(b'expected')
            expected = {'source.py': hashlib.sha256(b'expected').hexdigest()}
            self.assertTrue(fingerprint(root, expected)['matched'])
            source.write_bytes(b'changed')
            self.assertFalse(fingerprint(root, expected)['matched'])
            source.write_bytes(b'expected')
            (root / 'extra.py').write_bytes(b'extra')
            self.assertFalse(fingerprint(root, expected)['matched'])
            (root / 'extra.py').unlink()
            source.unlink()
            self.assertFalse(fingerprint(root, expected)['matched'])

    def test_mutated_reference_cannot_be_counted_as_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(Path(docxtpl.__file__).parent, root / 'docxtpl',
                            ignore=shutil.ignore_patterns('__pycache__'))
            target = root / 'docxtpl' / 'richtext.py'
            target.write_bytes(target.read_bytes() + b'\n# deliberately mutated\n')
            environment = os.environ.copy()
            environment['PYTHONPATH'] = str(root) + os.pathsep + environment.get('PYTHONPATH', '')
            result = subprocess.run([sys.executable, '-m', 'paritylab.cli', '--candidate', 'reference',
                                     '--suite', 'synthetic', '--case', 'plain', '--repeat', '1',
                                     '--warmup', '0', '--visual', 'off', '--out', str(root / 'out')],
                                    env=environment, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / 'out' / 'report.json').read_text())
            self.assertEqual(report['counts']['passed'], 0)
            self.assertEqual(report['cases'][0]['reference']['status'], 'error')
            self.assertIn('does not match the pinned reference',
                          report['cases'][0]['reference']['exception']['message'])


if __name__ == '__main__':
    unittest.main()
