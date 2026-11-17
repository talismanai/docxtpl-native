"""Check repeated and changed experimental ZIP inputs against Python zlib."""
import argparse
from concurrent.futures import ThreadPoolExecutor
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


def make_source(seed, side):
    pixels = random.Random(seed).randbytes(side * side * 3)
    image = io.BytesIO()
    Image.frombytes('RGB', (side, side), pixels).save(image, format='PNG')
    image.seek(0)
    document = Document()
    document.add_paragraph('Hello {{ value }}')
    document.add_picture(image)
    source = io.BytesIO()
    document.save(source)
    return source.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--rounds', type=int, default=1)
    parser.add_argument('--eviction', action='store_true', help='use four larger distinct media payloads')
    args = parser.parse_args()
    if args.workers < 1 or args.rounds < 1:
        parser.error('workers and rounds must be positive')
    side = 1536 if args.eviction else 256
    sources = [make_source(seed, side) for seed in (range(51, 55) if args.eviction else (51, 52))]
    runs = ([(0, 'A'), (1, 'A'), (2, 'A'), (3, 'A'), (0, 'A')] if args.eviction
            else [(0, 'A'), (0, 'A'), (0, 'B'), (1, 'A'), (0, 'A')]) * args.rounds
    media_bytes = 0
    media_cache_bytes = 0
    for source in sources:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            for name in archive.namelist():
                if name.startswith('word/media/'):
                    media = archive.read(name)
                    media_bytes += len(media)
                    if args.eviction:
                        compressor = zlib.compressobj(-1, zlib.DEFLATED, -15)
                        media_cache_bytes += len(media) + len(compressor.compress(media) + compressor.flush())
    if args.eviction and media_cache_bytes <= 32 * 1024 * 1024:
        raise AssertionError('insufficient input to pressure the 32 MiB cache')
    def verify(run):
        source, value = run
        parts = 0
        template = docxtpl_native.DocxTemplate(io.BytesIO(sources[source]))
        template.render({'value': value})
        output = io.BytesIO()
        template.save(output)
        data = output.getvalue()
        assert Document(io.BytesIO(data)).paragraphs[0].text == 'Hello ' + value
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if entry.compress_type != zipfile.ZIP_DEFLATED:
                    raise AssertionError('unexpected ZIP method: ' + entry.filename)
                if data[entry.header_offset:entry.header_offset + 4] != b'PK\x03\x04':
                    raise AssertionError('invalid local header: ' + entry.filename)
                name_length, extra_length = struct.unpack_from('<HH', data, entry.header_offset + 26)
                start = entry.header_offset + 30 + name_length + extra_length
                actual = data[start:start + entry.compress_size]
                compressor = zlib.compressobj(-1, zlib.DEFLATED, -15)
                expected = compressor.compress(archive.read(entry)) + compressor.flush()
                if actual != expected:
                    raise AssertionError('deflate mismatch: ' + entry.filename)
                parts += 1
        return parts
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        parts = sum(pool.map(verify, runs))
    extension = docxtpl_native._extension().__file__
    with open(extension, 'rb') as binary:
        binary_hash = hashlib.sha256(binary.read()).hexdigest()
    print(json.dumps({'runs': len(runs), 'workers': args.workers, 'media_input_bytes': media_bytes, 'media_cache_bytes': media_cache_bytes, 'parts_checked': parts, 'zlib_version': zlib.ZLIB_RUNTIME_VERSION,
                      'extension': extension, 'extension_sha256': binary_hash,
                      'scope': 'Repeated and changed generated DOCX inputs; experimental candidate only.'}, indent=2))


if __name__ == '__main__':
    main()
