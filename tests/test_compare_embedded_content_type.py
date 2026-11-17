"""A declared embedded Word package must be validated regardless of its name."""

from __future__ import annotations

import io
import unittest
import xml.etree.ElementTree as etree
import zipfile

from paritylab.compare import CT, compare_docx
from test_compare import document, mutate


WORD_PACKAGE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
PART = 'word/embeddings/nested.bin'


def embedded(payload: bytes, *, default: bool, content_type: str = WORD_PACKAGE) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(document('outer'))) as source, zipfile.ZipFile(output, 'w') as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == '[Content_Types].xml':
                root = etree.fromstring(data)
                if default:
                    etree.SubElement(root, f'{{{CT}}}Default', Extension='bin', ContentType=content_type)
                else:
                    etree.SubElement(root, f'{{{CT}}}Override', PartName=f'/{PART}', ContentType=content_type)
                data = etree.tostring(root)
            target.writestr(item, data)
        target.writestr(PART, payload)
    return output.getvalue()


class EmbeddedContentTypeTests(unittest.TestCase):
    def test_invalid_identical_package_fails_for_default_and_override(self) -> None:
        for default in (False, True):
            with self.subTest(default=default):
                archive = embedded(b'not a zip', default=default)
                result = compare_docx(archive, archive)
                self.assertFalse(result['equivalent'])
                self.assertTrue(any(PART in error for error in result['reference_errors']))
                self.assertTrue(any(PART in error for error in result['candidate_errors']))

    def test_valid_package_and_other_binary_content_pass(self) -> None:
        for default in (False, True):
            with self.subTest(default=default):
                archive = embedded(document('inner'), default=default)
                self.assertTrue(compare_docx(archive, archive)['equivalent'])
        binary = embedded(b'not a zip', default=False, content_type='application/octet-stream')
        self.assertTrue(compare_docx(binary, binary)['equivalent'])

    def test_changed_embedded_package_has_nested_diagnostic(self) -> None:
        first = embedded(document('one'), default=False)
        second = embedded(document('two'), default=False)
        result = compare_docx(first, second)
        self.assertFalse(result['equivalent'])
        difference = next(item for item in result['differences'] if item['part'] == PART)
        self.assertFalse(difference['embedded_docx']['equivalent'])

    def test_override_takes_precedence_over_extension_default(self) -> None:
        binary = embedded(b'not a zip', default=False, content_type='application/octet-stream')

        def add_default(data: bytes) -> bytes:
            root = etree.fromstring(data)
            etree.SubElement(root, f'{{{CT}}}Default', Extension='bin', ContentType=WORD_PACKAGE)
            return etree.tostring(root)

        archive = mutate(binary, '[Content_Types].xml', add_default)
        self.assertTrue(compare_docx(archive, archive)['equivalent'])

    def test_content_type_names_are_case_insensitive(self) -> None:
        def change_case(data: bytes) -> bytes:
            root = etree.fromstring(data)
            for entry in root:
                if entry.get('Extension') == 'bin':
                    entry.set('Extension', 'BIN')
                if entry.get('PartName') == f'/{PART}':
                    entry.set('PartName', f'/{PART.upper()}')
            return etree.tostring(root)

        for default in (False, True):
            with self.subTest(default=default):
                valid = mutate(embedded(document('inner'), default=default), '[Content_Types].xml', change_case)
                self.assertTrue(compare_docx(valid, valid)['equivalent'])
                invalid = mutate(embedded(b'not a zip', default=default), '[Content_Types].xml', change_case)
                result = compare_docx(invalid, invalid)
                self.assertFalse(result['equivalent'])
                self.assertTrue(any(PART in error and 'invalid ZIP' in error for error in result['candidate_errors']))


if __name__ == '__main__':
    unittest.main()
