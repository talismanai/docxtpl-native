"""Native concatenation algorithms with caller-owned Python values."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch
import jinja2
import docxtpl_native
from docxtpl_native.bridge import BridgeConfig
from markupsafe import Markup
from native_audit import forbid
from paritylab.protocol_base import generate, observed
from paritylab.concat_probes import make_case

class NativeConcatAlgorithmTests(unittest.TestCase):
    def observe(self, native, autoescape, shape, block_helpers=False):
        env, source, context, trace, failure = make_case(shape, autoescape)
        try:
            if native:
                with forbid('jinja2.runtime.str_join','jinja2.runtime.markup_join',
                            message='Python concat algorithm called') if block_helpers else ExitStack():
                    result=docxtpl_native._extension().PythonRenderSession(BridgeConfig(env,False),context).render('case',source)
            else:result=env.from_string(source).render(context)
            return ['ok',result,trace]
        except Exception as error:
            return ['error',type(error).__name__,str(error),error is failure,trace]

    def test_protocols_and_native_execution(self):
        for shape in ('plain','markup','html','error','surrogate','subclass'):
            for autoescape in (False,True):
                with self.subTest(shape=shape,autoescape=autoescape):
                    self.assertEqual(self.observe(True,autoescape,shape,True),self.observe(False,autoescape,shape))

    def test_markup_constructor_order_and_types(self):
        def run(native):
            import jinja2.runtime
            original = Markup.__new__
            values = ['<&', Markup('<safe>'), '\"\'']
            trace = []
            def construct(cls, value='', *args, **kwargs):
                trace.append([type(value).__name__, repr(value)])
                return original(cls, value, *args, **kwargs)
            with patch.object(Markup, '__new__', staticmethod(construct)):
                value = (docxtpl_native._extension().python_concat(values, True) if native
                         else jinja2.runtime.markup_join(values))
            return type(value), repr(value), trace
        self.assertEqual(run(True), run(False))

    def test_custom_markup_escape_remains_a_user_callback(self):
        def run(native):
            import jinja2.runtime
            trace = []
            class CustomMarkup(Markup):
                @classmethod
                def escape(cls, value):
                    trace.append([type(value).__name__, repr(value)])
                    return '[' + str(value) + ']'
            values = ['first', Markup('safe'), 'last']
            with patch.object(jinja2.runtime, 'Markup', CustomMarkup):
                value = (docxtpl_native._extension().python_concat(values, True) if native
                         else jinja2.runtime.markup_join(values))
            return type(value) is CustomMarkup, str(value), trace
        self.assertEqual(run(True), run(False))

    def test_temporary_string_survives_later_conversions(self):
        def run(native):
            import jinja2.runtime
            import warnings
            trace = []
            class Temporary(str):
                def __html__(self):
                    trace.append('html')
                    return 'safe'
                def __del__(self):
                    trace.append('release')
            class First:
                def __str__(self):
                    trace.append('first')
                    return Temporary('temporary')
            class Last:
                def __str__(self):
                    trace.append('last')
                    return 'last'
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', DeprecationWarning)
                value = (docxtpl_native._extension().python_concat([First(), Last()], True) if native
                         else jinja2.runtime.markup_join([First(), Last()]))
            return str(value), trace
        self.assertEqual(run(True), run(False))

    def test_complete_documents_without_python_concat_or_renderer(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for autoescape, expected in ((False, 'Café β42'), (True, 'Café <&42')):
            with forbid('jinja2.runtime.str_join', 'jinja2.runtime.markup_join', 'jinja2.Template.render',
                        message='Python executor called'):
                native = concat_document(docxtpl_native, autoescape)
            with self.subTest(autoescape=autoescape):
                self.assertEqual(observed(native).observation['text'], expected)


def concat_document(engine, autoescape):
    """A complete document concatenating text, Markup and an integer."""
    context = {'a': 'Café <' if autoescape else 'Café ',
               'b': Markup('&amp;') if autoescape else Markup('β'), 'c': 42}
    return generate(engine, '{{ a ~ b ~ c }}', context, jinja2.Environment(autoescape=autoescape))
