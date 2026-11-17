"""Compare raw compression cost; this does not benchmark DOCX generation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import statistics
import subprocess
import tempfile
import zipfile

from zlib_rs_compat_experiment import patch


def main():
    parser=argparse.ArgumentParser()
    for arg in ('crate-dir','zip','flate2-rlib','dependency-dir','native-lib-dir'):parser.add_argument('--'+arg,required=True)
    args=parser.parse_args();source=Path(args.crate_dir).resolve()
    rlib=Path(args.flate2_rlib).resolve();deps=Path(args.dependency_dir).resolve();native=Path(args.native_lib_dir).resolve()
    with zipfile.ZipFile(args.zip) as archive:styles=archive.read('word/styles.xml')
    rng=random.Random(931);inputs={
        'small_xml':(b'<w:p><w:t>Hello</w:t></w:p>'*400,200),
        'official_styles':(styles,30),
        'random_1mib':(rng.randbytes(1024*1024),5),
    }
    with tempfile.TemporaryDirectory(prefix='deflate-bench-') as folder:
        root=Path(folder);modified=root/'modified';shutil.copytree(source,modified);patch(modified)
        library=root/'libcompat.rlib';binary=root/'bench'
        subprocess.run(['rustc','--crate-name','zlib_rs','--edition=2021','--crate-type','rlib',
            '--cfg','feature="std"','--cfg','feature="rust-allocator"','-C','opt-level=3',
            str(modified/'src/lib.rs'),'-o',str(library)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        subprocess.run(['rustc','--edition=2024','-C','opt-level=3',str(Path(__file__).with_name('deflate_compat_bench.rs')),
            '--extern','zlib_rs='+str(library),'--extern','flate2='+str(rlib),'-L','dependency='+str(deps),
            '-L','native='+str(native),'-o',str(binary)],check=True)
        cases={}
        for name,(data,iterations) in inputs.items():
            result=json.loads(subprocess.run([str(binary),str(iterations)],input=data,stdout=subprocess.PIPE,check=True).stdout)
            result.update(input_bytes=len(data),input_sha256=hashlib.sha256(data).hexdigest(),iterations=iterations,
                median_current_over_compat=statistics.median(x['current_ns']/x['compat_ns'] for x in result['samples']))
            cases[name]=result
        output={'source_revision':os.environ.get('GITHUB_SHA'),'rustc':subprocess.check_output(['rustc','--version'],text=True).strip(),
            'flate2_rlib_sha256':hashlib.sha256(rlib.read_bytes()).hexdigest(),
            'compat_rlib_sha256':hashlib.sha256(library.read_bytes()).hexdigest(),'cases':cases,
            'notice':'Synthetic raw compression only; concurrent system load may affect results. No DOCX or visual comparison.'}
    print(json.dumps(output,indent=2))


if __name__=='__main__':main()
