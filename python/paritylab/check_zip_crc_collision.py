"""Check ZIP payload identity when distinct media share length and CRC32."""
import hashlib
import io
import json
import random
import struct
import zlib
import zipfile

from docx import Document
from PIL import Image
import docxtpl_native


def colliding_suffixes():
    rng = random.Random(816)
    seen = {}
    for index in range(350000):
        value = rng.randbytes(32)
        crc = zlib.crc32(value)
        if crc in seen and seen[crc] != value:
            return seen[crc], value, index + 1
        seen[crc] = value
    raise AssertionError('collision not found')


def main():
    a, b, attempts = colliding_suffixes()
    image = io.BytesIO()
    Image.new('RGB', (2, 2), (1, 2, 3)).save(image, format='PNG')
    images = [image.getvalue() + suffix for suffix in (a, b)]
    if images[0] == images[1] or len(images[0]) != len(images[1]) or zlib.crc32(images[0]) != zlib.crc32(images[1]):
        raise AssertionError('collision invalid')
    for data in images:
        Image.open(io.BytesIO(data)).load()
    document = Document()
    document.add_paragraph('Hello {{ value }}')
    document.add_picture(io.BytesIO(image.getvalue()))
    initial = io.BytesIO()
    document.save(initial)
    sources = []
    for data in images:
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(initial.getvalue())) as source, zipfile.ZipFile(output, 'w') as target:
            for entry in source.infolist():
                content = data if entry.filename.startswith('word/media/') else source.read(entry)
                target.writestr(entry, content)
        sources.append(output.getvalue())
    for index in (0, 1, 0, 1):
        template = docxtpl_native.DocxTemplate(io.BytesIO(sources[index]))
        template.render({'value': 'test'})
        output = io.BytesIO()
        template.save(output)
        payload = output.getvalue()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            media = [entry for entry in archive.infolist() if entry.filename.startswith('word/media/')]
            if len(media) != 1 or archive.read(media[0]) != images[index]:
                raise AssertionError('cached media mismatch')
            entry = media[0]
            if entry.compress_type != zipfile.ZIP_DEFLATED:
                raise AssertionError('unexpected ZIP compression')
            name, extra = struct.unpack_from('<HH', payload, entry.header_offset + 26)
            start = entry.header_offset + 30 + name + extra
            compressor = zlib.compressobj(-1, zlib.DEFLATED, -15)
            expected = compressor.compress(images[index]) + compressor.flush()
            if payload[start:start + entry.compress_size] != expected:
                raise AssertionError('compressed stream mismatch')
    extension = docxtpl_native._extension().__file__
    with open(extension, 'rb') as binary:
        binary_hash = hashlib.sha256(binary.read()).hexdigest()
    print(json.dumps({'runs': 4, 'attempts': attempts, 'crc32': zlib.crc32(images[0]),
                      'payload_bytes': len(images[0]), 'extension': extension,
                      'extension_sha256': binary_hash,
                      'scope': 'Two valid PNG payloads of equal CRC32 and length, alternating renders.'}, indent=2))


if __name__ == '__main__':
    main()
