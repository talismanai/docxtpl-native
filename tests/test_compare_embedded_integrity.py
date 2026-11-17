"""Mutations of embedded DOCX parts must not produce parity passes."""

import unittest

from paritylab.compare import compare_docx
from test_compare import change_zip_timestamps, corrupt_inner_crc, document, embed_document, mutate


class EmbeddedIntegrityMutationTests(unittest.TestCase):
    def test_matching_invalid_embedded_documents_fail(self) -> None:
        outer = document('outer')
        invalid_xml = mutate(document('inner'), 'word/document.xml', lambda _: b'<broken>')
        for inner in (b'not a zip', corrupt_inner_crc(), invalid_xml):
            with self.subTest(inner=inner[:12]):
                damaged = embed_document(outer, inner)
                result = compare_docx(damaged, damaged)
                self.assertFalse(result['equivalent'])
                self.assertFalse(result['content_equal'])
                self.assertFalse(result['structure_and_format_equal'])
                self.assertTrue(any('word/embeddings/nested.docx' in error
                                    for error in result['reference_errors']))
                self.assertTrue(any('word/embeddings/nested.docx' in error
                                    for error in result['candidate_errors']))

    def test_valid_identical_embedded_document_passes(self) -> None:
        outer = embed_document(document('outer'), document('inner'))
        self.assertTrue(compare_docx(outer, outer)['equivalent'])

    def test_nested_invalid_document_is_reported_at_each_level(self) -> None:
        inner = embed_document(document('middle'), b'not a zip')
        outer = embed_document(document('outer'), inner)
        result = compare_docx(outer, outer)
        self.assertFalse(result['equivalent'])
        self.assertTrue(any(error.count('word/embeddings/nested.docx') == 2
                            for error in result['candidate_errors']))

    def test_nesting_beyond_limit_is_unverified(self) -> None:
        nested = document('innermost')
        for _ in range(9):
            nested = embed_document(document('outer'), nested)
        result = compare_docx(nested, nested)
        self.assertFalse(result['equivalent'])
        self.assertTrue(any('nesting limit exceeded' in error for error in result['candidate_errors']))

    def test_zip_metadata_mutation_is_visible_separately_from_structure(self) -> None:
        original = document('content')
        changed = change_zip_timestamps(original)
        result = compare_docx(original, changed)
        self.assertTrue(result['equivalent'])
        self.assertFalse(result['archive_bytes_equal'])
        self.assertNotEqual(result['reference_sha256'], result['candidate_sha256'])
        self.assertTrue(compare_docx(original, original)['archive_bytes_equal'])

if __name__ == '__main__':
    unittest.main()
