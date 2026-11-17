"""Scope isolation using the existing VM, including imports and shared objects."""
import unittest
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.include_scope_probes import CASES, FRAGMENT, execute


def block_executors():
    return forbid('jinja2.Template.render', 'jinja2.Template.render_async',
                  'jinja2.Template.generate_async', message='Python executor called')


class IncludeAssignmentScopeTests(unittest.TestCase):
    def test_complete_documents_in_sync_and_async(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for asynchronous in (False, True):
            for name, (source, expected) in CASES.items():
                with self.subTest(case=name, asynchronous=asynchronous):
                    with block_executors():
                        native = execute(docxtpl_native, source=source, asynchronous=asynchronous)
                    self.assertEqual(native.observation, {'text': expected})
                    self.assertTrue(native.outputs)

    def test_import_exports_and_include_controls(self):
        cases = [
            ("{% import 'fragment' as m %}{{ m.x }}|{{ x }}", 'inner|outer'),
            ("{% from 'fragment' import x as imported %}{{ imported }}|{{ x }}", 'inner|outer'),
            ("{% include 'fragment' without context %}|{{ x }}", 'inner|outer'),
            ("{% include ['missing', 'fragment'] %}|{{ x }}", 'inner|outer'),
            ("{% include 'missing' ignore missing %}{{ x }}", 'outer'),
            ("{% include 'nested' %}|{{ x }}", 'inner|nested|outer'),
            ("{% include 'mutate' %}{{ ns.value }}|{{ x }}", 'changed|outer'),
            ("{% include 'define' %}{{ local|default('missing') }}|{{ x }}", 'missing|outer'),
        ]
        templates = {
            'fragment': FRAGMENT,
            'nested': "{% set x='nested' %}{% include 'fragment' %}|{{ x }}",
            'mutate': "{% set ns.value='changed' %}{% set x='inner' %}",
            'define': "{% macro local() %}local{% endmacro %}",
        }
        for asynchronous in (False, True):
            for source, expected in cases:
                def run(native):
                    environment = jinja2.Environment(
                        loader=jinja2.DictLoader(templates), enable_async=asynchronous)
                    values = {'x': 'outer', 'ns': jinja2.utils.Namespace(value='initial')}
                    if native:
                        with block_executors():
                            return docxtpl_native._extension().PythonRenderSession(
                                BridgeConfig(environment, False), values).render('<string>', source)
                    return environment.from_string(source).render(values)
                with self.subTest(source=source, asynchronous=asynchronous):
                    self.assertEqual(run(False), expected)
                    self.assertEqual(run(True), expected)
