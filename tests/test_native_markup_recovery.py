"""Markup rendered into the body is recovered as docxtpl's fix_tables does with lxml."""

import io
import re
import unittest
import warnings
import zipfile
from typing import Any

import docxtpl
import docxtpl_native
from paritylab.protocol_base import template


def body(engine: Any, source: str, context: dict[str, Any]) -> str:
    document = engine.DocxTemplate(io.BytesIO(template(source)))
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        document.render(context)
    output = io.BytesIO()
    document.save(output)
    xml = zipfile.ZipFile(output).read('word/document.xml').decode()
    match = re.search(r'<w:body>(.*?)<w:sectPr', xml, re.S)
    assert match is not None
    return re.sub(r'0x[0-9a-f]+', '0x', match.group(1))


class MarkupRecoveryTests(unittest.TestCase):
    def test_object_repr_and_malformed_tags_match_reference(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            # A Python value materialized after the tables were fixed.
            ('{{ missing|reverse }}', {}),
            # An attribute without a value ends the start tag.
            ('A {{ value }} B', {'value': '<x y z>t'}),
            ('{{ value }}', {'value': '<b c="1" d>t'}),
            # An unquoted attribute value.
            ('{{ value }}', {'value': '<b c=d>t'}),
            # Well-formed markup stays as it is.
            ('{{ value }}', {'value': '<b>bold</b>'}),
        ]
        for source, context in cases:
            with self.subTest(source=source, context=context):
                self.assertEqual(body(docxtpl_native, source, context), body(docxtpl, source, context))

    def test_stray_less_than_is_kept_as_text(self) -> None:
        # A deliberate difference (docs/STATUS.md): lxml drops the `<`; here it stays text.
        self.assertIn('a &lt; b', body(docxtpl_native, '{{ value }}', {'value': 'a < b'}))


if __name__ == '__main__':
    unittest.main()
