"""docxtpl objects given to this package (see the divergences in docs/API.md).

The package does not recognise docxtpl's classes: their instances are ordinary
Python objects to the engine. They still render through their own ``__str__``
and ``__html__`` on the Python-model path, which these tests compare with
docxtpl. docxtpl 0.17 compatibility is out of scope; its former tests were
removed with the shim they covered.
"""

from __future__ import annotations

import base64
import io
import unittest
from typing import Any

import docxtpl
import docxtpl_native
from docx import Document
from paritylab.compare import compare_docx


class ReferenceObjectTests(unittest.TestCase):
    def test_classes_are_unrelated(self) -> None:
        for name in ("DocxTemplate", "RichText", "RichTextParagraph", "Listing", "InlineImage", "Subdoc"):
            with self.subTest(name=name):
                ours, reference = getattr(docxtpl_native, name), getattr(docxtpl, name)
                self.assertFalse(issubclass(ours, reference))
                self.assertFalse(issubclass(reference, ours))
        self.assertNotIsInstance(docxtpl.InlineImage(None, io.BytesIO(b"image")), docxtpl_native.InlineImage)
        self.assertNotIsInstance(docxtpl_native.RichTextParagraph("Text"), docxtpl.RichTextParagraph)

    def test_reference_values_are_not_package_values(self) -> None:
        values = (docxtpl.InlineImage(None, io.BytesIO(b"image")), docxtpl.RichText("x"),
                  docxtpl.RichTextParagraph("x"), docxtpl.Listing("x"))
        for value in values:
            with self.subTest(type=type(value).__name__):
                with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature,
                                            "context type docxtpl\\.[a-z_]+\\." + type(value).__name__):
                    docxtpl_native._value(value)

    def test_reference_rich_text_is_not_merged_into_package_rich_text(self) -> None:
        ours = docxtpl_native.RichText()
        ours.add(docxtpl.RichText("inner"))
        self.assertNotIn("<w:t xml:space=\"preserve\">inner</w:t>", ours.xml)
        self.assertIn("&lt;w:r&gt;", ours.xml)
        merged = docxtpl_native.RichText()
        merged.add(docxtpl_native.RichText("inner"))
        self.assertEqual(merged.xml, docxtpl_native.RichText("inner").xml)

    def test_render_reference_image_in_complete_document(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
        document = Document()
        document.add_paragraph("Imagem: {{ imagem }}")
        source = io.BytesIO()
        document.save(source)

        class Derived(docxtpl.InlineImage):
            pass

        def render(engine: Any, image_class: type[Any]) -> bytes:
            template = engine.DocxTemplate(io.BytesIO(source.getvalue()))
            image = image_class(template, io.BytesIO(png))
            template.render({"imagem": image})
            output = io.BytesIO()
            template.save(output)
            return output.getvalue()

        for image_class in (docxtpl.InlineImage, Derived):
            with self.subTest(image_class=image_class.__name__):
                result = compare_docx(render(docxtpl, image_class), render(docxtpl_native, image_class))
                self.assertTrue(result["equivalent"], result["differences"])

    def test_render_with_reference_richtext_and_changed_values(self) -> None:
        document = Document()
        document.add_paragraph('Café {{r name }}')
        document.sections[0].header.paragraphs[0].text = '{{ title }}'
        source = io.BytesIO()
        document.save(source)

        def render(engine: Any, name: str) -> bytes:
            template = engine.DocxTemplate(io.BytesIO(source.getvalue()))
            template.render({'name': docxtpl.RichText(name), 'title': 'Header'})
            output = io.BytesIO()
            template.save(output)
            return output.getvalue()

        for name in ('Ana', 'Beatriz'):
            with self.subTest(name=name):
                reference = render(docxtpl, name)
                candidate = render(docxtpl_native, name)
                result = compare_docx(reference, candidate)
                self.assertTrue(result['equivalent'], result['differences'])
                self.assertEqual(Document(io.BytesIO(candidate)).paragraphs[0].text, f'Café {name}')



if __name__ == '__main__':
    unittest.main()
