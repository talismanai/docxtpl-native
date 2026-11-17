"""Callback dispatch observes live policies and Python keyword protocols."""
import sys
import subprocess
import textwrap
import types
import unittest
from unittest.mock import patch

import jinja2
import jinja2.runtime as runtime
import jinja2.utils as utils
from jinja2.sandbox import SandboxedEnvironment
import docxtpl_native
import docxtpl
from paritylab.call_policy_probes import PROBES
from paritylab.compare import compare_docx
from test_native_sandbox_call import native_sandbox_only
from test_native_sandbox_call import call


def observe(native, variant):
    environment = SandboxedEnvironment()
    context = runtime.Context(environment, {}, 'case', {})
    trace = []
    def callback(*args, **kwargs):
        return [len(args), bool(args) and args[0] is environment]
    def invoke(kwargs=None):
        return call(native, environment, context, callback, kwargs=kwargs)
    if variant == 'runtime_alias':
        class Policy:
            context = object()
            eval_context = object()
            environment = object()
            @classmethod
            def from_obj(cls, obj):
                trace.append('callback' if obj is callback else '__call__')
                return cls.environment if obj is callback else None
        with patch.object(runtime, '_PassArg', Policy):
            result = invoke()
    elif variant == 'from_obj_override':
        def policy(cls, obj):
            trace.append('callback' if obj is callback else '__call__')
            return cls.environment if obj is callback else None
        with patch.object(utils._PassArg, 'from_obj', classmethod(policy)):
            result = invoke()
    elif variant in ('runtime_hasattr', 'utils_hasattr'):
        def check(obj, name):
            trace.append(name)
            return hasattr(obj, name)
        module = runtime if variant == 'runtime_hasattr' else utils
        with patch.object(module, 'hasattr', check, create=True):
            result = invoke()
    else:
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                trace.append(['key', str(other)])
                return str.__eq__(self, other)
        if variant == 'derived_lookup_order':
            callback.jinja_pass_arg = utils._PassArg.context
            def derived(self, locals):
                trace.append('derived')
                return self
            context.derived = types.MethodType(derived, context)
        result = invoke({Key('_loop_vars'): {'local': 1}})
    return {'result': result, 'trace': trace}


