"""Report entry payload and metadata differences between two ZIP files."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path
from zipfile import ZipFile, ZipInfo


def local_header(archive: bytes, entry: ZipInfo) -> tuple[dict[str, object], int]:
    header = struct.unpack_from('<IHHHHHIIIHH', archive, entry.header_offset)
    if header[0] != 0x04034B50:
        raise ValueError(f'invalid local ZIP header: {entry.filename}')
    names = ('version_needed', 'flags', 'compression', 'dos_time', 'dos_date',
             'crc32', 'compressed_size', 'uncompressed_size')
    start_name = entry.header_offset + 30
    start_extra = start_name + header[-2]
    start = start_extra + header[-1]
    if start > len(archive):
        raise ValueError(f'truncated local ZIP header: {entry.filename}')
    fields: dict[str, object] = dict(zip(names, header[1:9], strict=True))
    fields.update({'filename': archive[start_name:start_extra], 'extra': archive[start_extra:start]})
    return fields, start


def compressed_payload(archive: bytes, entry: ZipInfo) -> bytes:
    _, start = local_header(archive, entry)
    payload = archive[start:start + entry.compress_size]
    if len(payload) != entry.compress_size:
        raise ValueError(f'truncated ZIP entry: {entry.filename}')
    return payload


def data_descriptor(archive: bytes, entry: ZipInfo) -> bytes | None:
    if not entry.flag_bits & 0x08:
        return None
    header, start = local_header(archive, entry)
    start += entry.compress_size
    signature = b'PK\x07\x08'
    has_signature = archive[start:start + 4] == signature
    if has_signature and entry.CRC == 0x08074B50:
        raise ValueError(f'ambiguous ZIP data descriptor signature: {entry.filename}')
    zip64 = (header['compressed_size'] == 0xFFFFFFFF or header['uncompressed_size'] == 0xFFFFFFFF
             or entry.compress_size > 0xFFFFFFFF or entry.file_size > 0xFFFFFFFF)
    length = (20 if zip64 else 12) + (4 if has_signature else 0)
    descriptor = archive[start:start + length]
    if len(descriptor) != length:
        raise ValueError(f'truncated ZIP data descriptor: {entry.filename}')
    return descriptor


def invalid_descriptor(archive: bytes, entry: ZipInfo) -> bool:
    descriptor = data_descriptor(archive, entry)
    if descriptor is None:
        return False
    if descriptor.startswith(b'PK\x07\x08'):
        descriptor = descriptor[4:]
    fmt = '<IQQ' if len(descriptor) == 20 else '<III'
    crc, compressed_size, file_size = struct.unpack(fmt, descriptor)
    return (crc, compressed_size, file_size) != (entry.CRC, entry.compress_size, entry.file_size)


def inspect(reference: Path, candidate: Path) -> dict[str, object]:
    reference_bytes, candidate_bytes = reference.read_bytes(), candidate.read_bytes()
    with ZipFile(reference) as left, ZipFile(candidate) as right:
        entries_left, entries_right = left.infolist(), right.infolist()
        names_left = [entry.filename for entry in entries_left]
        names_right = [entry.filename for entry in entries_right]
        if names_left != names_right or len(names_left) != len(set(names_left)):
            raise ValueError('ZIP entry order differs or contains duplicate names')
        fields = ('date_time', 'compress_type', 'compress_size', 'file_size', 'CRC',
                  'external_attr', 'internal_attr', 'flag_bits', 'create_system',
                  'create_version', 'extract_version', 'reserved', 'volume', 'header_offset',
                  'extra', 'comment')
        local_left = [local_header(reference_bytes, entry)[0] for entry in entries_left]
        local_right = [local_header(candidate_bytes, entry)[0] for entry in entries_right]
        local_fields = ('version_needed', 'flags', 'compression', 'dos_time', 'dos_date',
                        'crc32', 'compressed_size', 'uncompressed_size', 'filename', 'extra')
        return {
            'entries': len(entries_left),
            'uncompressed_differences': [a.filename for a, b in zip(entries_left, entries_right, strict=True)
                                         if left.read(a) != right.read(b)],
            'compressed_differences': [a.filename for a, b in zip(entries_left, entries_right, strict=True)
                                       if compressed_payload(reference_bytes, a) != compressed_payload(candidate_bytes, b)],
            'descriptor_differences': [a.filename for a, b in zip(entries_left, entries_right, strict=True)
                                       if data_descriptor(reference_bytes, a) != data_descriptor(candidate_bytes, b)],
            'reference_descriptor_errors': [entry.filename for entry in entries_left
                                            if invalid_descriptor(reference_bytes, entry)],
            'candidate_descriptor_errors': [entry.filename for entry in entries_right
                                            if invalid_descriptor(candidate_bytes, entry)],
            'metadata_difference_counts': {field: sum(getattr(a, field) != getattr(b, field)
                                                      for a, b in zip(entries_left, entries_right, strict=True))
                                           for field in fields},
            'local_header_difference_counts': {field: sum(a[field] != b[field]
                                                          for a, b in zip(local_left, local_right, strict=True))
                                               for field in local_fields},
            'archive_comment_equal': left.comment == right.comment,
            'archive_bytes_equal': reference_bytes == candidate_bytes,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference', type=Path)
    parser.add_argument('candidate', type=Path)
    args = parser.parse_args()
    print(json.dumps(inspect(args.reference, args.candidate), sort_keys=True))


if __name__ == '__main__':
    main()
