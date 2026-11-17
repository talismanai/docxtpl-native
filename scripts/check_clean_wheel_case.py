"""Render a synthetic package in a pinned-reference or clean-wheel process."""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.metadata
import io
import json
import sys
from pathlib import Path
from typing import Any

from docx import Document


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('reference', 'candidate'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root: Path = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'source.docx'

    if args.mode == 'reference':
        from docxtpl import DocxTemplate
        from paritylab.reference_fingerprint import installed_reference

        if not installed_reference()['matched']:
            raise AssertionError('reference source does not match its pinned revision')
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=='
        )
        document = Document()
        document.add_paragraph('Café {{ name }}')
        document.add_table(rows=1, cols=2).rows[0].cells[0].text = '{{ value }}'
        document.add_picture(io.BytesIO(image))
        document.save(str(source))
    else:
        import docxtpl
        import docxtpl_native
        from docxtpl_native import DocxTemplate

        module = Path(docxtpl_native.__file__).resolve()
        extension = Path(docxtpl_native._extension().__file__).resolve()
        dependency = Path(docxtpl.__file__).resolve()
        prefix = Path(sys.prefix).resolve()
        if not all(path.is_relative_to(prefix) for path in (module, extension, dependency)):
            raise AssertionError('an import escaped the clean environment')
        capabilities: dict[str, Any] = docxtpl_native.capabilities()
        if not capabilities['native_available'] or capabilities['reference_fallback']:
            raise AssertionError('native engine unavailable or reference fallback active')

    template = DocxTemplate(str(source))
    template.render({'name': 'Ada', 'value': 'célula'})
    output = root / f'{args.mode}.docx'
    template.save(str(output))
    report: dict[str, Any] = {
        'mode': args.mode,
        'python': sys.version.split()[0],
        'prefix': sys.prefix,
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    if args.mode == 'candidate':
        report.update(
            module=str(module), extension=str(extension), dependency_docxtpl=str(dependency),
            versions={name: importlib.metadata.version(name) for name in
                      ('docxtpl-native', 'docxtpl', 'Jinja2', 'python-docx', 'lxml', 'MarkupSafe')},
            native_available=capabilities['native_available'],
            reference_fallback=capabilities['reference_fallback'],
        )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
