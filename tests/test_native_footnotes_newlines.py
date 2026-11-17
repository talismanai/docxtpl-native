"""Footnote XML preserves Jinja's newline normalization."""
import io
import unittest
import zipfile
from types import ModuleType

from docx import Document
from docx.opc.part import Part
from docx.opc.packuri import PackURI
from docx.oxml.ns import nsmap
import docxtpl
import docxtpl_native
import jinja2


def source_with_footnotes(text):
    doc = Document()
    doc.add_paragraph('body')
    part = doc.part
    notes = Part(PackURI('/word/footnotes.xml'),
        'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml',
        text.encode('utf-8'), part.package)
    part.relate_to(notes, 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes')
    result = io.BytesIO()
    doc.save(result)
    return result.getvalue()


def render_package(engine: ModuleType, blob: bytes, mode: str = 'direct') -> zipfile.ZipFile:
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    context = {'value': 'hello'}
    if mode == 'subdoc':
        context['sub'] = tpl.new_subdoc()
        context['sub'].add_paragraph('sub')
    tpl.render(context, jinja2.Environment() if mode == 'environment' else None)
    output = io.BytesIO()
    tpl.save(output)
    return zipfile.ZipFile(io.BytesIO(output.getvalue()))


def render_notes(engine: ModuleType, blob: bytes, mode: str = 'direct') -> bytes:
    with render_package(engine, blob, mode) as package:
        return package.read('word/footnotes.xml')


class NativeFootnoteNewlineTests(unittest.TestCase):
    def test_footnote_newlines_match_reference(self):
        for newline in ('\r\n', '\r', '\n'):
            for text in ('fixed', '{{ value }}'):
                with self.subTest(newline=repr(newline), text=text):
                    source = ('<?xml version="1.0" encoding="UTF-8"?>' + newline +
                        f'<w:footnotes xmlns:w="{nsmap["w"]}"><w:footnote w:id="1">' +
                        newline + f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' + newline +
                        '</w:footnote></w:footnotes>')
                    blob = source_with_footnotes(source)
                    self.assertEqual(render_notes(docxtpl_native, blob), render_notes(docxtpl, blob))

    def test_live_model_footnote_newlines_match_reference(self) -> None:
        # Subdocuments and supplied environments select the exposed model.
        for mode in ('subdoc', 'environment'):
            for newline in ('\r\n', '\r'):
                for text in ('fixed', '{{ value }}'):
                    with self.subTest(mode=mode, newline=repr(newline), text=text):
                        source = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + newline +
                            f'<w:footnotes xmlns:w="{nsmap["w"]}"><w:footnote w:id="1">' +
                            f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:footnote></w:footnotes>')
                        blob = source_with_footnotes(source)
                        expected = render_notes(docxtpl, blob, mode)
                        self.assertNotIn(b'\r', expected)
                        self.assertEqual(render_notes(docxtpl_native, blob, mode), expected)

    def test_property_newlines_match_reference(self) -> None:
        doc = Document()
        doc.add_paragraph('body')
        doc.core_properties.title = 'a\r\nb\rc {{ value }}'
        source = io.BytesIO()
        doc.save(source)
        blob = source.getvalue()
        for mode in ('direct', 'subdoc', 'environment'):
            with self.subTest(mode=mode):
                titles = []
                for engine in (docxtpl, docxtpl_native):
                    with render_package(engine, blob, mode) as package:
                        titles.append(package.read('docProps/core.xml'))
                self.assertIn(b'a\nb\nc hello', titles[0])
                self.assertEqual(titles[1], titles[0])
