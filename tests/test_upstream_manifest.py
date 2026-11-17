"""The public corpus must not silently lose or modify upstream examples."""

import argparse
import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paritylab.cli import _upstream_examples, run


class UpstreamManifestTests(unittest.TestCase):
    def test_pinned_files_are_sorted_and_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'b.py').write_bytes(b'b')
            (root / 'a.py').write_bytes(b'a')
            manifest = {name + '.py': hashlib.sha256(name.encode()).hexdigest() for name in ('a', 'b')}
            with (patch('paritylab.cli.UPSTREAM_EXAMPLE_SHA256', manifest),
                  patch('paritylab.cli.UPSTREAM_FIXTURE_SHA256', {})):
                self.assertEqual([path.name for path in _upstream_examples(root)], ['a.py', 'b.py'])

    def test_missing_changed_and_extra_files_fail_before_workers_run(self) -> None:
        expected = hashlib.sha256(b'original').hexdigest()
        for mutation in ('missing', 'changed', 'extra'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                upstream = root / 'upstream' / 'tests'
                upstream.mkdir(parents=True)
                if mutation != 'missing':
                    (upstream / 'example.py').write_bytes(b'changed' if mutation == 'changed' else b'original')
                if mutation == 'extra':
                    (upstream / 'unexpected.py').write_bytes(b'extra')
                args = argparse.Namespace(out=str(root / 'out'), candidate='reference', suite='upstream',
                                          case=None, repeat=1, warmup=0, jobs=1, visual='off',
                                          upstream=str(root / 'upstream'), real_manifest=None)
                with (patch('paritylab.cli.UPSTREAM_EXAMPLE_SHA256', {'example.py': expected}),
                      patch('paritylab.cli.UPSTREAM_FIXTURE_SHA256', {}),
                      patch('paritylab.cli._worker') as worker,
                      self.assertRaisesRegex(ValueError, mutation)):
                    run(args)
                worker.assert_not_called()

    def test_missing_and_mutated_binary_fixtures_stop_before_workers(self) -> None:
        expected = hashlib.sha256(b'\x00original\xff').hexdigest()
        script_hash = hashlib.sha256(b'example').hexdigest()
        for mutation in ('missing', 'changed'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                upstream = root / 'upstream' / 'tests'
                (upstream / 'templates').mkdir(parents=True)
                (upstream / 'example.py').write_bytes(b'example')
                if mutation == 'changed':
                    (upstream / 'templates' / 'input.docx').write_bytes(b'\x00modified\xff')
                args = argparse.Namespace(out=str(root / 'out'), candidate='reference', suite='upstream',
                                          case=None, repeat=1, warmup=0, jobs=1, visual='off',
                                          upstream=str(root / 'upstream'), real_manifest=None)
                with (patch('paritylab.cli.UPSTREAM_EXAMPLE_SHA256', {'example.py': script_hash}),
                      patch('paritylab.cli.UPSTREAM_FIXTURE_SHA256', {'templates/input.docx': expected}),
                      patch('paritylab.cli._worker') as worker,
                      self.assertRaisesRegex(ValueError, re.escape(f"{mutation}=['templates/input.docx']"))):
                    run(args)
                worker.assert_not_called()

    def test_reference_revision_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch('paritylab.cli.MANIFEST_REFERENCE_COMMIT', 'different'):
                with self.assertRaisesRegex(ValueError, 'manifest revision'):
                    _upstream_examples(Path(temporary))
