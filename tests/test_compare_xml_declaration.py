"""Mutation checks for XML declaration semantics in OPC parts."""

import unittest

from paritylab.compare import compare_docx
from test_compare import document, mutate


def with_declaration(blob: bytes, declaration: bytes, part: str = 'word/document.xml') -> bytes:
    def replace(xml: bytes) -> bytes:
        body = xml.split(b'?>', 1)[1] if xml.startswith(b'<?xml') else xml
        return declaration + body.lstrip()

    return mutate(blob, part, replace)


class XmlDeclarationComparisonTests(unittest.TestCase):
    def test_semantic_declaration_mutations_are_detected(self) -> None:
        original = document('same')
        declarations = (
            (b'<?xml version="1.0" standalone="yes"?>', b'<?xml version="1.0" standalone="no"?>'),
            (b'<?xml version="1.0"?>', b'<?xml version="1.1"?>'),
        )
        for left, right in declarations:
            with self.subTest(left=left, right=right):
                result = compare_docx(with_declaration(original, left), with_declaration(original, right))
                self.assertFalse(result['equivalent'])
                self.assertTrue(result['content_equal'])
                self.assertFalse(result['structure_and_format_equal'])

    def test_lexical_declaration_changes_remain_equivalent(self) -> None:
        original = document('same')
        left = with_declaration(original, b'<?xml version="1.0" standalone="yes"?>')
        right = with_declaration(original, b"<?xml version='1.0' standalone='yes' ?>")
        self.assertTrue(compare_docx(left, right)['equivalent'])

    def test_standalone_mutations_are_detected_in_other_xml_parts(self) -> None:
        original = document('same')
        for part in ('[Content_Types].xml', 'word/styles.xml', 'word/_rels/document.xml.rels'):
            with self.subTest(part=part):
                left = with_declaration(original, b'<?xml version="1.0" standalone="yes"?>', part)
                right = with_declaration(original, b'<?xml version="1.0" standalone="no"?>', part)
                result = compare_docx(left, right)
                self.assertFalse(result['equivalent'])
                self.assertTrue(any(change['part'] == part for change in result['differences']))

    def test_omitted_standalone_is_equivalent_to_no(self) -> None:
        original = document('same')
        left = with_declaration(original, b'<?xml version="1.0"?>')
        right = with_declaration(original, b'<?xml version="1.0" standalone="no"?>')
        self.assertTrue(compare_docx(left, right)['equivalent'])
