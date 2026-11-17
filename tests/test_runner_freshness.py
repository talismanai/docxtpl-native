"""A failed or stale worker cannot become a parity pass."""

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paritylab.cli import _worker, run


def arguments(root: Path) -> argparse.Namespace:
    return argparse.Namespace(out=str(root), candidate='reference', suite='synthetic',
                              case=['strict_undefined'], repeat=1, warmup=0, jobs=1,
                              visual='off', upstream='unused', real_manifest=None)


class RunnerFreshnessTests(unittest.TestCase):
    def test_existing_report_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            report = out / 'report.json'
            report.write_text('previous report')
            with self.assertRaisesRegex(ValueError, 'output directory is not empty'):
                run(arguments(out))
            self.assertEqual(report.read_text(), 'previous report')

    def test_preexisting_worker_result_is_not_read_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)
            stale = out / 'worker.json'
            stale.write_text('{"status":"ok"}')
            with patch('paritylab.cli.subprocess.run') as process:
                result = _worker({}, out)
            process.assert_not_called()
            self.assertEqual(result['status'], 'worker_error')
            self.assertEqual(stale.read_text(), '{"status":"ok"}')

    def test_nonzero_exit_cannot_use_a_written_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary)

            def fail(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                (out / 'worker.json').write_text('{"status":"ok"}')
                return subprocess.CompletedProcess(args=[], returncode=7, stdout='', stderr='failed')

            with patch('paritylab.cli.subprocess.run', side_effect=fail):
                result = _worker({}, out)
            self.assertEqual(result['status'], 'worker_error')
            self.assertIn('worker exit 7', result['exception']['message'])

    def test_expected_exception_requires_the_declared_render_failure(self) -> None:
        expected = {'type': 'jinja2.exceptions.UndefinedError', 'message': "'missing' is undefined"}
        for kind, expected_exit, expected_status in (
            ('matching_infrastructure_error', 1, 'reference_error'),
            ('unexpected_success', 1, 'reference_error'),
            ('matching_setup_exception', 1, 'reference_error'),
            ('declared_exception', 0, 'pass'),
            ('candidate_worker_error', 1, 'gap'),
            ('candidate_setup_exception', 1, 'gap'),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                def worker(request: dict[str, object], out: Path) -> dict[str, object]:
                    if kind == 'matching_infrastructure_error':
                        return {'status': 'error', 'exception': {'type': 'builtins.ImportError',
                                'message': 'missing dependency'}, 'samples': [], 'outputs': []}
                    if kind == 'unexpected_success':
                        return {'status': 'ok', 'exception': None, 'samples': [], 'outputs': []}
                    status = 'worker_error' if kind == 'candidate_worker_error' and out.name == 'candidate' else 'error'
                    stage = 'setup' if kind == 'matching_setup_exception' or (
                        kind == 'candidate_setup_exception' and out.name == 'candidate') else 'render'
                    return {'status': status, 'stage': stage, 'exception': expected, 'samples': [], 'outputs': []}

                with patch('paritylab.cli._worker', side_effect=worker):
                    self.assertEqual(run(arguments(root)), expected_exit)
                report = json.loads((root / 'report.json').read_text())
                self.assertEqual(report['cases'][0]['status'], expected_status)
