"""Build an isolated, incomplete ZIP compatibility prototype for experiments.

The output is not the project library. It deliberately rejects ZIP64 and
directory entries and assumes a Unix ZIP creator. Never use it for production.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib
import zipfile

from zlib_rs_compat_experiment import patch


def sha(data):return hashlib.sha256(data).hexdigest()


def replace(path, old, new):
    source=path.read_text()
    if source.count(old)!=1:raise ValueError('unexpected prototype base: '+str(path))
    path.write_text(source.replace(old,new))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True)
    parser.add_argument('--crate-dir',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--cache',action='store_true',help='apply the experimental bounded compression cache')
    args=parser.parse_args();repo=Path(__file__).resolve().parents[2]
    source=Path(args.crate_dir).resolve();out=Path(args.out).resolve()
    if tomllib.loads((source/'Cargo.toml').read_text())['package']['version']!='0.6.8':raise ValueError('expected zlib-rs 0.6.8')
    out.mkdir(parents=True,exist_ok=False)
    revision=subprocess.check_output(['git','rev-parse',args.base],cwd=repo,text=True).strip()
    # The binding lived in `src` before it moved to `bindings/python`.
    binding='bindings' if subprocess.run(['git','cat-file','-e',revision+':bindings'],cwd=repo).returncode==0 else 'src'
    archive=subprocess.check_output(['git','archive',revision,'Cargo.toml','Cargo.lock','pyproject.toml','README.md',
        'LICENSE','THIRD_PARTY.md',binding,'crates','vendor','python'],cwd=repo)
    with tarfile.open(fileobj=io.BytesIO(archive)) as data:data.extractall(out,filter='data')
    fork=out/'vendor/zlib-rs-ref';shutil.copytree(source,fork);patch(fork)
    (fork/'Cargo.toml').write_text('''[package]
name = "zlib-rs-ref"
version = "0.6.8"
edition = "2021"
rust-version = "1.75"
license = "Zlib"
[lib]
name = "zlib_rs_ref"
path = "src/lib.rs"
[features]
ZLIB_DEBUG = []
__internal-api = []
__internal-fuzz = []
__internal-fuzz-disable-checksum = []
__internal-test = []
avx512 = ["vpclmulqdq"]
c-allocator = []
default = ["std", "c-allocator"]
lsx = []
rust-allocator = []
std = ["rust-allocator"]
vpclmulqdq = []
''')
    members='"vendor/docxtplrs"]' if '"vendor/docxtplrs"]' in (out/'Cargo.toml').read_text() else '"vendor/docxtplrs",\n]'
    replace(out/'Cargo.toml',members,'"vendor/docxtplrs", "vendor/zlib-rs-ref"]')
    replace(out/'crates/docxtpl-core/Cargo.toml','[dependencies]\n',
        '[dependencies]\nzlib-rs-ref = { path = "../../vendor/zlib-rs-ref", default-features = false, features = ["std"] }\n')
    replace(out/'crates/docxtpl-core/src/lib.rs','//! Rust DOCX templating core, shared by language bindings.\n',
        '//! Rust DOCX templating core, shared by language bindings.\nmod zip_ref_proto;\n')
    replace(out/'crates/docxtpl-core/src/opc.rs','    output.to_bytes()\n}',
        '    crate::zip_ref_proto::encode(&output)\n}')
    prototype=Path(__file__).with_name('zip_ref_prototype.rs')
    shutil.copy2(prototype,out/'crates/docxtpl-core/src/zip_ref_proto.rs')
    cache_patch=Path(__file__).with_name('zip_ref_cache_prototype.patch')
    if args.cache:
        subprocess.run(['patch','--batch','-p0'],cwd=out/'crates/docxtpl-core/src',
                       input=cache_patch.read_text(),text=True,check=True)
    wheels=out/'wheels';wheels.mkdir()
    subprocess.run(['maturin','build','--release','--offline','--auditwheel','skip','-o',str(wheels)],cwd=out,check=True)
    subprocess.run(['cargo','metadata','--locked','--offline','--format-version','1'],cwd=out,check=True,stdout=subprocess.DEVNULL)
    wheel=next(wheels.glob('*.whl'))
    with zipfile.ZipFile(wheel) as built:
        names=[name for name in built.namelist() if name.startswith('docxtpl_native/_native') and name.endswith('.so')]
        if len(names)!=1:raise ValueError('expected one native extension')
        extension=out/'python'/names[0];extension.write_bytes(built.read(names[0]))
    result={'source_revision':os.environ.get('GITHUB_SHA'),'base_revision':revision,
        'zlib_rs_version':'0.6.8','prototype_source_sha256':sha(prototype.read_bytes()),
        'cache_patch_sha256':sha(cache_patch.read_bytes()) if args.cache else None,
        'compiled_writer_sha256':sha((out/'crates/docxtpl-core/src/zip_ref_proto.rs').read_bytes()),
        'patched_zlib_sources':{name:sha((fork/name).read_bytes()) for name in
            ('src/deflate.rs','src/deflate/algorithm/slow.rs','src/deflate/algorithm/mod.rs')},
        'wheel_sha256':sha(wheel.read_bytes()),'extension_sha256':sha(extension.read_bytes()),
        'cargo_lock_sha256':sha((out/'Cargo.lock').read_bytes()),
        'limitations':['ZIP64 unsupported','directory entries unsupported','Unix ZIP creator hardcoded',
                       'does not integrate with every postprocessing path'],
        'notice':'Laboratory build only; not a production library or a compatibility qualification.'}
    (out/'prototype.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'prototype':str(out),'manifest':str(out/'prototype.json')}))


if __name__=='__main__':main()
