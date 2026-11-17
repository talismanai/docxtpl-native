"""Direct part rendering uses the native runtime and preserves callbacks."""
import array
import unittest
import jinja2
import docxtpl
import docxtpl_native


class NativeRenderXmlPartTests(unittest.TestCase):
    def test_invalid_input_errors_and_state_match_reference(self):
        def observe(engine, value):
            tpl = engine.DocxTemplate(None)
            previous = object(); tpl.current_rendering_part = previous
            try: tpl.render_xml_part(value, object(), {})
            except TypeError as error:
                return str(error), tpl.current_rendering_part is previous
            raise AssertionError('expected input error')
        for value in (None, 1, b'<w:p/>', bytearray(b'x'), memoryview(b'x'),
                      memoryview(b'abcd')[::2], array.array('i', [1]), []):
            with self.subTest(value=repr(value)):
                self.assertEqual(observe(docxtpl_native, value), observe(docxtpl, value))

    def test_custom_buffer_protocol_side_effects_match_reference(self):
        def observe(engine):
            trace = []
            class Buffer:
                def __buffer__(self, flags): trace.append(('acquire', flags)); return memoryview(b'x')
                def __release_buffer__(self, view): trace.append(('release', view.tobytes()))
            try: engine.DocxTemplate(None).render_xml_part(Buffer(), object(), {})
            except TypeError as error: return str(error), trace
            raise AssertionError('expected input error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_direct_render_sets_part_and_resolves_listing(self):
        def observe(engine):
            tpl = engine.DocxTemplate(None)
            part = object()
            trace = []
            def callback(value):
                trace.append(('callback', tpl.current_rendering_part is part, value))
                return value
            xml = '<w:p><w:r><w:t>{{ callback(value) }}</w:t></w:r></w:p>'
            result = tpl.render_xml_part(xml, part, {'value': 'A\tB', 'callback': callback})
            return result, tpl.current_rendering_part is part, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_overlapping_escaped_delimiters_match_reference(self):
        for value in ('{_{_%','{_%_}','%_}_}'):
            with self.subTest(value=value):
                xml='<w:p>{{ value }}</w:p>'
                reference=docxtpl.DocxTemplate(None).render_xml_part(xml,object(),{'value':value})
                native=docxtpl_native.DocxTemplate(None).render_xml_part(xml,object(),{'value':value})
                self.assertEqual(native,reference)

    def test_listing_override_receives_rendered_xml(self):
        def observe(engine):
            calls = []
            marker = object()
            class Template(engine.DocxTemplate):
                def resolve_listing(self, xml):
                    calls.append(xml)
                    return marker
            tpl = Template(None)
            result = tpl.render_xml_part('<w:p>{{ value }}</w:p>', object(), {'value': 'X'})
            return result is marker, calls
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_callback_exception_preserves_instance(self):
        def observe(engine):
            failure = RuntimeError('callback failed')
            part = object()
            tpl = engine.DocxTemplate(None)
            def fail(): raise failure
            try: tpl.render_xml_part('<w:p>{{ fail() }}</w:p>', part, {'fail': fail})
            except RuntimeError as error:
                return error is failure, str(error), tpl.current_rendering_part is part
            raise AssertionError('expected callback error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_template_syntax_error_context(self):
        def observe(engine):
            part = object()
            tpl = engine.DocxTemplate(None)
            try: tpl.render_xml_part('<w:p>{% if %}</w:p>', part, {})
            except jinja2.TemplateSyntaxError as error:
                return str(error), error.lineno, list(error.docx_context), tpl.current_rendering_part is part
            raise AssertionError('expected syntax error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
