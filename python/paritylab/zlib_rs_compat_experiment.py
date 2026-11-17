"""Isolated experiment for byte-compatible default-level Rust deflate.

Copies the pinned crate into a temporary directory; never modifies the vendored
or Cargo registry source. This does not change the library's compressor.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
import zlib


def sha(data):return hashlib.sha256(data).hexdigest()


def replace(path, old, new):
    text=path.read_text()
    if text.count(old)!=1:raise ValueError('unexpected pinned source: '+str(path))
    path.write_text(text.replace(old,new))


def patch(source):
    main=source/'src/deflate.rs';slow=source/'src/deflate/algorithm/slow.rs';config=source/'src/deflate/algorithm/mod.rs'
    replace(main,'state.hash_calc_variant = HashCalcVariant::for_max_chain_length(state.max_chain_length);',
        'state.hash_calc_variant = if level == 6 { HashCalcVariant::Roll } else { HashCalcVariant::for_max_chain_length(state.max_chain_length) };')
    replace(main,'if state.max_chain_length > 1024 {','if matches!(state.hash_calc_variant, HashCalcVariant::Roll) {')
    replace(config,'Config::new(8, 16, 128, 128, medium::deflate_medium), // 6',
        'Config::new(8, 16, 128, 128, slow::deflate_slow), // 6')
    replace(slow,'hash_head = if state.lookahead >= WANT_MIN_MATCH {',
        'hash_head = if state.lookahead >= if state.level == 6 { STD_MIN_MATCH } else { WANT_MIN_MATCH } {')
    replace(slow,'if match_len <= 5 && (state.strategy == Strategy::Filtered) {',
        'if match_len <= 5 && (state.strategy == Strategy::Filtered || (state.level == 6 && match_len == 3 && state.strstart.saturating_sub(state.match_start as usize) > 4096)) {')


def compile_crate(source, binary, library):
    subprocess.run(['rustc','--crate-name','zlib_rs','--edition=2021','--crate-type','rlib',
        '--cfg','feature="std"','--cfg','feature="rust-allocator"','-C','opt-level=2',
        str(source/'src/lib.rs'),'-o',str(library)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['rustc','--edition=2024',str(Path(__file__).with_name('deflate_zlib_rs.rs')),
        '--extern','zlib_rs='+str(library),'-L','dependency='+str(library.parent),'-o',str(binary)],check=True)


def rust(binary, data, level=-1, chunk=None):
    command=[str(binary),str(level)]
    if chunk is not None:command.append(str(chunk))
    return subprocess.run(command,input=data,stdout=subprocess.PIPE,check=True).stdout


def reference(data, chunk=None):
    compressor=zlib.compressobj(-1,zlib.DEFLATED,-15)
    size=chunk or max(1,len(data));output=[]
    for start in range(0,len(data),size):output.append(compressor.compress(data[start:start+size]))
    output.append(compressor.flush());return b''.join(output)


def sample(rng, size, style):
    if style==0:return rng.randbytes(size)
    if style==1:return bytes([rng.randrange(256)])*size
    if style==2:
        block=rng.randbytes(rng.randrange(2,65));return (block*((size+len(block)-1)//len(block)))[:size]
    if style==3:return bytes(rng.randrange(8) for _ in range(size))
    if style==4:
        block=b'<w:p><w:t>text &amp; more</w:t></w:p>';return (block*((size+len(block)-1)//len(block)))[:size]
    block=rng.randbytes(8192);data=bytearray((block*((size+8191)//8192))[:size])
    for index in range(0,size,997):data[index]=rng.randrange(256)
    return bytes(data)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--crate-dir',required=True);parser.add_argument('--zip',required=True)
    parser.add_argument('--samples',type=int,default=600);args=parser.parse_args()
    if args.samples<1:parser.error('samples must be positive')
    source=Path(args.crate_dir).resolve();archive_path=Path(args.zip)
    version=tomllib.loads((source/'Cargo.toml').read_text())['package']['version']
    if version!='0.6.8':raise ValueError('expected zlib-rs 0.6.8')
    paths=['src/deflate.rs','src/deflate/algorithm/slow.rs','src/deflate/algorithm/mod.rs']
    before={name:sha((source/name).read_bytes()) for name in paths}
    with tempfile.TemporaryDirectory(prefix='zlib-rs-compat-') as folder:
        root=Path(folder);modified=root/'modified';shutil.copytree(source,modified);patch(modified)
        after={name:sha((modified/name).read_bytes()) for name in paths}
        candidate=root/'candidate';original=root/'original';candidate_lib=root/'libcandidate.rlib';original_lib=root/'liboriginal.rlib'
        compile_crate(modified,candidate,candidate_lib);compile_crate(source,original,original_lib)
        cases=[]
        def check(name,data,chunk=None):
            expected=reference(data,chunk);actual=rust(candidate,data,-1,chunk)
            if zlib.decompress(actual,-15)!=data:raise ValueError('invalid deflate: '+name)
            cases.append({'case':name,'size':len(data),'chunk':chunk,'input_sha256':sha(data),
                'reference_sha256':sha(expected),'candidate_sha256':sha(actual),'equal':expected==actual})
        with zipfile.ZipFile(archive_path) as archive:
            for item in archive.infolist():
                if not item.is_dir():check('official/'+item.filename,archive.read(item))
        rng=random.Random(0xdbcafe)
        for index in range(args.samples):
            size=rng.choice([rng.randrange(0,8192),rng.randrange(8192,150000),rng.randrange(150000,400000)])
            check('synthetic/'+str(index),sample(rng,size,index%6))
        for index in range(48):
            size=rng.choice([0,1,255,4096,32768,65537,131073]);chunk=rng.choice([1,7,1024,16384])
            check('chunked/'+str(index),sample(rng,size,index%6),chunk)
        other=[]
        for level in range(10):
            if level==6:continue
            for size in (0,1,256,4096,65537,131073):
                for style in (0,2):
                    data=sample(rng,size,style);a=rust(original,data,level);b=rust(candidate,data,level)
                    other.append({'level':level,'size':size,'style':style,'input_sha256':sha(data),'equal':a==b})
        result={'source_revision':os.environ.get('GITHUB_SHA'),'crate_version':version,
            'original_sources':before,'patched_sources':after,'reference_zlib':zlib.ZLIB_RUNTIME_VERSION,
            'rustc':subprocess.check_output(['rustc','--version'],text=True).strip(),
            'input_archive_sha256':sha(archive_path.read_bytes()),'random_seed':'0xdbcafe',
            'candidate_rlib_sha256':sha(candidate_lib.read_bytes()),'cases':cases,'other_levels':other,
            'counts':{'cases':len(cases),'equal':sum(case['equal'] for case in cases),
                      'other_levels':len(other),'other_levels_equal':sum(case['equal'] for case in other)},
            'notice':'Isolated Rust compressor experiment; not integrated DOCX parity or a performance benchmark.'}
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
