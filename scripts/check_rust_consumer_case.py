"""Prepare and compare a synthetic DOCX for the external Rust consumer."""
from __future__ import annotations

import argparse
import base64
import io
import json
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docxtpl import DocxTemplate
from paritylab.compare import compare_docx
from paritylab.reference_fingerprint import installed_reference


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'compare'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root: Path = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.mode == 'prepare':
        if not installed_reference()['matched']:
            raise AssertionError('reference source does not match its pinned revision')
        image = base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=='
        )
        document = Document()
        document.add_paragraph('Hello {{ name }}')
        document.add_table(rows=1, cols=1).cell(0, 0).text = 'fixed table'
        document.add_picture(io.BytesIO(image))
        source = root / 'source.docx'
        document.save(str(source))
        template = DocxTemplate(str(source))
        template.render({'name': 'Rust'})
        template.save(str(root / 'reference.docx'))
        print(json.dumps({'reference_fingerprint_matched': True, 'source': str(source)}))
        return

    reference, candidate = root / 'reference.docx', root / 'candidate.docx'
    result = compare_docx(reference.read_bytes(), candidate.read_bytes())
    with ZipFile(reference) as first, ZipFile(candidate) as second:
        media = [name for name in first.namelist() if name.startswith('word/media/')]
        result['media_bytes_equal'] = bool(media) and all(first.read(name) == second.read(name) for name in media)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['equivalent'] or not result['media_bytes_equal']:
        raise SystemExit('complete DOCX or media comparison failed')


if __name__ == '__main__':
    main()
