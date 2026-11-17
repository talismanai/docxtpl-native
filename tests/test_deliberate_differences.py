"""docxtpl behaviours docxtpl-native deliberately does not reproduce.

Reproducing them would damage the document. Each test renders the same
template with docxtpl and docxtpl-native and pins both results, so the
difference stays deliberate and visible. Copied-list nsids are covered in
test_native_deferred_marker and the engine-level cases in vendor/docxtplrs
(patch.rs, recover.rs, numbering.rs). docs/STATUS.md gives the reason for
each difference.
"""

import html
import io
import re
import unittest
import zipfile
from collections.abc import Mapping
from types import ModuleType

import docxtpl
import docxtpl_native
from docx import Document


def render(engine: ModuleType, source: bytes, context: Mapping[str, object], autoescape: bool = False) -> str:
    """The main document XML of `source` rendered by `engine`."""
    document = engine.DocxTemplate(io.BytesIO(source))
    document.render(dict(context), autoescape=autoescape)
    output = io.BytesIO()
    document.save(output)
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as package:
        return package.read('word/document.xml').decode()


def text(xml: str) -> list[str]:
    return [html.unescape(value) for value in re.findall(r'<w:t(?:\s[^>]*)?>([^<]*)</w:t>', xml)]


class DeliberateDifferencesTests(unittest.TestCase):
    def test_dash_merge_does_not_delete_a_following_table(self) -> None:
        """docxtpl's `-%}` pattern stops at `<w:tbl>`/`<w:tc>`... and deletes
        the markup in between: the text before the tag and the table start."""
        document = Document()
        document.add_paragraph('antes {% if True -%}')
        document.add_table(rows=1, cols=1).cell(0, 0).text = 'celula'
        document.add_paragraph('{% endif %} depois')
        stream = io.BytesIO()
        document.save(stream)
        source = stream.getvalue()

        reference = render(docxtpl, source, {})
        self.assertEqual(reference.count('<w:tbl>'), 0)
        self.assertEqual(text(reference), ['celula', ' depois'])

        native = render(docxtpl_native, source, {})
        self.assertEqual(native.count('<w:tbl>'), 1)
        self.assertEqual(native.count('</w:tbl>'), 1)
        self.assertEqual(text(native), ['antes ', 'celula', ' depois'])

    def test_ill_formed_text_is_kept(self) -> None:
        """libxml2's recover mode drops a stray `&`, and every entity reference
        after the first error, from the text."""
        document = Document()
        document.add_paragraph('{{ value }}')
        document.add_paragraph('depois & {{ tail }}')
        stream = io.BytesIO()
        document.save(stream)
        source = stream.getvalue()
        context = {'value': 'A & B < C', 'tail': 'fim'}

        self.assertEqual(text(render(docxtpl, source, context)), ['A  B  C', 'depois  fim'])
        self.assertEqual(text(render(docxtpl_native, source, context)), ['A & B < C', 'depois & fim'])


if __name__ == '__main__':
    unittest.main()
