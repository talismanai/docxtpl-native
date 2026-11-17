"""Differential gates for native formatting and caller-owned finalizers."""
import unittest
import docxtpl_native
import jinja2
from markupsafe import Markup
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.protocol_base import generate, observed


def block_formatting():
    return forbid('docxtpl_native.bridge.BridgeConfig.format_value', 'docxtpl_native.bridge.escape',
                  'markupsafe.escape', 'jinja2.Template.render', message='Python formatting algorithm called')


def make_value(shape, trace, failure):
    class Text(str):
        def __str__(self):
            trace.append('text-str')
            return '<text>'
    class Object:
        def __str__(self):
            trace.append('object-str')
            if shape == 'str_error':
                raise failure
            return '<object>'
    class Html(Object):
        def __getattribute__(self, name):
            if name == '__html__':
                trace.append('lookup-html')
            return super().__getattribute__(name)
        def __html__(self):
            trace.append('html')
            if shape == 'html_error':
                raise failure
            return 'safe &amp; value'
    return {'str': '<&>', 'int': 2**100, 'float': -0.0, 'bool': True,
            'bytes': b'<bytes>', 'none': None, 'subclass': Text('original'),
            'object': Object(), 'str_error': Object(), 'html': Html(),
            'html_error': Html(), 'markup': Markup('safe &amp; value')}[shape]


def observe(native, shape, autoescape, kind=None):
    trace = []
    failure = ValueError('format failed')
    value = make_value(shape, trace, failure)
    environment = jinja2.Environment(autoescape=autoescape)
    if kind:
        def finalize(*args):
            if kind == 'plain':
                trace.append(['finalize', args[0] is value])
            elif kind == 'environment':
                trace.append(['finalize', args[0] is environment, args[1] is value])
            elif kind == 'context':
                trace.append(['finalize', args[0].environment is environment,
                              args[0]['local'], args[0].eval_ctx.autoescape,
                              args[1] is value])
            else:
                trace.append(['finalize', args[0].environment is environment,
                              args[0].autoescape, args[1] is value])
            return args[-1]
        decorator = {'plain': lambda x:x, 'context': jinja2.pass_context,
                     'environment': jinja2.pass_environment, 'eval_context': jinja2.pass_eval_context}[kind]
        environment.finalize = decorator(finalize)
    source = "{% set local='L' %}{{ value }}"
    try:
        if native:
            with block_formatting():
                output = docxtpl_native._extension().PythonRenderSession(
                    BridgeConfig(environment, False), {'value': value}).render('case', source)
        else:
            output = environment.from_string(source).render({'value': value})
        return ['ok', output, trace]
    except Exception as error:
        return ['error', type(error).__module__, type(error).__name__, str(error), error is failure, trace]


class NativeFormatAlgorithmTests(unittest.TestCase):
    def test_types_protocols_and_errors_without_python_formatter(self):
        for shape in ('str', 'int', 'float', 'bool', 'bytes', 'none', 'subclass',
                      'object', 'str_error', 'html', 'html_error', 'markup'):
            for autoescape in (False, True):
                with self.subTest(shape=shape, autoescape=autoescape):
                    self.assertEqual(observe(True, shape, autoescape), observe(False, shape, autoescape))

    def test_finalizer_injection_and_value_identity(self):
        for kind in ('plain', 'context', 'environment', 'eval_context'):
            for shape in ('int', 'html', 'html_error', 'markup'):
                for autoescape in (False, True):
                    with self.subTest(kind=kind, shape=shape, autoescape=autoescape):
                        self.assertEqual(observe(True, shape, autoescape, kind),
                                         observe(False, shape, autoescape, kind))

    def test_existing_finalizer_mutation_and_truthiness_probes(self):
        from paritylab.finalizer_probes import PROBES
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for name, probe in PROBES.items():
            with block_formatting():
                actual = probe.execute(docxtpl_native)
            with self.subTest(name=name):
                self.assertEqual(actual.observation, probe.expected)
                self.assertEqual(bool(actual.outputs), not probe.assertion_only)

    def test_finalizer_exception_preserves_identity_and_stops_later_values(self):
        def run(native, autoescape):
            trace = []
            failure = ValueError('finalizer failure')
            @jinja2.pass_context
            def finalize(context, value):
                trace.append(['finalize', value, context.eval_ctx.autoescape])
                raise failure
            def later():
                trace.append('later')
                return 'unexpected'
            environment = jinja2.Environment(finalize=finalize, autoescape=autoescape)
            source = '{{ value }}{{ later() }}'
            context = {'value': 42, 'later': later}
            try:
                if native:
                    with block_formatting():
                        docxtpl_native._extension().PythonRenderSession(
                            BridgeConfig(environment, False), context).render('case', source)
                else:
                    environment.from_string(source).render(context)
            except Exception as error:
                return [type(error).__name__, str(error), error is failure, trace]
            raise AssertionError('missing finalizer error')
        for autoescape in (False, True):
            with self.subTest(autoescape=autoescape):
                expected = run(False, autoescape)
                self.assertEqual(expected, ['ValueError', 'finalizer failure', True,
                                           [['finalize', 42, autoescape]]])
                self.assertEqual(run(True, autoescape), expected)

    def test_complete_documents_without_python_formatter(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for autoescape in (False, True):
            with block_formatting():
                actual = format_document(docxtpl_native, autoescape)
            with self.subTest(autoescape=autoescape):
                self.assertEqual(observed(actual).observation['text'],
                                 '1267650600228229401496703205376|True|None|safe')


def format_document(engine, autoescape):
    """A complete document formatting an integer, a bool, None and Markup."""
    source = '{{ value }}|{{ flag }}|{{ empty }}|{{ safe }}'
    context = {'value': 2**100, 'flag': True, 'empty': None, 'safe': Markup('safe')}
    return generate(engine, source, context, jinja2.Environment(autoescape=autoescape))


if __name__ == '__main__':
    unittest.main()
