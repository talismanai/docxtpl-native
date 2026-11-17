"""Exact ZIP differential checks against the compiled production package writer.

The optional forced-ZIP64 mode exercises its metadata adapter without a >2 GB
allocation. Oversized payload/offset fields are also covered in Rust unit tests;
this script does not claim a multi-gigabyte end-to-end run or other platforms.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import random
import subprocess
import tempfile
import zipfile
import zlib


def check(binary, folder, name, entries, force64=False, input_level=None):
    original = folder / (name + '.input.zip')
    output = folder / (name + '.rust.zip')
    with zipfile.ZipFile(original, 'w', compression=zipfile.ZIP_STORED if input_level is None else zipfile.ZIP_DEFLATED,
                         compresslevel=input_level) as archive:
        for key, data in entries:
            archive.writestr(key, data)
    expected = io.BytesIO()
    with zipfile.ZipFile(expected, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for key, data in entries:
            if key.endswith('/') and not force64:
                continue  # OPC save omits directory records.
            info = zipfile.ZipInfo(key, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info._compresslevel = 6
            with archive.open(info, 'w', force_zip64=force64) as stream:
                stream.write(data)
    subprocess.run([str(binary), str(original), str(output)] + (['force64'] if force64 else []), check=True)
    reference, native = expected.getvalue(), output.read_bytes()
    return {'case': name, 'input_entries': len(entries), 'forced_zip64': force64, 'input_compression_level': input_level,
            'equal': reference == native, 'reference_bytes': len(reference), 'native_bytes': len(native),
            'reference_sha256': hashlib.sha256(reference).hexdigest(),
            'native_sha256': hashlib.sha256(native).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--official', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    binary = Path(args.binary).resolve()
    with tempfile.TemporaryDirectory(prefix='zip-writer-contract-') as directory:
        folder = Path(directory)
        with zipfile.ZipFile(args.official) as archive:
            official = [(name, archive.read(name)) for name in archive.namelist()]
        mixed = [('empty', b''), ('ascii.txt', b'hi' * 100), ('subdir/á-文.txt', b'\0' * 100000),
                 ('random', random.Random(42).randbytes(1024 * 1024)), ('directory/', b'')]
        cases = [check(binary, folder, 'empty', []), check(binary, folder, 'official', official),
                 check(binary, folder, 'mixed', mixed), check(binary, folder, 'forced_zip64', mixed, True)]
        cases.append(check(binary, folder, 'packed_level1', mixed, input_level=1))
        for count in [65535, 65536]:
            cases.append(check(binary, folder, 'entry_count_' + str(count), [(str(i), b'') for i in range(count)]))
    report = {'source_revision': os.environ.get('GITHUB_SHA'), 'python': platform.python_version(),
              'platform': platform.platform(), 'reference_zlib': zlib.ZLIB_RUNTIME_VERSION,
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cases': cases, 'passed': sum(case['equal'] for case in cases), 'total': len(cases),
              'scope': 'Complete generated ZIP bytes; force64 uses the production metadata adapter; no >2 GB end-to-end allocation.'}
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(case['equal'] for case in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
