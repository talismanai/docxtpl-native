"""Footnote rendering follows package parts and document sections."""
import io
import unittest
import zipfile
from docx import Document
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml.ns import nsmap
import docxtpl, docxtpl_native

NOTE_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml'
NOTE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes'


def make_document(relation=NOTE_REL, content_type=NOTE_TYPE, sections=1, nested=False):
    doc=Document(); doc.add_paragraph('body')
    if sections==0:
        body=doc._element.body; body.remove(body.sectPr)
    for _ in range(sections-1): doc.add_section()
    xml=f'<w:footnotes xmlns:w="{nsmap["w"]}"><w:footnote w:id="1"><w:p><w:r><w:t>{{{{ value }}}}</w:t></w:r></w:p></w:footnote></w:footnotes>'
    note=Part(PackURI('/word/footnotes.xml'),content_type,xml.encode(),doc.part.package)
    owner=doc.sections[0].header.part if nested else doc.part
    owner.relate_to(note, relation)
    out=io.BytesIO();doc.save(out);return out.getvalue()


def observe(engine, data):
    tpl=engine.DocxTemplate(io.BytesIO(data));tpl.render({'value':'{{ later }}','later':'X'})
    out=io.BytesIO();tpl.save(out)
    with zipfile.ZipFile(io.BytesIO(out.getvalue())) as z:return z.read('word/footnotes.xml')


class NativeFootnoteDiscoveryTests(unittest.TestCase):
    def test_package_part_selection_and_section_repetition(self):
        for opts in ({'relation':'urn:custom'}, {'content_type':'application/xml'},
                     {'sections':0}, {'sections':2}, {'nested':True,'relation':'urn:custom'}):
            with self.subTest(**opts):
                data=make_document(**opts)
                self.assertEqual(observe(docxtpl_native,data),observe(docxtpl,data))
