"""The Python object graph stays authoritative when the body is replaced."""
import unittest
import sys
import docxtpl
import docxtpl_native


class NativeMapTreeTests(unittest.TestCase):
    def test_attribute_names_match_reference_identity(self):
        def observe(engine):
            trace = []; names = {name: sys.intern(name) for name in ('_element', 'body', 'replace')}
            class Root:
                def __getattribute__(self, name):
                    if name in ('body', 'replace'): trace.append((name, name is names[name]))
                    if name == 'body': return body
                    return object.__getattribute__(self, name)
                def replace(self, old, new): return None
            class Document:
                def __getattribute__(self, name):
                    if name == '_element': trace.append((name, name is names[name])); return Root()
                    return object.__getattribute__(self, name)
            body = object(); tpl = engine.DocxTemplate(None); tpl.docx = Document()
            tpl.map_tree(object())
            return trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_access_order_identity_and_return_value(self):
        def observe(engine):
            trace = []; body = object(); replacement = object()
            class Root:
                @property
                def body(self): trace.append('body'); return body
                def replace(self, old, new):
                    trace.append(('replace', old is body, new is replacement))
                    return 'ignored'
            class Document:
                @property
                def _element(self): trace.append('element'); return Root()
            tpl = engine.DocxTemplate(None); tpl.docx = Document()
            result = tpl.map_tree(replacement)
            return result, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_original_mutation_exception_is_preserved(self):
        def observe(engine):
            failure = RuntimeError('replace failed'); trace = []
            class Root:
                body = object()
                def replace(self, old, new): trace.append('replace'); raise failure
            class Document: _element = Root()
            tpl = engine.DocxTemplate(None); tpl.docx = Document()
            try: tpl.map_tree(object())
            except RuntimeError as error: return error is failure, str(error), trace
            raise AssertionError('expected original exception')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
