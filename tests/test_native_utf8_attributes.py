"""Non-ASCII attribute values are saved as raw UTF-8, as lxml writes them."""
import io
import unittest
import zipfile
from types import ModuleType

from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
import docxtpl
import docxtpl_native

URL = "https://example.test/façade?q=a&quoted='yes'"


def template() -> bytes:
    doc = Document()
    paragraph = doc.add_paragraph('{{ link }}')
    paragraph._p.append(parse_xml(f'<w:bookmarkStart {nsdecls("w")} w:id="0" w:name="façade"/>'))
    paragraph._p.append(parse_xml(f'<w:bookmarkEnd {nsdecls("w")} w:id="0"/>'))
    output = io.BytesIO()
    doc.save(output)
    return output.getvalue()


def saved_parts(engine: ModuleType, blob: bytes) -> dict[str, bytes]:
    tpl = engine.DocxTemplate(io.BytesIO(blob))
    tpl.render({'link': engine.RichText('link', url_id=tpl.build_url_id(URL))})
    output = io.BytesIO()
    tpl.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        return {name: package.read(name) for name in package.namelist()}


class NativeUtf8AttributeTests(unittest.TestCase):
    def test_hyperlink_target_and_body_attributes_match_reference(self) -> None:
        blob = template()
        expected = saved_parts(docxtpl, blob)
        self.assertIn('façade'.encode(), expected['word/_rels/document.xml.rels'])
        self.assertIn('w:name="façade"'.encode(), expected['word/document.xml'])
        self.assertEqual(saved_parts(docxtpl_native, blob), expected)


if __name__ == '__main__':
    unittest.main()