class NativeCallPolicyTests(unittest.TestCase):
    def test_complete_document_live_policy_sync_and_async(self):
        for name, probe in PROBES.items():
            with self.subTest(name=name):
                expected = probe.execute(docxtpl)
                self.assertEqual(expected.observation, probe.expected)
                with native_sandbox_only():
                    actual = probe.execute(docxtpl_native)
                self.assertEqual(actual.observation, expected.observation)
                self.assertEqual(actual.outputs.keys(), expected.outputs.keys())
                for filename, blob in actual.outputs.items():
                    difference = compare_docx(expected.outputs[filename], blob)
                    self.assertTrue(difference['equivalent'], difference)

    def check(self, variant):
        self.assertEqual(observe(True, variant), observe(False, variant))

    def test_runtime_pass_arg_alias(self):
        self.check('runtime_alias')

    def test_from_obj_override(self):
        self.check('from_obj_override')

    def test_runtime_hasattr_global(self):
        self.check('runtime_hasattr')

    def test_from_obj_hasattr_global(self):
        self.check('utils_hasattr')

    def test_keyword_binding_and_reserved_key_pop(self):
        self.check('reserved_key_pop')

    def test_derived_lookup_order(self):
        self.check('derived_lookup_order')

    def test_duplicate_parameters_and_nonstring_keys_precede_callback(self):
        for key in ('_SandboxedEnvironment__self', '_SandboxedEnvironment__context',
                    '_SandboxedEnvironment__obj', '_Context__self', '_Context__obj', 42):
            def run(native):
                trace = []
                class Callback:
                    @property
                    def unsafe_callable(self):
                        trace.append('policy')
                        return False
                    def __call__(self, **kwargs):
                        trace.append('called')
                        return 'incorrect success'
                environment = SandboxedEnvironment()
                context = runtime.Context(environment, {}, 'case', {})
                try:
                    value = call(native, environment, context, Callback(), kwargs={key: 1})
                    return value, trace
                except Exception as error:
                    return type(error).__name__, str(error), trace
            with self.subTest(key=key):
                expected = run(False)
                self.assertEqual(expected[0], 'TypeError')
                self.assertEqual(run(True), expected)

    def test_standard_from_obj_runs_natively(self):
        forbidden = {runtime.Context.call.__code__, utils._PassArg.from_obj.__func__.__code__,
                     SandboxedEnvironment.call.__code__, SandboxedEnvironment.is_safe_callable.__code__}
        environment = SandboxedEnvironment()
        context = runtime.Context(environment, {}, 'case', {})
        marker = object()
        @jinja2.pass_environment
        def callback(value):
            self.assertIs(value, environment)
            return marker
        def profile(frame, event, argument):
            if event == 'call' and frame.f_code in forbidden:
                raise AssertionError('standard Python callback dispatch executed')
        previous = sys.getprofile()
        sys.setprofile(profile)
        try:
            self.assertIs(docxtpl_native._extension().python_sandbox_call(
                environment, context, callback, (), {}), marker)
        finally:
            sys.setprofile(previous)

    def test_method_is_resolved_before_call_attribute(self):
        def run(native):
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            trace = []
            class Policy:
                context = object()
                eval_context = object()
                environment = object()
                def __init__(self, label): self.label = label
                @property
                def from_obj(self):
                    trace.append(self.label + ':resolve')
                    def choose(obj):
                        trace.append(self.label + ':invoke')
                        return self.environment if obj is callback else None
                    return choose
            old, new = Policy('old'), Policy('new')
            class Callback:
                reads = 0
                def __getattribute__(self, name):
                    if name == '__call__':
                        self.reads += 1
                        trace.append('attribute:' + str(self.reads))
                        if self.reads == 2:
                            runtime._PassArg = new
                    return object.__getattribute__(self, name)
                def __call__(self, value):
                    return value is environment
            callback = Callback()
            with patch.object(runtime, '_PassArg', old):
                value = call(native, environment, context, callback)
            return value, trace
        expected = run(False)
        self.assertTrue(expected[0])
        self.assertEqual(run(True), expected)

    def test_each_marker_resolves_the_current_runtime_alias(self):
        def run(native):
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            marker = object()
            replacement = types.SimpleNamespace(context=object(), eval_context=object(), environment=marker)
            class Policy:
                eval_context = object()
                environment = object()
                @property
                def context(self):
                    runtime._PassArg = replacement
                    return object()
                def from_obj(self, obj):
                    return marker if obj is callback else None
            def callback(*args):
                return len(args) == 1 and args[0] is environment
            with patch.object(runtime, '_PassArg', Policy()):
                return call(native, environment, context, callback)
        self.assertTrue(run(False))
        self.assertTrue(run(True))

    def test_code_changed_by_argument_descriptor_is_observed(self):
        function = utils._PassArg.from_obj.__func__
        original = function.__code__
        def replacement(cls, obj): return None
        def run(native):
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            class Callback:
                reads = 0
                def __getattribute__(self, name):
                    if name == '__call__':
                        self.reads += 1
                        if self.reads == 2:
                            function.__code__ = replacement.__code__
                    return object.__getattribute__(self, name)
                @jinja2.pass_environment
                def __call__(self, *args): return len(args)
            try:
                return call(native, environment, context, Callback())
            finally:
                function.__code__ = original
        self.assertEqual(run(False), 0)
        self.assertEqual(run(True), 0)

    def test_from_obj_errors_keep_identity(self):
        for kind in (ValueError, StopIteration, KeyboardInterrupt):
            for native in (False, True):
                failure = kind('policy failed')
                def policy(cls, obj): raise failure
                environment = SandboxedEnvironment()
                context = runtime.Context(environment, {}, 'case', {})
                with self.subTest(kind=kind, native=native), patch.object(utils._PassArg, 'from_obj', classmethod(policy)):
                    with self.assertRaises(kind) as caught:
                        call(native, environment, context, lambda: 'unexpected call')
                    self.assertIs(caught.exception, failure)

    def test_module_removal_does_not_reimport_utils(self):
        environment = SandboxedEnvironment()
        context = runtime.Context(environment, {}, 'case', {})
        @jinja2.pass_environment
        def callback(value): return value is environment
        module = sys.modules.pop('jinja2.utils')
        try:
            self.assertTrue(call(True, environment, context, callback))
            self.assertNotIn('jinja2.utils', sys.modules)
        finally:
            sys.modules['jinja2.utils'] = module
            jinja2.utils = module

    def test_keyword_formatting_preserves_name_snapshot_and_unicode(self):
        function = runtime.Context.call
        original_name = function.__qualname__
        def run(native):
            class Key(str):
                __hash__ = str.__hash__
                def __str__(self):
                    function.__qualname__ = 'changed during formatting'
                    return 'keyword\ud800'
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            function.__qualname__ = 'Context.call\udfff'
            try:
                call(native, environment, context, lambda **kwargs: None,
                     kwargs={Key('_Context__self'): 1})
            except TypeError as error:
                return str(error)
            finally:
                function.__qualname__ = original_name
        expected = run(False)
        self.assertIn('Context.call\udfff', expected)
        self.assertIn('keyword\ud800', expected)
        self.assertEqual(run(True), expected)

    def test_keyword_equality_error_precedes_sandbox_policy(self):
        for native in (False, True):
            failure = ValueError('keyword equality')
            trace = []
            class Key(str):
                __hash__ = str.__hash__
                def __eq__(self, other):
                    trace.append(str(other))
                    raise failure
            class Callback:
                @property
                def unsafe_callable(self):
                    trace.append('unexpected policy')
                    return False
                def __call__(self, **kwargs): pass
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            with self.subTest(native=native):
                with self.assertRaises(ValueError) as caught:
                    call(native, environment, context, Callback(), kwargs={Key('key'): 1})
                self.assertIs(caught.exception, failure)
                self.assertEqual(trace, ['_SandboxedEnvironment__self'])

    def test_keyword_values_are_snapshotted_before_comparison(self):
        def run(native):
            trace = []
            values = {}
            class Key(str):
                __hash__ = str.__hash__
                def __eq__(self, other):
                    trace.append(str(other))
                    values.clear()
                    return str.__eq__(self, other)
            values.update({Key('first'): 1, 'last': 2})
            environment = SandboxedEnvironment()
            context = runtime.Context(environment, {}, 'case', {})
            result = call(native, environment, context,
                          lambda **kwargs: [(str(key), value, type(key) is Key) for key, value in kwargs.items()],
                          kwargs=values)
            return result, trace, values
        self.assertEqual(run(True), run(False))

    def test_override_before_binding_import_remains_a_callback(self):
        code = textwrap.dedent('''
            import sys, jinja2
            from jinja2.sandbox import SandboxedEnvironment
            trace = []
            def policy(cls, obj):
                trace.append('policy')
                return None
            jinja2.utils._PassArg.from_obj = classmethod(policy)
            import docxtpl_native
            assert 'docxtpl_native.bridge' not in sys.modules
            environment = SandboxedEnvironment()
            context = jinja2.runtime.Context(environment, {}, 'case', {})
            assert docxtpl_native._extension().python_sandbox_call(
                environment, context, lambda: 'done', (), {}) == 'done'
            assert trace == ['policy', 'policy'], trace
        ''')
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)


if __name__ == '__main__':
    unittest.main()
