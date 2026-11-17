"""Print the example and fixture manifest from a local pinned Git revision."""

import argparse
import hashlib
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    repo: Path = args.repo.resolve()
    revision: str = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', '--verify', f'{args.revision}^{{commit}}'], text=True,
    ).strip()
    output = subprocess.check_output(['git', '-C', str(repo), 'ls-tree', '-r', '-z', '--name-only',
                                      revision, '--', 'tests/'])
    paths = sorted(name.decode('utf-8') for name in output.split(b'\0') if name)
    examples: dict[str, str] = {}
    fixtures: dict[str, str] = {}
    for name in paths:
        relative = Path(name).relative_to('tests').as_posix()
        if relative.startswith('templates/'):
            destination = fixtures
        elif '/' not in relative and relative.endswith('.py') and relative not in ('__init__.py', 'runtests.py'):
            destination = examples
        else:
            continue
        data = subprocess.check_output(['git', '-C', str(repo), 'cat-file', 'blob', f'{revision}:{name}'])
        destination[relative] = hashlib.sha256(data).hexdigest()
    print('"""SHA-256 manifest of upstream example scripts and fixtures at REFERENCE_COMMIT.\n')
    print('Regenerate it with scripts/generate_upstream_manifest.py when the pinned reference revision changes.\n"""\n')
    print(f'MANIFEST_REFERENCE_COMMIT = {revision!r}\n')
    for index, (label, entries) in enumerate((('UPSTREAM_EXAMPLE_SHA256', examples),
                                              ('UPSTREAM_FIXTURE_SHA256', fixtures))):
        if index:
            print()
        print(f'{label}: dict[str, str] = {{')
        for name, digest in entries.items():
            print(f'    {name!r}: {digest!r},')
        print('}')


if __name__ == '__main__':
    main()
