"""Mutations of top-level ZIP entries must be reported as invalid documents."""

import io
import struct
import unittest
import zipfile

from paritylab.compare import compare_docx, inspect_docx
from test_compare import corrupt_inner_crc, document


def corrupt_deflate_entry(blob: bytes, name: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as target:
        for entry in source.infolist():
            target.writestr(entry.filename, source.read(entry))
    data = bytearray(output.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offset = archive.getinfo(name).header_offset
    name_length, extra_length = struct.unpack_from('<HH', data, offset + 26)
    data[offset + 30 + name_length + extra_length] = 0xFF
    return bytes(data)


class ZipEntryMutationTests(unittest.TestCase):
    def test_crc_failure_is_reported_with_the_part_name(self) -> None:
        damaged = corrupt_inner_crc()
        inspection = inspect_docx(damaged)
        self.assertTrue(any('word/document.xml' in error and 'CRC' in error for error in inspection['errors']))

        result = compare_docx(document(), damaged)
        self.assertFalse(result['equivalent'])
        self.assertTrue(result['candidate_errors'])

    def test_matching_crc_failures_are_not_equivalent(self) -> None:
        damaged = corrupt_inner_crc()
        result = compare_docx(damaged, damaged)
        self.assertFalse(result['equivalent'])
        self.assertFalse(result['content_equal'])
        self.assertFalse(result['structure_and_format_equal'])
        self.assertTrue(result['reference_errors'])
        self.assertTrue(result['candidate_errors'])

    def test_corrupt_deflate_stream_is_reported(self) -> None:
        damaged = corrupt_deflate_entry(document(), 'word/document.xml')
        result = compare_docx(document(), damaged)
        self.assertFalse(result['equivalent'])
        self.assertTrue(any('word/document.xml' in error and 'ZIP entry read failed' in error
                            for error in result['candidate_errors']))


if __name__ == '__main__':
    unittest.main()
