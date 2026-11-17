"""Mutation checks for the ZIP diagnostics used in clean-wheel evidence."""

from __future__ import annotations

import struct
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from scripts.inspect_zip_pair import inspect


class InspectZipPairTests(unittest.TestCase):
    def test_matching_corrupt_data_descriptors_are_reported(self) -> None:
        class Unseekable(BytesIO):
            def seek(self, offset: int, whence: int = 0) -> int:
                raise OSError('not seekable')

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'corrupt.zip'
            stream = Unseekable()
            with ZipFile(stream, 'w') as archive:
                archive.writestr('part.xml', b'body')
            data = bytearray(stream.getvalue())
            descriptor = data.index(b'PK\x07\x08')
            data[descriptor + 4] ^= 1
            path.write_bytes(data)

            result = inspect(path, path)
            self.assertEqual(result['descriptor_differences'], [])
            self.assertEqual(result['reference_descriptor_errors'], ['part.xml'])
            self.assertEqual(result['candidate_descriptor_errors'], ['part.xml'])

    def test_detects_unsigned_data_descriptor_mutation(self) -> None:
        class Unseekable(BytesIO):
            def seek(self, offset: int, whence: int = 0) -> int:
                raise OSError('not seekable')

        for zip64 in (False, True):
            with self.subTest(zip64=zip64), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original, mutated = root / 'original.zip', root / 'mutated.zip'
                stream = Unseekable()
                with ZipFile(stream, 'w') as archive, archive.open('part.xml', 'w', force_zip64=zip64) as part:
                    part.write(b'body')
                data = bytearray(stream.getvalue())
                descriptor = data.index(b'PK\x07\x08')
                del data[descriptor:descriptor + 4]
                end = data.rindex(b'PK\x05\x06')
                central_offset = struct.unpack_from('<I', data, end + 16)[0]
                struct.pack_into('<I', data, end + 16, central_offset - 4)
                original.write_bytes(data)
                data[descriptor] ^= 1
                mutated.write_bytes(data)
                result = inspect(original, mutated)
                self.assertEqual(result['uncompressed_differences'], [])
                self.assertEqual(result['descriptor_differences'], ['part.xml'])
                self.assertEqual(result['reference_descriptor_errors'], [])
                self.assertEqual(result['candidate_descriptor_errors'], ['part.xml'])

    def test_detects_data_descriptor_mutation(self) -> None:
        class Unseekable(BytesIO):
            def seek(self, offset: int, whence: int = 0) -> int:
                raise OSError('not seekable')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, mutated = root / 'original.zip', root / 'mutated.zip'
            stream = Unseekable()
            with ZipFile(stream, 'w') as archive:
                archive.writestr('part.xml', b'body')
            data = bytearray(stream.getvalue())
            original.write_bytes(data)
            descriptor = data.index(b'PK\x07\x08')
            data[descriptor + 4] ^= 1
            mutated.write_bytes(data)
            result = inspect(original, mutated)
            self.assertEqual(result['uncompressed_differences'], [])
            self.assertEqual(result['descriptor_differences'], ['part.xml'])
            self.assertEqual(result['reference_descriptor_errors'], [])
            self.assertEqual(result['candidate_descriptor_errors'], ['part.xml'])

    def test_detects_zip64_data_descriptor_mutation(self) -> None:
        class Unseekable(BytesIO):
            def seek(self, offset: int, whence: int = 0) -> int:
                raise OSError('not seekable')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, mutated = root / 'original.zip', root / 'mutated.zip'
            stream = Unseekable()
            with ZipFile(stream, 'w') as archive, archive.open('part.xml', 'w', force_zip64=True) as part:
                part.write(b'body')
            data = bytearray(stream.getvalue())
            original.write_bytes(data)
            descriptor = data.index(b'PK\x07\x08')
            data[descriptor + 4] ^= 1
            mutated.write_bytes(data)
            result = inspect(original, mutated)
            self.assertEqual(result['uncompressed_differences'], [])
            self.assertEqual(result['descriptor_differences'], ['part.xml'])
            self.assertEqual(result['reference_descriptor_errors'], [])
            self.assertEqual(result['candidate_descriptor_errors'], ['part.xml'])

    def test_detects_central_directory_version_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, mutated = root / 'original.zip', root / 'mutated.zip'
            with ZipFile(original, 'w') as archive:
                archive.writestr('part.xml', b'body')
            data = bytearray(original.read_bytes())
            central = data.index(b'PK\x01\x02')
            struct.pack_into('<H', data, central + 6, 21)
            mutated.write_bytes(data)
            result = inspect(original, mutated)
            self.assertEqual(result['uncompressed_differences'], [])
            self.assertEqual(result['metadata_difference_counts']['extract_version'], 1)  # ty: ignore[not-subscriptable]

    def test_detects_archive_comment_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, mutated = root / 'original.zip', root / 'mutated.zip'
            for path, comment in ((original, b''), (mutated, b'changed')):
                with ZipFile(path, 'w') as archive:
                    archive.writestr('part.xml', b'body')
                    archive.comment = comment
            result = inspect(original, mutated)
            self.assertEqual(result['uncompressed_differences'], [])
            self.assertFalse(result['archive_comment_equal'])

    def test_detects_local_header_timestamp_without_central_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original.zip'
            mutated = root / 'mutated.zip'
            info = ZipInfo('part.xml', (2020, 1, 1, 0, 0, 0))
            with ZipFile(original, 'w') as archive:
                archive.writestr(info, b'body')
            data = bytearray(original.read_bytes())
            struct.pack_into('<H', data, 10, 1)
            mutated.write_bytes(data)

            result = inspect(original, mutated)
            self.assertEqual(result['uncompressed_differences'], [])
            self.assertEqual(result['metadata_difference_counts']['date_time'], 0)  # ty: ignore[not-subscriptable]
            self.assertEqual(result['local_header_difference_counts']['dos_time'], 1)  # ty: ignore[not-subscriptable]

    def test_distinguishes_payload_compression_and_timestamp_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def write(name: str, payload: bytes, compression: int, second: int) -> Path:
                path = root / name
                info = ZipInfo('part.xml', (2020, 1, 1, 0, 0, second))
                info.compress_type = compression
                with ZipFile(path, 'w') as archive:
                    archive.writestr(info, payload)
                return path

            original = write('original.zip', b'body', ZIP_STORED, 0)
            payload = inspect(original, write('payload.zip', b'changed', ZIP_STORED, 0))
            self.assertEqual(payload['uncompressed_differences'], ['part.xml'])
            self.assertEqual(payload['compressed_differences'], ['part.xml'])

            compressed = inspect(original, write('compressed.zip', b'body', ZIP_DEFLATED, 0))
            self.assertEqual(compressed['uncompressed_differences'], [])
            self.assertEqual(compressed['compressed_differences'], ['part.xml'])

            timestamp = inspect(original, write('timestamp.zip', b'body', ZIP_STORED, 2))
            self.assertEqual(timestamp['uncompressed_differences'], [])
            self.assertEqual(timestamp['compressed_differences'], [])
            self.assertEqual(timestamp['metadata_difference_counts']['date_time'], 1)  # ty: ignore[not-subscriptable]
            self.assertFalse(timestamp['archive_bytes_equal'])


if __name__ == '__main__':
    unittest.main()
