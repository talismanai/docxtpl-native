"""Known XML parts must retain the reference's serialized bytes."""
import io
import unittest
import zipfile

import docxtpl
import docxtpl_native
from docx import Document
from docx.oxml.ns import qn
from paritylab.protocol_base import template

KNOWN_PARTS = ('word/document.xml', 'word/styles.xml', 'word/settings.xml',
               'word/numbering.xml', 'word/header1.xml', 'word/footer1.xml',
               'word/comments.xml', 'docProps/core.xml')
OPAQUE_PARTS = ('word/stylesWithEffects.xml', 'customXml/item1.xml')


def with_noncanonical_declarations(blob):
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(blob)) as source, zipfile.ZipFile(out, 'w') as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename in KNOWN_PARTS + OPAQUE_PARTS:
                old = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>\n"
                new = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                assert data.startswith(old)
                data = new + data[len(old):]
            target.writestr(info, data)
    return out.getvalue()


def render(engine, blob):
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    tpl.render({'value': 'hello'})
    out = io.BytesIO()
    tpl.save(out)
    return out.getvalue()


class NativeOpcXmlBytesTests(unittest.TestCase):
    def test_exposed_empty_text_node_survives_save(self):
        blob = template('visible')
        def render_and_mutate(engine):
            tpl = engine.DocxTemplate(io.BytesIO(blob))
            tpl.render({})
            text = tpl.docx.paragraphs[0].runs[0]._r.find(qn('w:t'))
            text.text = ''
            out = io.BytesIO(); tpl.save(out)
            with zipfile.ZipFile(io.BytesIO(out.getvalue())) as package:
                return package.read('word/document.xml')
        self.assertEqual(render_and_mutate(docxtpl_native), render_and_mutate(docxtpl))

    def test_known_xml_parts_match_reference_bytes(self):
        doc = Document()
        run = doc.add_paragraph('{{ value }}').runs[0]
        doc.add_comment(run, text='note', author='A')
        doc.sections[0].header.paragraphs[0].text = 'header'
        doc.sections[0].footer.paragraphs[0].text = 'footer'
        source = io.BytesIO(); doc.save(source)
        blob = with_noncanonical_declarations(source.getvalue())
        reference = render(docxtpl, blob)
        native = render(docxtpl_native, blob)
        with zipfile.ZipFile(io.BytesIO(reference)) as expected, zipfile.ZipFile(io.BytesIO(native)) as actual:
            for name in KNOWN_PARTS + OPAQUE_PARTS:
                with self.subTest(part=name):
                    self.assertEqual(actual.read(name), expected.read(name))
