"""Differential checks for file-subdocument master drawing IDs."""
import io
import unittest
import zipfile

import docxtpl
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
from PIL import Image


def image(color):
    output = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(output, format="PNG")
    output.seek(0)
    return output


def saved(document):
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def child_with_structural_whitespace(document):
    source = io.BytesIO(saved(document))
    output = io.BytesIO()
    with zipfile.ZipFile(source) as archive, zipfile.ZipFile(output, "w") as rewritten:
        for entry in archive.infolist():
            payload = archive.read(entry.filename)
            if entry.filename == "word/document.xml":
                marker = b"<wp:extent "
                if marker not in payload:
                    raise AssertionError("inline drawing not found")
                payload = payload.replace(marker, b"\n    " + marker, 1)
            rewritten.writestr(entry, payload)
    return output.getvalue()


def collect_identifiers(engine, use_subdoc):
    master = Document()
    paragraph = master.add_paragraph()
    paragraph.add_run().add_picture(image("red"))
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), "17")
    start.set(qn("w:name"), "master_bookmark")
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), "17")
    paragraph._p.insert(0, start)
    paragraph._p.append(end)
    master.sections[0].header.paragraphs[0].add_run().add_picture(image("blue"))
    master.add_paragraph("{{p subdoc }}" if use_subdoc else "unused subdocument")

    child = Document()
    child.add_picture(image("green"))
    template = engine.DocxTemplate(io.BytesIO(saved(master)))
    subdoc = template.new_subdoc(io.BytesIO(child_with_structural_whitespace(child)))
    template.render({"subdoc": subdoc})
    output = io.BytesIO()
    template.save(output)
    ns = {
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
        "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    }
    result = {}
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        for part in ("word/document.xml", "word/header1.xml"):
            root = etree.fromstring(package.read(part))
            result[part] = {
                "pictures": [element.get("id") for element in root.xpath(".//pic:cNvPr", namespaces=ns)],
                "drawings": [element.get("id") for element in root.xpath(".//wp:docPr", namespaces=ns)],
                "bookmarks": [element.get(qn("w:id")) for element in root.xpath(".//w:bookmarkStart", namespaces=ns)],
                "inline_blank_nodes": [
                    value for inline in root.xpath(".//wp:inline", namespaces=ns)
                    for element in inline.iter()
                    for value in (element.text, element.tail)
                    if value is not None and value.isspace()
                ],
            }
    return result


class SubdocumentMasterIdentifiersTests(unittest.TestCase):
    def test_reference_stages_master_and_preserves_child_pictures(self):
        used = collect_identifiers(docxtpl, True)
        self.assertEqual(used["word/document.xml"]["pictures"], ["1", "0"])
        self.assertEqual(used["word/header1.xml"]["pictures"], ["2"])
        self.assertEqual(used["word/document.xml"]["bookmarks"], ["0"])
        self.assertEqual(used["word/document.xml"]["inline_blank_nodes"], [])

    def test_master_and_inserted_picture_ids_match_reference(self):
        import docxtpl_native
        for use_subdoc in (False, True):
            with self.subTest(use_subdoc=use_subdoc):
                self.assertEqual(collect_identifiers(docxtpl_native, use_subdoc),
                                 collect_identifiers(docxtpl, use_subdoc))


if __name__ == "__main__":
    unittest.main()
