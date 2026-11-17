"""Compare raw deflate streams from CPython zlib and the Rust zlib-rs crate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import zipfile
import zlib


def digest(data):return hashlib.sha256(data).hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--zip',required=True)
    parser.add_argument('--rlib',required=True)
    parser.add_argument('--dependency-dir',required=True)
    args=parser.parse_args()
    source=Path(args.zip);rlib=Path(args.rlib).resolve();dependencies=Path(args.dependency_dir).resolve()
    blob=source.read_bytes();records=[]
    with tempfile.TemporaryDirectory(prefix='zlib-rs-probe-') as folder:
        binary=Path(folder)/'deflate_probe'
        subprocess.run(['rustc','--edition=2024',str(Path(__file__).with_name('deflate_zlib_rs.rs')),
                        '--extern','zlib_rs='+str(rlib),'-L','dependency='+str(dependencies),
                        '-o',str(binary)],check=True)
        with zipfile.ZipFile(source) as archive:
            for entry in archive.infolist():
                if entry.is_dir():continue
                data=archive.read(entry)
                compressor=zlib.compressobj(zlib.Z_DEFAULT_COMPRESSION,zlib.DEFLATED,-15)
                expected=compressor.compress(data)+compressor.flush()
                actual=subprocess.run([str(binary)],input=data,stdout=subprocess.PIPE,check=True).stdout
                if zlib.decompress(actual,-15)!=data:raise AssertionError('invalid Rust deflate: '+entry.filename)
                records.append({'name':entry.filename,'input_sha256':digest(data),'input_bytes':len(data),
                    'reference_bytes':len(expected),'rust_bytes':len(actual),'equal':expected==actual,
                    'reference_sha256':digest(expected),'rust_sha256':digest(actual)})
    print(json.dumps({'source_revision':os.environ.get('GITHUB_SHA'),'python':platform.python_version(),
        'zlib_runtime':zlib.ZLIB_RUNTIME_VERSION,'zlib_rs_version':'0.6.8',
        'rustc':subprocess.check_output(['rustc','--version'],text=True).strip(),
        'input_archive_sha256':digest(blob),'rlib_sha256':digest(rlib.read_bytes()),
        'config':'raw deflate, default compression, window bits -15; input without flush, then finish',
        'parts':records,'equal_count':sum(r['equal'] for r in records),
        'notice':'Compression self-check only; not end-to-end DOCX parity or performance.'},indent=2))


if __name__=='__main__':main()
