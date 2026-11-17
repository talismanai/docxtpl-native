"""In-place docPr renumbering must preserve Python object protocols."""
import unittest
import sys
import builtins
import docxtpl
import docxtpl_native
from docx.oxml.ns import nsmap
from lxml import etree


class NativeFixDocPrTests(unittest.TestCase):
    def test_does_not_import_namespace_module_during_call(self):
        class Tree:
            def xpath(self, expression, *, namespaces): return []
        def observe(engine):
            old = builtins.__import__
            def guarded(name, *args, **kwargs):
                if name == 'docx.oxml.ns': raise RuntimeError('unexpected runtime import')
                return old(name, *args, **kwargs)
            try:
                builtins.__import__ = guarded
                return engine.DocxTemplate(None).fix_docpr_ids(Tree())
            finally:
                builtins.__import__ = old
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_xpath_expression_has_reference_identity_semantics(self):
        def observe(engine):
            expressions = []
            class Tree:
                def xpath(self, expression, *, namespaces): expressions.append(expression); return []
            tpl = engine.DocxTemplate(None)
            tpl.fix_docpr_ids(Tree()); tpl.fix_docpr_ids(Tree())
            return expressions[0] is expressions[1], expressions[0] is sys.intern('//wp:docPr')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_in_place_updates_and_counter(self):
        xml = '<root xmlns:wp="%s"><wp:docPr id="4"/><wp:docPr id="4"/></root>' % nsmap['wp']
        for engine in (docxtpl, docxtpl_native):
            tree = etree.fromstring(xml); tpl = engine.DocxTemplate(None); tpl.docx_ids_index = 1000
            self.assertIsNone(tpl.fix_docpr_ids(tree))
            self.assertEqual([item.get('id') for item in tree], ['1001', '1002'])
            self.assertEqual(tpl.docx_ids_index, 1002)

    def test_inplace_arithmetic_and_mapping_protocol_order(self):
        def observe(engine):
            trace = []
            class Counter:
                def __init__(self): self.value = 1000
                def __iadd__(self, other): trace.append(('iadd', other)); self.value += other; return self
                def __str__(self): trace.append('str'); return str(self.value)
            class Mapping:
                def __setitem__(self, key, value): trace.append(('set', key, value))
            class Element: attrib = Mapping()
            class Tree:
                def xpath(self, expression, *, namespaces):
                    trace.append(('xpath', expression, namespaces is nsmap))
                    return [Element(), Element()]
            tpl = engine.DocxTemplate(None); counter = Counter(); tpl.docx_ids_index = counter
            result = tpl.fix_docpr_ids(Tree())
            return result, tpl.docx_ids_index is counter, counter.value, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_mutation_failure_preserves_counter_and_original_exception(self):
        def observe(engine):
            failure = RuntimeError('write failed')
            class Mapping:
                def __setitem__(self, key, value): raise failure
            class Element: attrib = Mapping()
            class Tree:
                def xpath(self, expression, *, namespaces): return [Element()]
            tpl = engine.DocxTemplate(None); tpl.docx_ids_index = 20
            try: tpl.fix_docpr_ids(Tree())
            except RuntimeError as error: return error is failure, str(error), tpl.docx_ids_index
            raise AssertionError('expected original exception')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
