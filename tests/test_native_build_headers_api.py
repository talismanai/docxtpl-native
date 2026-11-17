"""Lazy header/footer building preserves callbacks and generator behavior."""
import gc
import unittest
import weakref
import docxtpl
import docxtpl_native

class NativeBuildHeadersTests(unittest.TestCase):
    def test_laziness_send_and_callback_order(self):
        def observe(engine):
            trace = []; uri = object(); context = object(); env = object()
            parts = [object(), object()]
            class Text:
                def encode(self, encoding): trace.append(('encode', encoding)); return b'encoded'
            class Template(engine.DocxTemplate):
                def get_headers_footers(self, actual_uri):
                    trace.append(('iterate', actual_uri is uri))
                    yield ('first', parts[0]); yield ['second', parts[1]]
                def get_part_xml(self, part): trace.append(('xml', parts.index(part))); return 'xml'
                def get_headers_footers_encoding(self, xml): trace.append(('encoding', xml)); return 'utf-8'
                def patch_xml(self, xml): trace.append(('patch', xml)); return 'patched'
                def render_xml_part(self, xml, part, actual_context, actual_env):
                    trace.append(('render', xml, parts.index(part), actual_context is context, actual_env is env)); return Text()
            tpl = Template(None); gen = tpl.build_headers_footers_xml(context, uri, env)
            before = list(trace); first = next(gen); second = gen.send('ignored')
            try: next(gen)
            except StopIteration: done = True
            return before, first, second, done, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_exception_closes_generator_and_preserves_identity(self):
        def observe(engine):
            trace = []; failure = RuntimeError('part failed')
            class Template(engine.DocxTemplate):
                def get_headers_footers(self, uri):
                    trace.append('iter'); yield 'first', object(); yield 'second', object()
                def get_part_xml(self, part): trace.append('xml'); raise failure
            gen = Template(None).build_headers_footers_xml({}, 'header')
            try: next(gen)
            except RuntimeError as error: same = error is failure
            try: next(gen)
            except StopIteration: closed = True
            return same, closed, trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_throw_closes_generator_and_preserves_exception(self):
        def observe(engine):
            failure = RuntimeError('injected')
            class Template(engine.DocxTemplate):
                def get_headers_footers(self, uri): yield 'key', object()
                def get_part_xml(self, part): return 'xml'
                def get_headers_footers_encoding(self, xml): return 'utf-8'
                def patch_xml(self, xml): return xml
                def render_xml_part(self, xml, part, context, env): return xml
            gen = Template(None).build_headers_footers_xml({}, 'header')
            first = next(gen)
            try: gen.throw(failure)
            except RuntimeError as error: same = error is failure
            try: next(gen)
            except StopIteration: closed = True
            return first, same, closed
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_unpacking_errors_match(self):
        def observe(engine, pair):
            class Template(engine.DocxTemplate):
                def get_headers_footers(self, uri): return iter([iter(pair)])
            gen = Template(None).build_headers_footers_xml({}, 'header')
            try: next(gen)
            except ValueError as error: return str(error)
            raise AssertionError('expected unpacking error')
        for pair in ([], ['only'], ['a', 'b', 'c']):
            with self.subTest(pair=pair):
                self.assertEqual(observe(docxtpl_native, pair), observe(docxtpl, pair))

    def test_stop_iteration_in_callback_is_not_swallowed(self):
        def observe(engine):
            failure = StopIteration('callback stop')
            class Template(engine.DocxTemplate):
                def get_headers_footers(self, uri): yield 'key', object()
                def get_part_xml(self, part): raise failure
            gen = Template(None).build_headers_footers_xml({}, 'header')
            try: next(gen)
            except RuntimeError as error:
                return str(error), error.__cause__ is failure
            raise AssertionError('expected RuntimeError')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_iterator_cycle_is_collectable(self):
        from docxtpl_native import _native
        class Items:
            def __iter__(self): return self
            def __next__(self): raise StopIteration
        class Template(docxtpl_native.DocxTemplate):
            def __init__(self): super().__init__(None); self.items = Items()
            def get_headers_footers(self, uri): return self.items
        tpl = Template(); items = tpl.items; probe = weakref.ref(items)
        iterator = _native.build_header_footer_iter(tpl, {}, 'header', None)
        items.parent = iterator
        del tpl, items, iterator
        gc.collect()
        self.assertIsNone(probe())
