"""Building body XML respects method overrides and live part identity."""
import unittest
import docxtpl
import docxtpl_native


class NativeBuildXmlTests(unittest.TestCase):
    def test_callbacks_and_access_order(self):
        def observe(engine):
            trace = []; source = object(); patched = object(); part = object(); context = object(); env = object(); marker = object()
            class Document:
                @property
                def _part(self): trace.append('part'); return part
            class Template(engine.DocxTemplate):
                def get_xml(self): trace.append('get'); return source
                def patch_xml(self, value): trace.append(('patch', value is source)); return patched
                def render_xml_part(self, xml, actual_part, actual_context, actual_env):
                    trace.append(('render', xml is patched, actual_part is part, actual_context is context, actual_env is env))
                    return marker
            tpl = Template(None); tpl.docx = Document()
            return tpl.build_xml(context, env) is marker, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_patch_exception_does_not_access_document(self):
        def observe(engine):
            failure = RuntimeError('patch failed'); trace = []
            class Template(engine.DocxTemplate):
                def get_xml(self): trace.append('get'); return 'xml'
                def patch_xml(self, value): trace.append('patch'); raise failure
            tpl = Template(None)
            try: tpl.build_xml({})
            except RuntimeError as error: return error is failure, trace
            raise AssertionError('expected patch error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
