"""Linux laboratory: actual ZIP64 payload/offset checks, bounded fixture disk use.

The payload uses the production Package writer. The offset driver exercises
the same ZipWriter and metadata adapter with a sparse prefix. Child peak RSS
is measured by wait4; elapsed time under arbitrary host load is not a benchmark.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import zipfile
import zlib


def run(command):
    with subprocess.Popen([str(item) for item in command]) as process:
        _, status, usage = os.wait4(process.pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
    return {'peak_rss_kib': usage.ru_maxrss}


def compare(reference, native):
    hashes = [hashlib.sha256(), hashlib.sha256()]; equal = True
    with reference.open('rb') as first, native.open('rb') as second:
        while True:
            a, b = first.read(1024 * 1024), second.read(1024 * 1024)
            if not a and not b: break
            equal &= a == b
            hashes[0].update(a); hashes[1].update(b)
    with zipfile.ZipFile(native) as archive:
        valid = archive.testzip() is None
    return {'equal': equal, 'crc_verified': valid, 'archive_bytes': native.stat().st_size,
            'reference_sha256': hashes[0].hexdigest(), 'native_sha256': hashes[1].hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--baseline-binary')
    parser.add_argument('--folder', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    if platform.system() != 'Linux': raise SystemExit('This RSS/ZIP64 laboratory requires Linux.')
    binary = Path(args.binary).resolve(); root = Path(args.folder); root.mkdir(parents=True, exist_ok=True)
    size = (1 << 31) + 1; original = root / 'payload.python.zip'; actual = root / 'payload.rust.zip'
    info = zipfile.ZipInfo('word/media/large.png', (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED; info._compresslevel = 6
    info.external_attr = 0o600 << 16; info.file_size = size
    block = b'\0' * (1024 * 1024)
    with zipfile.ZipFile(original, 'w') as archive:
        with archive.open(info, 'w') as stream:
            remaining = size
            while remaining:
                count = min(remaining, len(block)); stream.write(block[:count]); remaining -= count
    usage = run([binary, original, actual])
    payload = {'case': 'payload_over_2gib', 'input_uncompressed_bytes': size, **usage, **compare(original, actual)}
    with zipfile.ZipFile(actual) as archive:
        assert archive.infolist()[0].file_size == size
    if args.baseline_binary:
        baseline = Path(args.baseline_binary).resolve(); previous = root / 'payload.baseline.zip'
        payload['baseline'] = {**run([baseline, original, previous]), **compare(original, previous),
                               'binary_sha256': hashlib.sha256(baseline.read_bytes()).hexdigest()}
    offset = (1 << 31) + 127; expected = root / 'offset.python.zip'; actual = root / 'offset.rust.zip'
    with expected.open('wb') as target:
        target.seek(offset)
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            info = zipfile.ZipInfo('a', (1980, 1, 1, 0, 0, 0)); info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16; info._compresslevel = 6
            archive.writestr(info, b'a')
    usage = run([binary, '--offset', actual])
    offset_case = {'case': 'offset_over_2gib', 'header_offset': offset, **usage, **compare(expected, actual)}
    with zipfile.ZipFile(actual) as archive:
        assert archive.infolist()[0].header_offset == offset
    cases = [payload, offset_case]
    report = {'source_revision': os.environ.get('GITHUB_SHA'), 'python': platform.python_version(),
              'platform': platform.platform(), 'reference_zlib': zlib.ZLIB_RUNTIME_VERSION,
              'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'cases': cases,
              'scope': 'Actual >2 GiB payload through production Package writer; real ZIP64 offsets through writer/metadata adapter; whole bytes and CRC. Peak child RSS, no latency claim.'}
    Path(args.out).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))
    return 0 if all(case['equal'] and case['crc_verified'] for case in cases) else 1


if __name__ == '__main__':
    raise SystemExit(main())
