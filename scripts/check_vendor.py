"""Verify the pinned upstream source and the exact audited local delta."""
import hashlib
import json
from pathlib import Path

for root in [Path('vendor/minijinja'),Path('vendor/docxtplrs'),Path('vendor/zlib-rs')]:
    manifest=json.loads((root/'UPSTREAM.json').read_text())
    expected={item['path']:item['git_blob_sha'] for item in manifest['files']}
    expected.update(manifest.get('local_changes',{}))
    expected.update(manifest.get('local_files',{}))
    for name,digest in expected.items():
        data=(root/name).read_bytes()
        actual=hashlib.sha1(f'blob {len(data)}\0'.encode()+data).hexdigest()
        if actual!=digest:raise SystemExit('unreviewed vendored source change: '+str(root/name))
    extras={str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()}-set(expected)-{'UPSTREAM.json','FORK.md'}
    if extras:raise SystemExit('untracked vendor source: '+', '.join(sorted(extras)))
    print(f"Verified {root.name} {manifest['version']} at {manifest['commit']}: {len(expected)} files, {len(manifest.get('local_changes',{}))} audited changes")
