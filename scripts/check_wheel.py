"""Check the wheel payload, rather than relying on editable-install behavior."""
from pathlib import Path
import re
import sys
import zipfile


def reference_requirements(metadata: str) -> list[str]:
    """Requires-Dist entries that would install docxtpl, under any extra."""
    found = []
    for line in metadata.splitlines():
        if line.startswith('Requires-Dist:'):
            requirement = line.split(':', 1)[1].strip()
            name = re.match(r'[A-Za-z0-9._-]+', requirement)
            if name and re.sub(r'[-_.]+', '-', name.group(0)).lower() == 'docxtpl':
                found.append(requirement)
    return found


def main():
    paths=list(Path(sys.argv[1] if len(sys.argv)>1 else 'dist').glob('*.whl'))
    if not paths:raise SystemExit('no wheels produced')
    for path in paths:
        with zipfile.ZipFile(path) as wheel:
            names=wheel.namelist()
            for package_file in ['docxtpl_native/__init__.py','docxtpl_native/bridge.py','docxtpl_native/inline_image.py','docxtpl_native/__main__.py','paritylab/__init__.py','paritylab/cli.py','paritylab/worker.py','paritylab/reference_fingerprint.py','paritylab/protocols.py','paritylab/protocol_base.py','paritylab/opc_probes.py','paritylab/inventory.py']:
                if package_file not in names:raise SystemExit(f'{path}: missing {package_file}')
            if not any(n.startswith('docxtpl_native/_native') and n.endswith(('.so','.pyd')) for n in names):
                raise SystemExit(f'{path}: native extension missing')
            for notice in ['vendor/notices/python-docx-LICENSE','vendor/notices/UNICODE-LICENSE','vendor/notices/corosensei-LICENSE-MIT','vendor/notices/scoped-tls-LICENSE-MIT','vendor/notices/scoped-tls-hkt-LICENSE-MIT','vendor/notices/CPython-LICENSE','vendor/notices/Jinja2-LICENSE','vendor/notices/MarkupSafe-LICENSE','vendor/minijinja/LICENSE','vendor/docxtplrs/LICENSE','vendor/zlib-rs/LICENSE']:
                if notice not in names or wheel.read(notice)!=Path(notice).read_bytes():
                    raise SystemExit(f'{path}: missing or modified attribution notice {notice}')
            if 'paritylab/iteration_probes.py' not in names:
                raise SystemExit(f'{path}: iteration protocol module missing')
            if 'paritylab/macro_probes.py' not in names:
                raise SystemExit(f'{path}: macro protocol module missing')
            metadata=[n for n in names if n.endswith('.dist-info/METADATA')]
            if len(metadata)!=1:
                raise SystemExit(f'{path}: expected one METADATA file')
            requirements=reference_requirements(wheel.read(metadata[0]).decode())
            if requirements:
                raise SystemExit(f'{path}: the package must not require docxtpl: {requirements}')
            entries=[n for n in names if n.endswith('.dist-info/entry_points.txt')]
            if not entries or 'docxtpl-parity' not in wheel.read(entries[0]).decode():
                raise SystemExit(f'{path}: CLI entry point missing')
        print(f'Validated {path}')

if __name__=='__main__':main()

