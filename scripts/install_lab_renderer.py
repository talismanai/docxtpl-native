"""Install the pinned Debian laboratory renderer under a new local directory.

Requires Linux x86_64, dpkg-deb and the Debian 13 system libraries. No system
packages or fonts are changed. Optional --archives reuses verified downloads.
The renderer needs local IPC sockets, including during --version verification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).resolve().parents[1] / 'compatibility/renderer-debian13.json')
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--archives', type=Path)
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        parser.error('the pinned packages target Linux x86_64')
    manifest = json.loads(args.manifest.read_text()); root = args.prefix.resolve()
    root.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='renderer-download-') as temporary:
        for package in manifest['packages']:
            name = package['file']
            if Path(name).name != name: raise ValueError('invalid package filename')
            archive = (args.archives if args.archives else Path(temporary)) / name
            if not args.archives:
                with urllib.request.urlopen(package['url'], timeout=60) as source, archive.open('wb') as output:
                    shutil.copyfileobj(source, output)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != package['sha256']:
                raise ValueError('package hash mismatch: ' + name)
            subprocess.run(['dpkg-deb', '--extract', str(archive), str(root)], check=True)
    relocations = []
    for path in root.rglob('*'):
        if path.is_symlink() and (target := os.readlink(path)).startswith('/'):
            replacement = os.path.relpath(root / target[1:], path.parent)
            path.unlink(); path.symlink_to(replacement)
            relocations.append({'path': str(path.relative_to(root)), 'before': target, 'after': replacement})
    program = root / 'usr/lib/libreoffice/program'
    fundamental = program / 'fundamentalrc'
    fundamental.write_text(fundamental.read_text().replace('BRAND_BASE_DIR=file:///usr/lib/libreoffice', 'BRAND_BASE_DIR=${ORIGIN}/..'))
    sofficerc = program / 'sofficerc'
    sofficerc.write_text(sofficerc.read_text().replace('FHS_CONFIG_FILE=file:///etc/libreoffice/sofficerc',
                          'FHS_CONFIG_FILE=' + (root / 'etc/libreoffice/sofficerc').as_uri()))
    # The package's post-install hook normally supplies this default via ucf.
    shutil.copyfile(root / 'usr/lib/libreoffice/share/.registry/main.xcd', root / 'etc/libreoffice/registry/main.xcd')
    data = root / 'usr/lib/libreoffice/share/liblangtag'
    data.symlink_to(os.path.relpath(root / 'usr/share/liblangtag', data.parent))
    binary_dir = root / 'bin'; binary_dir.mkdir()
    wrapper = binary_dir / 'soffice'
    # A Python executable wrapper avoids shell interpolation of installation paths.
    wrapper.write_text('#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n'
        'root=Path(__file__).resolve().parents[1]\nprogram=root/"usr/lib/libreoffice/program"\n'
        'os.environ["LD_LIBRARY_PATH"]=str(program)+":"+str(root/"usr/lib/x86_64-linux-gnu")\n'
        'os.execv(str(program/"soffice"),[str(program/"soffice"),*__import__("sys").argv[1:]])\n')
    wrapper.chmod(0o755)
    with tempfile.TemporaryDirectory(prefix='renderer-verify-') as profile:
        result = subprocess.run([str(wrapper), '-env:UserInstallation=' + Path(profile).as_uri(), '--headless', '--version'],
                                check=True, capture_output=True, text=True)
    if result.stdout.strip() != manifest['renderer_version']:
        raise RuntimeError('unexpected renderer version: ' + result.stdout)
    record = {'manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
              'version': result.stdout.strip(), 'relocations': relocations,
              'files': {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in [program/'soffice.bin', program/'libmergedlo.so', fundamental, sofficerc, wrapper]}}
    (root/'installation.json').write_text(json.dumps(record, indent=2)+'\n')
    print(json.dumps({'version': record['version'], 'path_entry': str(binary_dir)}))


if __name__ == '__main__': main()
