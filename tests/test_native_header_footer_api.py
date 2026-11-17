"""Observable lazy relationship enumeration matches docxtpl."""
import unittest
import sys
import docxtpl
import docxtpl_native


class HeaderFooterApiTests(unittest.TestCase):
    def test_attribute_name_identity_is_preserved_for_custom_objects(self):
        def observe(engine):
            trace = []; names = {name: sys.intern(name) for name in ('reltype', 'target_part', 'blob')}
            class Part:
                def __getattribute__(self, name):
                    if name == 'blob': trace.append((name, name is names[name])); return b'x'
                    return object.__getattribute__(self, name)
            class Relation:
                def __getattribute__(self, name):
                    if name in ('reltype', 'target_part'):
                        trace.append((name, name is names[name]))
                        return 'header' if name == 'reltype' else part
                    return object.__getattribute__(self, name)
            class Object: pass
            part = Part(); owner = Object(); owner.rels = {'r': Relation()}
            document = Object(); document._part = owner
            tpl = engine.DocxTemplate(None); tpl.docx = document
            return list(tpl.get_headers_footers('header'))[0][0], trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_generator_throw_propagates_original_exception_and_closes(self):
        def observe(engine):
            class Object: pass
            part = Object(); part.blob = b'x'
            relation = Object(); relation.reltype = 'header'; relation.target_part = part
            owner = Object(); owner.rels = {'a': relation, 'b': relation}
            document = Object(); document._part = owner
            tpl = engine.DocxTemplate(None); tpl.docx = document
            iterator = tpl.get_headers_footers('header'); next(iterator)
            failure = ValueError('injected')
            try: iterator.throw(failure)
            except ValueError as actual: same = actual is failure
            else: raise AssertionError('expected injected exception')
            try: next(iterator)
            except StopIteration: closed = True
            else: closed = False
            return same, closed
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_generator_send_ignores_value_and_continues(self):
        def observe(engine):
            class Object: pass
            first = Object(); first.blob = b'a'
            second = Object(); second.blob = b'b'
            a = Object(); a.reltype = 'header'; a.target_part = first
            b = Object(); b.reltype = 'header'; b.target_part = second
            owner = Object(); owner.rels = {'a': a, 'b': b}
            document = Object(); document._part = owner
            tpl = engine.DocxTemplate(None); tpl.docx = document
            iterator = tpl.get_headers_footers('header')
            first_item = next(iterator); second_item = iterator.send('ignored')
            try: iterator.send('ignored again')
            except StopIteration as error: end = error.value
            else: raise AssertionError('expected exhaustion')
            return first_item[0], first_item[1] is first, second_item[0], second_item[1] is second, end
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_iteration_is_lazy_and_preserves_order_and_part_identity(self):
        def observe(engine):
            trace = []
            class Part:
                def __init__(self, name, blob): self.name, self.value = name, blob
                @property
                def blob(self): trace.append(('blob', self.name)); return self.value
            class Rel:
                def __init__(self, name, kind, part): self.name, self.kind, self.part = name, kind, part
                @property
                def reltype(self): trace.append(('type', self.name)); return self.kind
                @property
                def target_part(self): trace.append(('part', self.name)); return self.part
            class Object: pass
            part = Object(); part.rels = {}
            document = Object(); document._part = part
            tpl = engine.DocxTemplate(None); tpl.docx = document
            first = Part('first', b'x'); empty = Part('empty', b''); second = Part('second', b'y')
            iterator = tpl.get_headers_footers('header')
            before = list(trace)
            part.rels.update({'a': Rel('a', 'other', first), 'b': Rel('b', 'header', empty),
                              'c': Rel('c', 'header', first), 'd': Rel('d', 'header', second)})
            found = list(iterator)
            return before, [(key, value.name) for key, value in found], trace, found[0][1] is first
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_missing_document_error_is_deferred_until_iteration(self):
        for engine in (docxtpl, docxtpl_native):
            tpl = engine.DocxTemplate(None)
            iterator = tpl.get_headers_footers('header')
            with self.assertRaises(AttributeError): next(iterator)

    def test_dictionary_mutation_during_iteration_raises(self):
        def observe(engine):
            class Object: pass
            part = Object(); part.blob = b'x'
            relation = Object(); relation.reltype = 'header'; relation.target_part = part
            owner = Object(); owner.rels = {'a': relation}
            document = Object(); document._part = owner
            tpl = engine.DocxTemplate(None); tpl.docx = document
            iterator = tpl.get_headers_footers('header'); next(iterator)
            owner.rels['b'] = relation
            try: next(iterator)
            except RuntimeError as error: return str(error)
            raise AssertionError('expected dictionary mutation error')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_non_tuple_pairs_and_unpacking_errors(self):
        def observe(engine, size):
            class Object: pass
            part = Object(); part.blob = b'x'
            relation = Object(); relation.reltype = 'header'; relation.target_part = part
            class Relationships:
                def items(self): return iter([iter(['a', relation, 'extra'][:size])])
            owner = Object(); owner.rels = Relationships()
            document = Object(); document._part = owner
            tpl = engine.DocxTemplate(None); tpl.docx = document
            try:
                return [(key, item is part) for key, item in tpl.get_headers_footers('header')]
            except ValueError as error:
                return str(error)
        for size in (0, 1, 2, 3):
            with self.subTest(size=size):
                self.assertEqual(observe(docxtpl_native, size), observe(docxtpl, size))
