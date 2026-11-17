"""Mutations of XML document type declarations must not be ignored."""

import unittest

from paritylab.compare import compare_docx
from test_compare import document, mutate


def add_entity(xml: bytes, value: bytes) -> bytes:
    end = xml.index(b'?>') + 2
    declaration = b'<!DOCTYPE w:document [<!ENTITY example "' + value + b'">]>'
    return (xml[:end] + declaration + xml[end:]).replace(b'Hello', b'&example;', 1)


class DoctypeMutationTests(unittest.TestCase):
    def test_changed_entity_declaration_is_a_structural_mismatch(self) -> None:
        original = document()
        first = mutate(original, 'word/document.xml', lambda xml: add_entity(xml, b'one'))
        second = mutate(original, 'word/document.xml', lambda xml: add_entity(xml, b'two'))
        self.assertTrue(compare_docx(first, first)['equivalent'])
        result = compare_docx(first, second)
        self.assertEqual(result['reference_errors'], [])
        self.assertEqual(result['candidate_errors'], [])
        self.assertFalse(result['equivalent'])
        self.assertFalse(result['content_equal'])
        self.assertFalse(result['structure_and_format_equal'])
        self.assertTrue(any(item['part'] == 'word/document.xml' for item in result['differences']))
        self.assertTrue(any(item['kind'] == 'document_content_unverified_dtd' for item in result['differences']))

    def test_doctype_parts_use_exact_bytes_even_for_lexical_changes(self) -> None:
        original = document()
        first = mutate(original, 'word/document.xml', lambda xml: add_entity(xml, b'one'))
        second = mutate(first, 'word/document.xml', lambda xml: xml.replace(b'<w:t>', b'<w:t >', 1))
        self.assertNotEqual(first, second)
        result = compare_docx(first, second)
        self.assertFalse(result['content_equal'])
        self.assertFalse(result['equivalent'])


if __name__ == '__main__':
    unittest.main()
