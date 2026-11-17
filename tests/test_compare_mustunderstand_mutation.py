import unittest

from paritylab.compare import compare_docx
from test_compare import document, mutate


class MustUnderstandMutationTests(unittest.TestCase):
    def test_changed_binding_in_prefix_list_is_detected(self) -> None:
        def add_attribute(xml: bytes) -> bytes:
            return xml.replace(
                b"<w:document ",
                b'<w:document xmlns:first="urn:first" xmlns:second="urn:original" '
                b'xmlns:unused="urn:unused" '
                b'mc:MustUnderstand="first second" ',
                1,
            )

        original = mutate(document(), "word/document.xml", add_attribute)
        changed = mutate(
            original,
            "word/document.xml",
            lambda xml: xml.replace(b"urn:original", b"urn:changed"),
        )
        self.assertTrue(compare_docx(original, original)["equivalent"])
        unused_changed = mutate(
            original,
            "word/document.xml",
            lambda xml: xml.replace(b"urn:unused", b"urn:unused-changed"),
        )
        self.assertTrue(compare_docx(original, unused_changed)["equivalent"])
        result = compare_docx(original, changed)
        self.assertTrue(result["content_equal"])
        self.assertFalse(result["structure_and_format_equal"])
        self.assertFalse(result["equivalent"])
