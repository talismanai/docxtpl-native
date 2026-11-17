"""Mutation controls for independently checking saved rendered pixels."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.audit_visual_corpus import audit_pages


class VisualCorpusAuditTests(unittest.TestCase):
    def test_detects_changed_pixels_and_missing_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference, candidate = root / 'reference', root / 'candidate'
            reference.mkdir()
            candidate.mkdir()
            Image.new('RGB', (2, 2), 'blue').save(reference / 'page-0001.png')
            Image.new('RGB', (2, 2), 'blue').save(candidate / 'page-0001.png')
            self.assertEqual(len(audit_pages(reference, candidate, 1)), 1)
            Image.new('RGB', (2, 2), 'red').save(candidate / 'page-0001.png')
            with self.assertRaisesRegex(RuntimeError, 'pixels differ'):
                audit_pages(reference, candidate, 1)
            with self.assertRaisesRegex(RuntimeError, 'missing or unexpected'):
                audit_pages(reference, candidate, 2)
            with self.assertRaisesRegex(RuntimeError, 'missing or unexpected'):
                audit_pages(reference, candidate, 0)


if __name__ == '__main__':
    unittest.main()
