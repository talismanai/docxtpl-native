"""Part parsing preserves the exposed document parser and callbacks."""
import unittest
import docxtpl
import docxtpl_native
import docx.opc
from docx.opc.oxml import parse_xml


class NativePartXmlTests(unittest.TestCase):
    def test_rebinding_package_attribute_does_not_replace_parser_globals(self):
        docxtpl_native._extension()
        original = docx.opc.oxml
        class Part: blob = b'<ok/>'
        try:
            docx.opc.oxml = object()
            self.assertEqual(docxtpl_native.DocxTemplate(None).get_part_xml(Part()),
                             docxtpl.DocxTemplate(None).get_part_xml(Part()))
        finally:
            docx.opc.oxml = original

    def test_same_parser_and_serialized_value(self):
        class Part:
            blob = b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:r><w:t>hi &amp; bye</w:t></w:r></w:p>'
        reference = docxtpl.DocxTemplate(None).get_part_xml(Part())
        candidate = docxtpl_native.DocxTemplate(None).get_part_xml(Part())
        self.assertEqual(candidate, reference)
        self.assertIs(type(parse_xml(Part.blob)), type(parse_xml(candidate)))

    def test_serializer_override_order_return_and_tree_type(self):
        def observe(engine):
            trace = []; sentinel = object()
            class Part:
                @property
                def blob(self): trace.append('blob'); return b'<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'
            class Template(engine.DocxTemplate):
                def __getattribute__(self, name):
                    if name == 'xml_to_string': trace.append('serializer lookup')
                    return super().__getattribute__(name)
                def xml_to_string(self, tree):
                    trace.append(('serialize', type(tree).__name__))
                    return sentinel
            result = Template(None).get_part_xml(Part())
            return result is sentinel, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_parse_errors_and_callback_exceptions(self):
        def observe(engine, content, fail=False):
            error = RuntimeError('serializer failure')
            class Part: blob = content
            class Template(engine.DocxTemplate):
                def xml_to_string(self, tree):
                    if fail: raise error
                    return super().xml_to_string(tree)
            try: Template(None).get_part_xml(Part())
            except Exception as exc: return type(exc).__name__, str(exc), exc is error
            raise AssertionError('expected exception')
        for content, fail in [(b'<bad>', False), (b'<ok/>', True), (None, False)]:
            with self.subTest(content=content, fail=fail):
                self.assertEqual(observe(docxtpl_native, content, fail), observe(docxtpl, content, fail))
