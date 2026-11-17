"""Activation identity, scope, publication and exception boundaries."""
import sys
import asyncio
import inspect
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import docxtpl
import docxtpl_native
import jinja2
from jinja2.nodes import EvalContext
from jinja2.runtime import Context, Macro
import jinja2.runtime as runtime
from docxtpl_native.bridge import BridgeConfig
from paritylab.compare import compare_docx
from paritylab.context_creation_probes import PROBES as CREATION
from paritylab.context_lifecycle_probes import PROBES as IDENTITY
from paritylab.dynamic_keyword_probes import PROBES as KEYWORDS


@contextmanager
def native_algorithms():
    forbidden = {function.__code__ for function in (
        Context.__init__, EvalContext.__init__, Context.call, Context.derived,
        Context.get_all, runtime.new_context, EvalContext.save, EvalContext.revert,
        Macro.__init__, Macro.__call__, Macro._invoke, Macro._async_invoke)}
    previous = sys.getprofile()
    def profile(frame, event, _):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('standard Python runtime algorithm executed: ' + frame.f_code.co_name)
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def render(native, source, values, environment):
    if not native:
        return environment.from_string(source).render(values)
    with native_algorithms():
        return docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), values).render('case', source)


class NativeContextLifecycleTests(unittest.TestCase):
    def test_unsupported_escaped_macros_do_not_execute_a_different_body(self):
        # Retained macro execution remains an explicit unsupported partition.
        # A sibling definition must not reuse the escaped macro's VM table slot.
        saved = []
        def keep(value): saved.append(value); return ''
        def use(): return saved[0]()
        source = ('{% macro outer() %}{% macro inner() %}inner{% endmacro %}'
                  '{{ keep(inner) }}{% endmacro %}{{ outer() }}'
                  '{% macro sibling() %}wrong-body{% endmacro %}{{ use() }}')
        environment = jinja2.Environment()
        self.assertEqual(environment.from_string(source).render(keep=keep, use=use), 'inner')
        saved.clear()
        session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), {'keep': keep, 'use': use})
        with self.assertRaisesRegex(docxtpl_native.UnsupportedFeature, 'defining VM scope'):
            session.render('case', source)
        with self.assertRaises(docxtpl_native.UnsupportedFeature):
            saved[0]()

    def test_include_preserves_native_loop_without_eager_lookahead(self):
        for asynchronous in (False, True):
            def observe(native):
                trace = []
                def values():
                    for value in (1, 2, 3):
                        trace.append(('next', value))
                        yield value
                def body(value): trace.append(('body', value)); return value
                environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader({
                    'child': '{{ body(x) }}:{{ loop.index }}:{{ loop.last }};'}))
                source = '{% for x in values %}{{ loop.index }}{% include "child" %}{% endfor %}'
                return render(native, source, {'values': values(), 'body': body}, environment), trace
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True), observe(False))

    def test_constructor_mutations_are_visible_to_root_and_include(self):
        def observe(native):
            trace = []
            class ConfiguredContext(Context):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    trace.append(self.name)
                    self.vars['value'] = 'child' if self.name else 'root'
            environment = jinja2.Environment(loader=jinja2.DictLoader({'child': '{{ value }}'}))
            environment.context_class = ConfiguredContext
            source = '{{ value }}{% include "child" %}{{ value }}'
            if native:
                output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False),
                    {'value': 'input'}).render('case', source)
            else: output = environment.from_string(source).render(value='input')
            return output, trace
        self.assertEqual(observe(False), ('rootchildroot', [None, 'child']))
        self.assertEqual(observe(True), observe(False))

    def test_macro_binding_errors_defaults_and_caller(self):
        sources = [
            '{% macro m(a,b=a,c=b) %}{{ a }}|{{ b }}|{{ c }}{% endmacro %}{{ m("X") }}',
            '{% macro m(a) %}{{ a }}|{{ kwargs.a }}{% endmacro %}{{ m(1,a=2) }}',
            '{% macro m(a) %}{{ a }}{% endmacro %}{{ m(1,2) }}',
            '{% macro m(a) %}{{ a }}{% endmacro %}{{ m(a=1,other=2) }}',
            '{% macro m() %}[{{ caller() }}]{% endmacro %}{% call m() %}X{% endcall %}',
            '{% macro m(caller=None) %}[{{ caller() }}]{% endmacro %}{% call m() %}X{% endcall %}',
        ]
        for asynchronous in (False, True):
            for source in sources:
                def observe(native):
                    try: return 'ok', render(native, source, {}, jinja2.Environment(enable_async=asynchronous))
                    except Exception as error: return type(error).__name__, str(error)
                with self.subTest(asynchronous=asynchronous, source=source):
                    self.assertEqual(observe(True), observe(False))

    def test_macro_identity_constructor_and_async_call_boundary(self):
        def observe(native, asynchronous):
            trace, calls = [], []
            class ObservedContext(Context):
                def call(self, callback, *args, **kwargs):
                    if isinstance(callback, Macro):
                        calls.append(callback)
                        trace.append(('macro', callback is calls[0]))
                    result = super().call(callback, *args, **kwargs)
                    if isinstance(callback, Macro): trace.append(('awaitable', inspect.isawaitable(result)))
                    return result
            async def delayed():
                trace.append('start')
                await asyncio.sleep(0)
                trace.append('end')
                return 'X'
            def immediate(): trace.append('body'); return 'X'
            environment = jinja2.Environment(enable_async=asynchronous)
            environment.context_class = ObservedContext
            source = '{% macro m() %}{{ body() }}{% endmacro %}{{ m() }}{{ m() }}'
            if native:
                # The user override explicitly invokes its superclass. Its
                # Python calls are user code, not the standard dispatch gate.
                output = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False),
                    {'body': delayed if asynchronous else immediate}).render('case', source)
            else: output = environment.from_string(source).render(body=delayed if asynchronous else immediate)
            return output, trace
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True, asynchronous), observe(False, asynchronous))

    def test_five_mapped_gaps_complete_documents(self):
        probes = {**CREATION, **IDENTITY, 'dynamic_keywords_macro': KEYWORDS['dynamic_keywords_macro']}
        for name, probe in probes.items():
            with self.subTest(name=name):
                reference = probe.execute(docxtpl)
                self.assertEqual(reference.observation, probe.expected)
                actual = probe.execute(docxtpl_native)
                self.assertEqual(actual.observation, reference.observation)
                self.assertEqual(set(actual.outputs), set(reference.outputs))
                for filename, blob in actual.outputs.items():
                    comparison = compare_docx(reference.outputs[filename], blob)
                    self.assertTrue(comparison['equivalent'], comparison)

    def test_scope_identity_and_publication(self):
        sources = {
            'root': '{{ probe("a") }}{{ probe("b") }}',
            'set': '{% set x="local" %}{{ probe("a") }}{{ probe("b") }}',
            'loop': '{% for x in values %}{{ probe("a") }}{{ probe("b") }}{% endfor %}{{ probe("after") }}',
            'loop_set': '{% for x in values %}{% set local=x %}{{ probe("a") }}{{ probe("b") }}{% endfor %}{{ probe("after") }}',
            'macro': '{% macro m(x) %}{% set local=x %}{{ probe("macro") }}{% endmacro %}{{ probe("before") }}{{ m(3) }}{{ probe("after") }}',
            'with': '{% with x="inner" %}{{ probe("inner") }}{% endwith %}{{ probe("after") }}',
            'include': '{{ probe("before") }}{% include "fragment" %}{% include "fragment" %}{{ probe("after") }}',
            'block': '{% block b %}{% set local="block" %}{{ probe("a") }}{{ probe("b") }}{% endblock %}{{ probe("after") }}',
            'escape': '{{ probe("before") }}{% autoescape enabled %}{{ probe("inside") }}{% endautoescape %}{{ probe("after") }}',
            'escape_set': '{% autoescape enabled %}{% set local="inner" %}{{ probe("inside") }}{% endautoescape %}{{ probe("after") }}{{ local|default("missing") }}',
        }
        def observe(native, source, asynchronous):
            contexts, evaluations, trace = [], [], []
            def index(items, value):
                for i, item in enumerate(items):
                    if item is value: return i
                items.append(value)
                return len(items) - 1
            @jinja2.pass_context
            def probe(context, label):
                trace.append((label, index(contexts, context), index(evaluations, context.eval_ctx),
                    context.name, context.eval_ctx.autoescape,
                    tuple(context.get(key, 'missing') for key in ('x', 'local')),
                    {k: v for k, v in context.vars.items() if k in ('x', 'local')},
                    sorted(k for k in context.exported_vars if k in ('x', 'local'))))
                return 'X'
            environment = jinja2.Environment(enable_async=asynchronous, loader=jinja2.DictLoader(
                {'fragment': '{{ probe("include-a") }}{{ probe("include-b") }}'}))
            output = render(native, source, {'probe': probe, 'values': [1, 2], 'x': 'outer', 'enabled': True}, environment)
            return output, trace
        for asynchronous in (False, True):
            for name, source in sources.items():
                with self.subTest(scope=name, asynchronous=asynchronous):
                    self.assertEqual(observe(True, source, asynchronous), observe(False, source, asynchronous))

    def test_retained_context_observes_assignments_before_failures(self):
        for source in (
            '{{ retain() }}{% set a,b=values %}{{ fail() }}',
            '{{ retain() }}{% set a,(b,c)=values %}',
            '{{ retain() }}{% set a %}local{% endset %}{{ fail() }}',
        ):
            def observe(native):
                contexts = []
                failure = ValueError('later failure')
                @jinja2.pass_context
                def retain(context): contexts.append(context); return ''
                def fail(): raise failure
                try:
                    render(native, source, {'retain': retain, 'fail': fail, 'values': [1, [2]]}, jinja2.Environment())
                except Exception as error:
                    result = type(error).__name__, error is failure
                else: result = 'success', False
                context = contexts[0]
                return result, dict(context.vars), sorted(context.exported_vars)
            with self.subTest(source=source):
                self.assertEqual(observe(True), observe(False))

    def test_filters_finalizers_and_calls_share_evaluation(self):
        for asynchronous in (False, True):
            def observe(native):
                contexts, evaluations, trace = [], [], []
                def record(context, label):
                    contexts.append(context)
                    evaluations.append(context.eval_ctx)
                    trace.append((label, context is contexts[0], context.eval_ctx is evaluations[0], context.eval_ctx.autoescape))
                    return 'X'
                @jinja2.pass_context
                def callback(context): return record(context, 'call')
                @jinja2.pass_context
                def filter_(context, value): return record(context, 'filter')
                @jinja2.pass_context
                def finalize(context, value): return record(context, 'finalizer')
                env = jinja2.Environment(enable_async=asynchronous, finalize=finalize)
                env.filters['custom'] = filter_
                result = render(native, '{{ callback() }}{% autoescape true %}{{ value|custom }}{% endautoescape %}{{ callback() }}',
                    {'callback': callback, 'value': 'x'}, env)
                return result, trace
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(observe(True), observe(False))

    def test_raw_autoescape_value_and_revert_identity(self):
        def observe(native):
            marker = object()
            retained, trace = [], []
            @jinja2.pass_eval_context
            def probe(context, label):
                retained.append(context)
                trace.append((label, context is retained[0], context.autoescape is marker, hasattr(context, 'custom')))
                if label == 'inside': context.custom = 42
                return ''
            output = render(native, '{{ probe("before") }}{% autoescape marker %}{{ probe("inside") }}{% endautoescape %}{{ probe("after") }}',
                {'probe': probe, 'marker': marker}, jinja2.Environment())
            return output, trace, vars(retained[0]).get('custom')
        self.assertEqual(observe(True), observe(False))


if __name__ == '__main__':
    unittest.main()
