"""Differential checks for the native RichTextParagraph construction path."""
import unittest

import docxtpl
import docxtpl_native
from docxtpl_native import _native


class RichTextParagraphTests(unittest.TestCase):
    def test_native_paragraph_xml_matches_reference(self):
        for text in ("plain", "Café <&> 世界", "", "  spaces  "):
            for style in (None, "", "Heading1", 'quote"style'):
                with self.subTest(text=text, style=style):
                    expected = docxtpl.RichTextParagraph()
                    expected.add(text, parastyle=style)
                    runs = docxtpl.RichText(text)
                    self.assertEqual(
                        _native.python_richtext_paragraph(runs.xml, style),
                        expected.xml,
                    )

    def test_wrapper_matches_reference_and_preserves_original_objects(self):
        # Each engine receives rich text built with its own RichText class.
        def build(engine):
            paragraph = engine.RichTextParagraph()
            for text, style in [
                ("", None), ("one <&>", "Title"),
                (engine.RichText("original", bold=True), None),
                (engine.RichText("native", italic=True), ""),
                (b"bytes", "Body"), (123, None), ("surrogate:\ud800", None),
            ]:
                paragraph.add(text, parastyle=style)
            return paragraph
        self.assertEqual(build(docxtpl_native).xml, build(docxtpl).xml)
        self.assertIs(docxtpl_native.RP, docxtpl_native.RichTextParagraph)

    def test_unknown_style_uses_reference_protocol(self):
        class Style:
            def __str__(self):
                return "custom"
        reference = docxtpl.RichTextParagraph()
        candidate = docxtpl_native.RichTextParagraph()
        reference.add("text", parastyle=Style())
        candidate.add("text", parastyle=Style())
        self.assertEqual(candidate.xml, reference.xml)


if __name__ == "__main__":
    unittest.main()
