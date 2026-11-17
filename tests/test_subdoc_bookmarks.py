"""Differential coverage for file-based subdocument bookmark IDs."""
import io
import unittest
import zipfile

import docxtpl
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree


def make_bookmark(paragraph, bookmark_id, name):
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def saved(document):
    result = io.BytesIO()
    document.save(result)
    return result.getvalue()


def render(engine, with_master_bookmark):
    master = Document()
    placeholder = master.add_paragraph("{{p subdoc }}")
    if with_master_bookmark:
        make_bookmark(placeholder, 0, "master")
    child = Document()
    make_bookmark(child.add_paragraph("first"), 0, "child_zero")
    make_bookmark(child.add_paragraph("second"), 7, "child_seven")
    template = engine.DocxTemplate(io.BytesIO(saved(master)))
    subdoc = template.new_subdoc(io.BytesIO(saved(child)))
    template.render({"subdoc": subdoc})
    result = io.BytesIO()
    template.save(result)
    with zipfile.ZipFile(io.BytesIO(result.getvalue())) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    return [(element.get(namespace + "name"), element.get(namespace + "id"))
            for element in root.iter(namespace + "bookmarkStart")]


class SubdocBookmarkTests(unittest.TestCase):
    def test_reference_preserves_file_bookmark_ids(self):
        for with_master_bookmark in (False, True):
            with self.subTest(with_master_bookmark=with_master_bookmark):
                children = [item for item in render(docxtpl, with_master_bookmark)
                            if item[0].startswith("child_")]
                self.assertEqual(children, [("child_zero", "0"), ("child_seven", "7")])

    def test_file_subdocument_preserves_its_bookmark_ids(self):
        import docxtpl_native
        for with_master_bookmark in (False, True):
            with self.subTest(with_master_bookmark=with_master_bookmark):
                expected = render(docxtpl, with_master_bookmark)
                actual = render(docxtpl_native, with_master_bookmark)
                expected_children = [item for item in expected if item[0].startswith("child_")]
                actual_children = [item for item in actual if item[0].startswith("child_")]
                self.assertEqual(actual_children, expected_children)


if __name__ == "__main__":
    unittest.main()
