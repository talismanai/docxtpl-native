"""Compare native rich text and images against the fingerprinted reference."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

import docxtpl
from docx import Document
from paritylab.reference_fingerprint import fingerprint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('reference', 'candidate'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    root: Path = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / 'source.docx'
    reference = fingerprint(Path(docxtpl.__file__).resolve().parent)
    if args.mode == 'reference':
        if not reference['matched']:
            raise AssertionError('reference source does not match its pinned revision')
        engine: Any = docxtpl
        document = Document()
        document.add_paragraph('{{r styled}}')
        document.add_paragraph('{{ picture }}')
        document.save(str(source))
    else:
        import docxtpl_native

        engine = docxtpl_native
        prefix = Path(sys.prefix).resolve()
        paths = (Path(docxtpl.__file__).resolve(), Path(engine.__file__).resolve(),
                 Path(engine._extension().__file__).resolve())
        if not all(path.is_relative_to(prefix) for path in paths):
            raise AssertionError('an import escaped the clean environment')
        capabilities = engine.capabilities()
        if not capabilities['native_available'] or capabilities['reference_fallback']:
            raise AssertionError('native engine unavailable or reference fallback active')

    methods = (
        (docxtpl.RichText, ('__init__', 'add')),
        (docxtpl.InlineImage, ('__init__', '_insert_image', '_set_doc_properties', '_add_hyperlink')),
    )
    monitored: dict[CodeType, str] = {}
    for cls, names in methods:
        for name in names:
            method = getattr(cls, name, None)
            code = getattr(method, '__code__', None)
            if isinstance(code, CodeType):
                monitored[code] = f'{cls.__name__}.{name}'
    calls: list[str] = []

    def profile(frame: FrameType, event: str, argument: object) -> None:
        if event == 'call' and frame.f_code in monitored:
            calls.append(monitored[frame.f_code])

    image = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPj/HwADAgH/5ncLrgAAAABJRU5ErkJggg=='
    )
    previous = sys.getprofile()
    try:
        sys.setprofile(profile)
        template = engine.DocxTemplate(str(source))
        styled = engine.RichText('font + color', font='Arial', color='003399')
        picture = engine.InlineImage(template, io.BytesIO(image), title='Résumé', descr='Naïve description')
        template.render({'styled': styled, 'picture': picture})
        output = root / f'{args.mode}.docx'
        template.save(str(output))
    finally:
        sys.setprofile(previous)

    if args.mode == 'reference':
        expected = {'RichText.__init__', 'RichText.add', 'InlineImage.__init__',
                    'InlineImage._insert_image', 'InlineImage._set_doc_properties'}
        if not expected.issubset(calls):
            raise AssertionError(f'reference profiling control did not run: {calls}')
    elif calls:
        raise AssertionError(f'monitored Python algorithms ran in candidate: {calls}')
    report = {
        'mode': args.mode,
        'python': sys.version.split()[0],
        'reference_fingerprint_matched': reference['matched'],
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'output_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'styled_xml': styled.xml,
        'monitored_python_calls': calls,
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
