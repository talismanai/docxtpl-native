import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import docxtpl
from paritylab.check_wheel_install import verify_sources
from paritylab.spec_coverage import assess
from paritylab.spec_test_coverage import assess_tests
from paritylab.standalone_evidence import CASES, CHECKER_SHA256, REFERENCE, REFERENCE_HELPER_SHA256, valid_wheel_evidence


class StandaloneCoverageTests(unittest.TestCase):
    def setUp(self):
        self.case = 'standalone/wheel_python311'
        self.versions = {'docxtpl': '0.20.2', 'Jinja2': '3.1.6', 'python-docx': '1.2.0',
                         'lxml': '6.1.1', 'docxcompose': '1.4.0'}
        self.root = '/tmp/docxtpl-wheel-smoke-fixture/venv'
        self.evidence = {
            'case': self.case, 'status': 'pass', 'mode': 'local_wheel_install',
            'source_revision': 'revision', 'python': '3.11.17', 'platform': 'linux', 'machine': 'x86_64',
            'environment': self.root,
            'module': self.root + '/lib/site-packages/docxtpl_native/__init__.py',
            'extension': self.root + '/lib/site-packages/docxtpl_native/_native.cpython-311.so',
            'laboratory': self.root + '/lib/site-packages/paritylab/reference_fingerprint.py',
            'wheel': 'docxtpl_native-0.1.0-cp311-cp311-linux_x86_64.whl',
            'wheel_sha256': 'a' * 64, 'extension_sha256': 'b' * 64,
            'fingerprint_helper_sha256': REFERENCE_HELPER_SHA256,
            'checker_sha256': CHECKER_SHA256,
            'dependency_site': '/tmp/dependencies/site-packages',
            'reference_module': '/tmp/dependencies/site-packages/docxtpl/__init__.py',
            'reference_source': copy.deepcopy(REFERENCE), 'dependencies': self.versions.copy(),
            'native_available': True, 'reference_fallback': False, 'rendered_text': 'Café Ada',
        }
        self.catalog = {'notice': 'enumerated only', 'contracts': [{
            'id': 'deployment.python311', 'description': 'wheel', 'reference': 'contract.json',
            'cases': [self.case], 'implementation': 'unassessed', 'scope': 'preserved', 'reviewed': False,
            'partitions': {'covered': ['local_wheel'], 'pending': ['portability']},
            'partition_cases': {'local_wheel': [self.case]},
        }]}

    def test_local_wheel_is_evidence_without_qualifying_the_contract(self):
        result = assess(self.catalog, None, 'revision', expected_versions=self.versions,
                        standalone_reports=[self.evidence])
        self.assertFalse(result['evidence_valid'])
        self.assertEqual(result['standalone_case_statuses'], {self.case: 'pass'})
        self.assertEqual(result['counts']['examples_passed'], 1)
        self.assertEqual(result['qualified'], 0)
        self.assertFalse(assess_tests(self.catalog, set(CASES))['complete'])

    def test_mutated_wheel_evidence_never_becomes_a_pass(self):
        mutations = [
            ('source_revision', 'other'), ('python', '3.12.14'), ('python', None),
            ('platform', 'darwin'), ('status', 'error'), ('native_available', False),
            ('machine', 'aarch64'),
            ('reference_fallback', True), ('wheel_sha256', 'invalid'),
            ('extension_sha256', 'A' * 64), ('rendered_text', 'other'),
            ('fingerprint_helper_sha256', 'a' * 64),
            ('checker_sha256', 'a' * 64),
            ('reference_module', '/outside/docxtpl/__init__.py'),
            ('module', '/outside/__init__.py'),
            ('extension', self.root + '/../outside/_native.cpython-311.so'),
            ('extension', self.root + '/lib/site-packages/docxtpl_native/_native.cpython-312.so'),
            ('environment', '/tmp'), ('wheel', '../unsafe.whl'),
            ('wheel', 'docxtpl_native-0.1.0-cp312-cp312-linux_x86_64.whl'),
            ('dependencies', {'docxtpl': '0.20.1', 'Jinja2': '3.1.6'}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                evidence = copy.deepcopy(self.evidence)
                evidence[field] = value
                self.assertFalse(valid_wheel_evidence(self.case, evidence, 'revision', self.versions))
                result = assess(self.catalog, None, 'revision', expected_versions=self.versions,
                                standalone_reports=[evidence])
                self.assertEqual(result['contracts'][0]['state'], 'unverified')
        changed = copy.deepcopy(self.evidence)
        changed['reference_source']['files']['template.py'] = 'changed'
        self.assertFalse(valid_wheel_evidence(self.case, changed, 'revision', self.versions))
        self.assertEqual(assess(self.catalog, None, 'revision', expected_versions=self.versions,
                                standalone_reports=[changed])['contracts'][0]['state'], 'unverified')
        self.assertFalse(valid_wheel_evidence(self.case, self.evidence, 'revision'))
        self.assertFalse(valid_wheel_evidence(self.case, self.evidence, 'revision', {}))

    def test_duplicate_standalone_results_are_ineligible(self):
        result = assess(self.catalog, None, 'revision', expected_versions=self.versions,
                        standalone_reports=[self.evidence, self.evidence])
        self.assertEqual(result['contracts'][0]['state'], 'unverified')

    def test_differential_case_cannot_spoof_installation(self):
        report = {'source_revision': 'revision', 'mode': 'differential', 'candidate': 'docxtpl_native',
                  'reference': 'docxtpl', 'visual_policy': 'required', 'status': 'pass',
                  'counts': {'total': 1, 'passed': 1, 'gaps_or_errors': 0},
                  'capabilities': {'native_available': True, 'reference_fallback': False},
                  'selection': {'suite': 'all', 'cases': [], 'protocol_cases': [], 'real_manifest': False,
                                'complete': True},
                  'cases': [{'id': self.case, 'status': 'pass'}]}
        result = assess(self.catalog, report, 'revision', expected_reference_source=None)
        self.assertTrue(result['evidence_valid'])
        self.assertEqual(result['contracts'][0]['state'], 'unverified')

    def test_registered_cases_have_an_executable_runner_and_distinct_versions(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual({item['python'] for item in CASES.values()}, {'3.11', '3.12'})
        self.assertTrue(all((root / item['runner']).is_file() for item in CASES.values()))

    def test_cli_accepts_standalone_evidence_without_a_differential_report(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            catalog = folder / 'catalog.json'
            evidence = folder / 'wheel.json'
            output = folder / 'result.json'
            catalog.write_text(json.dumps(self.catalog))
            evidence.write_text(json.dumps(self.evidence))
            subprocess.run([sys.executable, str(root / 'scripts/spec_coverage.py'),
                            '--catalog', str(catalog), '--report', str(folder / 'missing.json'),
                            '--revision', 'revision', '--standalone-report', str(evidence),
                            '--reference-contract', str(root / 'compatibility/contract.json'),
                            '--out', str(output), '--markdown-out', str(folder / 'result.md')],
                           cwd=root, check=True, capture_output=True, text=True)
            result = json.loads(output.read_text())
            self.assertEqual(result['standalone_case_statuses'][self.case], 'pass')
            self.assertEqual(result['qualified'], 0)

    def test_checker_independently_rejects_mutated_reference_and_wheel_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dependency_site = root / 'dependencies'
            reference = dependency_site / 'docxtpl'
            shutil.copytree(Path(docxtpl.__file__).parent, reference,
                            ignore=shutil.ignore_patterns('__pycache__'))
            environment = root / 'venv'
            package = environment / 'site-packages' / 'docxtpl_native'
            package.mkdir(parents=True)
            module, extension = package / '__init__.py', package / '_native.so'
            module.write_bytes(b'package')
            extension.write_bytes(b'extension')
            helper = environment / 'site-packages' / 'paritylab' / 'reference_fingerprint.py'
            helper.parent.mkdir()
            source_helper = Path(__file__).resolve().parents[1] / 'python/paritylab/reference_fingerprint.py'
            helper.write_bytes(source_helper.read_bytes())
            observed = {'module': str(module), 'extension': str(extension), 'laboratory': str(helper),
                        'extension_sha256': hashlib.sha256(extension.read_bytes()).hexdigest(),
                        'reference_module': str(reference / '__init__.py'),
                        'reference_source': copy.deepcopy(REFERENCE)}
            self.assertEqual(verify_sources(observed, dependency_site, environment), REFERENCE_HELPER_SHA256)
            helper.write_bytes(helper.read_bytes() + b'\n# mutated helper\n')
            with self.assertRaisesRegex(AssertionError, 'fingerprint helper differs'):
                verify_sources(observed, dependency_site, environment)
            helper.write_bytes(source_helper.read_bytes())
            target = reference / 'template.py'
            target.write_bytes(target.read_bytes() + b'\n# mutated reference\n')
            with self.assertRaisesRegex(AssertionError, 'independent source check'):
                verify_sources(observed, dependency_site, environment)


if __name__ == '__main__':
    unittest.main()
