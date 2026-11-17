"""Reference footnote parts always decode and encode bytes as UTF-8."""
import io
import unittest
import zipfile

from docx import Document
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml.ns import nsmap
import docxtpl
import docxtpl_native

NOTE_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'
NOTE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes'


def source_with_note(blob):
    document = Document()
    document.add_paragraph('body')
    note = Part(PackURI('/word/footnotes.xml'), NOTE_TYPE, blob, document.part.package)
    document.part.relate_to(note, NOTE_REL)
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def render_note(engine, blob):
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    tpl.render({'value': '世界'})
    output = io.BytesIO()
    tpl.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        return package.read('word/footnotes.xml')


class NativeFootnoteEncodingTests(unittest.TestCase):
    def test_utf8_regardless_of_declaration_and_bom(self):
        for encoding, bom in [('ISO-8859-1', b''), ('UTF-8', b'\xef\xbb\xbf')]:
            with self.subTest(encoding=encoding, bom=bool(bom)):
                xml = (f'<?xml version="1.0" encoding="{encoding}"?>'
                       f'<w:footnotes xmlns:w="{nsmap["w"]}"><w:footnote w:id="1">'
                       '<w:p><w:r><w:t>café {{ value }}</w:t></w:r></w:p>'
                       '</w:footnote></w:footnotes>').encode('utf-8')
                blob = source_with_note(bom + xml)
                self.assertEqual(render_note(docxtpl_native, blob), render_note(docxtpl, blob))

    def test_invalid_utf8_matches_decode_error(self):
        invalid = (b'\xff', b'\xc3', b'\xe2(\xa1', b'\xed\xa0\x80',
                   b'prefix \xe2(\xa1', b'prefix \xf0\x9f')
        for bad in invalid:
            with self.subTest(bad=bad):
                blob = source_with_note(bad)
                def observe_error(engine):
                    try:
                        engine.DocxTemplate(io.BytesIO(blob)).render({})
                    except Exception as exc:
                        return (type(exc).__name__, str(exc), getattr(exc, 'encoding', None),
                                getattr(exc, 'object', None), getattr(exc, 'start', None),
                                getattr(exc, 'end', None), getattr(exc, 'reason', None))
                    return None
                self.assertEqual(observe_error(docxtpl_native), observe_error(docxtpl))
