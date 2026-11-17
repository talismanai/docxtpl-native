"""Property rendering preserves per-property order and partial commits."""
import unittest
import jinja2
import docxtpl
import docxtpl_native

NAMES = ('author', 'comments', 'identifier', 'language', 'subject', 'title')


class NativeRenderPropertiesTests(unittest.TestCase):
    def test_order_setters_and_environment_filter(self):
        def observe(engine):
            trace = []
            class Props:
                def __init__(self): object.__setattr__(self, 'values', {name: '{{ value|mark }}' for name in NAMES})
                def __getattribute__(self, name):
                    if name in NAMES: trace.append(('get', name)); return object.__getattribute__(self, 'values')[name]
                    return object.__getattribute__(self, name)
                def __setattr__(self, name, value):
                    if name in NAMES: trace.append(('set', name, value)); self.values[name] = value
                    else: object.__setattr__(self, name, value)
            props = Props()
            class Doc:
                @property
                def core_properties(self): trace.append('properties'); return props
            env = jinja2.Environment()
            def mark(value): trace.append(('filter', value)); return value.upper()
            env.filters['mark'] = mark
            tpl = engine.DocxTemplate(None); tpl.docx = Doc()
            result = tpl.render_properties({'value':'hello'}, env)
            return result, dict(props.values), trace
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))

    def test_error_keeps_earlier_property_updates_and_original_exception(self):
        def observe(engine):
            failure = RuntimeError('filter failed'); trace = []
            class Props:
                author = '{{ value }}'; comments = '{{ value }}'; identifier = '{{ fail() }}'
                language = 'keep'; subject = 'keep'; title = 'keep'
            class Doc: core_properties = Props()
            def fail(): trace.append('fail'); raise failure
            tpl = engine.DocxTemplate(None); tpl.docx = Doc()
            try: tpl.render_properties({'value':'X', 'fail':fail})
            except RuntimeError as error:
                p = tpl.docx.core_properties
                return error is failure, [getattr(p, name) for name in NAMES], trace
            raise AssertionError('expected callback failure')
        self.assertEqual(observe(docxtpl_native), observe(docxtpl))
