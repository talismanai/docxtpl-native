"""Reference-order coverage for styles imported from a file subdocument."""
import io
import unittest
import zipfile

import docxtpl
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree


def save(document):
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def style_order(engine):
    master = Document()
    master.add_paragraph("{{p subdoc }}")
    child = Document()
    later = child.styles.add_style("Z Later Test", WD_STYLE_TYPE.PARAGRAPH)
    linked = child.styles.add_style("Z Linked Test", WD_STYLE_TYPE.CHARACTER)
    heading = child.styles.add_style("Z Heading Test", WD_STYLE_TYPE.PARAGRAPH)
    link = OxmlElement("w:link")
    link.set(qn("w:val"), linked.style_id)
    heading.element.append(link)
    child.add_paragraph("first", heading)
    child.add_paragraph("second", later)

    template = engine.DocxTemplate(io.BytesIO(save(master)))
    template.render({"subdoc": template.new_subdoc(io.BytesIO(save(child)))})
    output = io.BytesIO()
    template.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        root = etree.fromstring(package.read("word/styles.xml"))
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    names = {heading.style_id, linked.style_id, later.style_id}
    return [element.get(ns + "styleId") for element in root
            if element.tag == ns + "style" and element.get(ns + "styleId") in names]


class SubdocumentStyleOrderTests(unittest.TestCase):
    def test_reference_style_order(self):
        self.assertEqual(style_order(docxtpl), ["ZHeadingTest", "ZLinkedTest", "ZLaterTest"])

    def test_linked_style_is_inserted_after_first_referenced_style(self):
        import docxtpl_native
        self.assertEqual(style_order(docxtpl_native), style_order(docxtpl))


if __name__ == "__main__":
    unittest.main()
