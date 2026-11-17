"""Saving an exposed python-docx model builds the package natively, as python-docx writes it."""

from __future__ import annotations

import io
import tempfile
import time
import unittest
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import docx.opc.part as opc_part
import docxtpl_native
from docx import Document
from docx.opc import pkgwriter
from docx.shared import Inches
from PIL import Image


def image() -> io.BytesIO:
    stream = io.BytesIO()
    Image.new('RGB', (4, 4), 'red').save(stream, 'PNG')
    stream.seek(0)
    return stream


def source(kind: str) -> bytes:
    document = Document()
    document.add_paragraph('Hello {{ name }}')
    if kind == 'rich':
        document.add_picture(image(), width=Inches(1))
        document.sections[0].header.paragraphs[0].text = 'Head {{ name }}'
        document.add_paragraph('item', style='List Bullet')
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


@contextmanager
def fixed_clock() -> Generator[None, None, None]:
    with patch('zipfile.time.localtime', lambda *args: time.struct_time((2024, 1, 2, 3, 4, 6, 0, 2, 0))):
        yield


@contextmanager
def python_docx_save() -> Generator[None, None, None]:
    with patch('docxtpl_native._save_guard.save_chain_is_standard', lambda document: False):
        yield


def save(data: bytes, target: Any, prepare: Callable[[Any], None] = lambda template: None) -> None:
    template = docxtpl_native.DocxTemplate(io.BytesIO(data))
    template.get_docx()
    prepare(template)
    template.render({'name': 'Ada'})
    template.save(target)


class NativeExposedSaveTests(unittest.TestCase):
    def test_archive_bytes_match_python_docx(self) -> None:
        def hyperlink(template: Any) -> None:
            template.build_url_id('https://example.com/?a=1&b="2"<3>')

        for kind in ('plain', 'rich'):
            for name, prepare in (('none', lambda template: None), ('hyperlink', hyperlink)):
                with self.subTest(kind=kind, prepare=name):
                    native, reference = io.BytesIO(), io.BytesIO()
                    with fixed_clock():
                        save(source(kind), native, prepare)
                        with python_docx_save():
                            save(source(kind), reference, prepare)
                    self.assertEqual(native.getvalue(), reference.getvalue())

    def test_path_target_matches_python_docx(self) -> None:
        with tempfile.TemporaryDirectory() as directory, fixed_clock():
            native, reference = Path(directory, 'native.docx'), Path(directory, 'reference.docx')
            save(source('rich'), native)
            with python_docx_save():
                save(source('rich'), str(reference))
            self.assertEqual(native.read_bytes(), reference.read_bytes())

    def test_part_hooks_run_in_python_docx_order(self) -> None:
        def observe(native: bool) -> list[str]:
            events: list[str] = []
            original_marshal, original_blob = opc_part.Part.before_marshal, opc_part.XmlPart.blob

            def before_marshal(part: Any) -> None:
                events.append('marshal:' + part.partname)
                original_marshal(part)

            def blob(part: Any) -> bytes:
                events.append('blob:' + part.partname)
                return original_blob.fget(part)

            data = source('rich')
            with (patch.object(opc_part.Part, 'before_marshal', before_marshal),
                  patch.object(opc_part.XmlPart, 'blob', property(blob))):
                if native:
                    save(data, io.BytesIO())
                else:
                    with python_docx_save():
                        save(data, io.BytesIO())
            return events

        expected = observe(False)
        self.assertTrue(expected)
        self.assertEqual(observe(True), expected)

    def test_replaced_writer_saves_with_python_docx(self) -> None:
        calls: list[str] = []
        original = pkgwriter.PackageWriter.write

        def write(pkg_file: Any, pkg_rels: Any, parts: Any) -> None:
            calls.append('write')
            original(pkg_file, pkg_rels, parts)

        data = source('plain')
        with patch.object(pkgwriter.PackageWriter, 'write', staticmethod(write)):
            target = io.BytesIO()
            save(data, target)
        self.assertEqual(calls, ['write'])
        self.assertTrue(zipfile.is_zipfile(target))

    def test_python_docx_writer_does_not_run(self) -> None:
        from native_audit import CodeCalls

        codes = (pkgwriter.PackageWriter.write, pkgwriter.PackageWriter._write_parts,
                 pkgwriter._ContentTypesItem.from_parts, pkgwriter._ContentTypesItem._add_content_type,
                 zipfile.ZipFile.writestr)
        data = source('rich')
        with CodeCalls(*codes) as calls:
            save(data, io.BytesIO())
        self.assertEqual(calls.calls, [])


if __name__ == '__main__':
    unittest.main()
