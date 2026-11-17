"""The package's own classes, used without docxtpl (see docs/API.md)."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from docx import Document

import docxtpl_native
from docxtpl_native import DocxTemplate, InlineImage, Listing, RichText, RichTextParagraph, Subdoc

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_CLASSES = (DocxTemplate, RichText, RichTextParagraph, Listing, InlineImage, Subdoc,
                  docxtpl_native.UnsupportedFeature)


def template(*lines: str) -> bytes:
    source = Document()
    for line in lines:
        source.add_paragraph(line)
    data = io.BytesIO()
    source.save(data)
    return data.getvalue()


def rendered_text(tpl: DocxTemplate) -> list[str]:
    output = io.BytesIO()
    tpl.save(output)
    return [paragraph.text for paragraph in Document(io.BytesIO(output.getvalue())).paragraphs]


class OwnClassTests(unittest.TestCase):
    def test_classes_and_bases_belong_to_this_package_or_builtins(self) -> None:
        for cls in PUBLIC_CLASSES:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(cls.__module__.startswith("docxtpl_native"), cls.__module__)
                self.assertIs(type(cls), type)
                for base in cls.__mro__:
                    module = base.__module__
                    self.assertTrue(module == "builtins" or module.startswith("docxtpl_native"),
                                    f"{base!r} comes from {module}")

    def test_aliases(self) -> None:
        self.assertIs(docxtpl_native.R, RichText)
        self.assertIs(docxtpl_native.RP, RichTextParagraph)

    def test_inline_image_attribute_defaults(self) -> None:
        image = object.__new__(InlineImage)
        self.assertEqual([getattr(image, name) for name in
                          ("tpl", "image_descriptor", "width", "height", "anchor", "title", "descr")], [None] * 7)

    def test_inline_image_converts_title_and_description(self) -> None:
        image = InlineImage(None, "picture.png", title=42, descr=False)
        self.assertEqual((image.title, image.descr), ("42", "False"))
        self.assertIsNone(InlineImage(None, "picture.png").title)

    def test_rich_text_nesting_and_paragraph_wrapping(self) -> None:
        inner = RichText("inner", bold=True)
        outer = RichText("outer")
        outer.add(inner)
        self.assertTrue(outer.xml.endswith(inner.xml))
        self.assertEqual(str(outer), outer.xml)
        self.assertEqual(outer.__html__(), outer.xml)
        paragraph = RichTextParagraph()
        paragraph.add("plain", parastyle="Heading1")
        self.assertIn('<w:pStyle w:val="Heading1"/>', paragraph.xml)
        self.assertIn(RichText("plain").xml, paragraph.xml)

    def test_listing_escapes(self) -> None:
        self.assertEqual(str(Listing("a<b & c")), "a&lt;b &amp; c")
        self.assertEqual(str(Listing(12)), "12")


class SubclassTests(unittest.TestCase):
    def test_subclasses_render_like_their_bases(self) -> None:
        class Rich(RichText):
            pass

        class Text(Listing):
            pass

        tpl = DocxTemplate(io.BytesIO(template("{{r rich }}", "{{ text }}")))
        tpl.render({"rich": Rich("bold", bold=True), "text": Text("x<y")})
        self.assertEqual(rendered_text(tpl)[:2], ["bold", "x<y"])

    def test_programmatic_subdocument_subclass(self) -> None:
        class Part(Subdoc):
            pass

        tpl = DocxTemplate(io.BytesIO(template("{{p part }}")))
        part = Part(tpl)
        part.add_paragraph("inside")
        tpl.render({"part": part})
        self.assertIn("inside", rendered_text(tpl))


class CommandLineTests(unittest.TestCase):
    def run_cli(self, directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "docxtpl_native", *arguments], cwd=directory,
                              env={"PYTHONPATH": str(ROOT / "python"), "PATH": ""},
                              capture_output=True, text=True, check=False)

    def test_renders_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / "in.docx").write_bytes(template("{{ greeting }}"))
            (directory / "data.json").write_text(json.dumps({"greeting": "hi"}))
            result = self.run_cli(directory, "in.docx", "data.json", "out.docx")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(),
                             ["Document successfully generated and saved at out.docx", "Exiting program!"])
            self.assertEqual(Document(str(directory / "out.docx")).paragraphs[0].text, "hi")

    def test_invalid_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            result = self.run_cli(directory, "missing.docx", "data.json", "out.docx", "-q")
            self.assertEqual(result.stdout, 'Error: The specified template_path "missing.docx" is not valid.\n')
            # As in docxtpl, a usage error is raised before the reporting block.
            result = self.run_cli(directory, "only-one")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("RuntimeError: Correct usage is:\npython -m docxtpl_native [-h] [-o] [-q] "
                          "template_path json_path output_filename", result.stderr)


if __name__ == "__main__":
    unittest.main()
