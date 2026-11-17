"""The native iterator must expose both of its Python references to GC."""
import gc
import unittest
import weakref
import docxtpl_native
from docxtpl_native import _native


class HeaderFooterGcTests(unittest.TestCase):
    def test_cycle_through_uri_can_be_collected(self):
        class Object: pass
        owner = Object(); owner.rels = {}
        document = Object(); document._part = owner
        tpl = docxtpl_native.DocxTemplate(None); tpl.docx = document
        uri = Object(); probe = weakref.ref(uri)
        iterator = _native.header_footer_iter(tpl, uri)
        uri.iterator = iterator
        del uri, iterator, tpl, document, owner
        gc.collect()
        self.assertIsNone(probe())

    def test_cycle_through_items_iterator_can_be_collected(self):
        class Items:
            def __iter__(self): return self
            def __next__(self): raise StopIteration
        class Relationships:
            def __init__(self, items): self.value = items
            def items(self): return self.value
        class Object: pass
        items = Items(); probe = weakref.ref(items)
        owner = Object(); owner.rels = Relationships(items)
        document = Object(); document._part = owner
        tpl = docxtpl_native.DocxTemplate(None); tpl.docx = document
        iterator = _native.header_footer_iter(tpl, 'header')
        items.parent = iterator
        del items, iterator, tpl, document, owner
        gc.collect()
        self.assertIsNone(probe())
