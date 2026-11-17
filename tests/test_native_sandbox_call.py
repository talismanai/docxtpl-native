"""Sandbox call decisions in Rust preserve callbacks and foreign protocols."""
import sys
import subprocess
import textwrap
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

import docxtpl_native
import jinja2
import jinja2.sandbox as sandbox
from jinja2.runtime import Context
from native_audit import forbid
from paritylab.protocol_base import generate, observed


@contextmanager
def native_sandbox_only():
    forbidden = {sandbox.SandboxedEnvironment.call.__code__,
                 sandbox.SandboxedEnvironment.is_safe_callable.__code__, Context.call.__code__}
    previous = sys.getprofile()
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code in forbidden:
            raise AssertionError('Python sandbox/context call algorithm executed')
    sys.setprofile(profile)
    try:
        yield
    finally:
        sys.setprofile(previous)


def sandbox_document(engine, asynchronous):
    """A complete document whose callback is dispatched through the sandbox."""
    environment = sandbox.SandboxedEnvironment(enable_async=asynchronous)
    @jinja2.pass_environment
    def callback(value, number):
        if value is not environment:
            raise AssertionError('callback received another environment')
        return str(number + 1)
    return generate(engine, '{{ callback(value) }}', {'callback': callback, 'value': 41}, environment)


def call(native, environment, context, callback, args=(), kwargs=None, gate=True):
    kwargs = {} if kwargs is None else kwargs
    if not native:
        return environment.call(context, callback, *args, **kwargs)
    with native_sandbox_only() if gate else ExitStack():
        return docxtpl_native._extension().python_sandbox_call(environment, context, callback, args, kwargs)


def safe(native, environment, callback, gate=True):
    if not native:
        return environment.is_safe_callable(callback)
    with native_sandbox_only() if gate else ExitStack():
        return docxtpl_native._extension().python_is_safe_callable(environment, callback)


class NativeSandboxCallTests(unittest.TestCase):
    def context(self, environment):
        return Context(environment, {'answer': 42}, 'case', {}, globals={})

    def test_flag_order_short_circuit_and_truth_protocol(self):
        for first in (False, True):
            for second in (False, True):
                def run(native):
                    trace = []
                    class Flag:
                        def __init__(self, name, value): self.name, self.value = name, value
                        def __bool__(self): trace.append(('bool', self.name)); return self.value
                    class Callback:
                        @property
                        def unsafe_callable(self): trace.append('unsafe'); return Flag('unsafe', first)
                        @property
                        def alters_data(self): trace.append('alters'); return Flag('alters', second)
                    result = safe(native, sandbox.SandboxedEnvironment(), Callback())
                    return result, type(result).__name__, trace
                with self.subTest(first=first, second=second):
                    self.assertEqual(run(True), run(False))

    def test_attribute_and_truth_errors_keep_identity(self):
        for flag in ('unsafe_callable', 'alters_data'):
            for phase in ('attribute', 'truth'):
                for error_type in (ValueError, StopIteration, KeyboardInterrupt):
                    failure = error_type('flag failed')
                    class Flag:
                        def __bool__(self): raise failure
                    class Callback:
                        def __getattribute__(self, name):
                            if name == flag:
                                if phase == 'attribute': raise failure
                                return Flag()
                            return super().__getattribute__(name)
                    for native in (False, True):
                        with self.subTest(flag=flag, phase=phase, error=error_type, native=native):
                            with self.assertRaises(error_type) as caught:
                                safe(native, sandbox.SandboxedEnvironment(), Callback())
                            self.assertIs(caught.exception, failure)

    def test_truthy_first_flag_is_retested_before_negation(self):
        for fails in (False, True):
            def run(native):
                trace = []
                failure = ValueError('second truth failed')
                class Flag:
                    def __bool__(self):
                        trace.append('truth')
                        if len(trace) == 1:
                            return True
                        if fails:
                            raise failure
                        return False
                class Callback:
                    unsafe_callable = Flag()
                    @property
                    def alters_data(self):
                        raise AssertionError('second flag accessed')
                try:
                    result = safe(native, sandbox.SandboxedEnvironment(), Callback())
                    return result, trace
                except ValueError as error:
                    return error is failure, trace
            with self.subTest(fails=fails):
                self.assertEqual(run(True), run(False))

    def test_missing_attributes_and_attribute_error_use_defaults(self):
        class Callback:
            @property
            def unsafe_callable(self): raise AttributeError('missing')
        for value in (object(), Callback(), lambda: None):
            self.assertIs(safe(True, sandbox.SandboxedEnvironment(), value), True)

    def test_getattr_global_is_resolved_again_after_first_callback(self):
        def run(native):
            trace = []
            marker = object()
            def first(obj, name, default):
                trace.append(('first', obj is marker, name, default))
                sandbox.getattr = second
                return False
            def second(obj, name, default):
                trace.append(('second', obj is marker, name, default))
                return True
            with patch.object(sandbox, 'getattr', first, create=True):
                result = safe(native, sandbox.SandboxedEnvironment(), marker)
            return result, trace
        self.assertEqual(run(True), run(False))

    def test_allowed_calls_preserve_context_decorators_arguments_and_return_identity(self):
        for decorator in (lambda fn: fn, jinja2.pass_context, jinja2.pass_eval_context, jinja2.pass_environment):
            environment = sandbox.SandboxedEnvironment()
            context = self.context(environment)
            marker = object()
            trace = []
            @decorator
            def callback(*args, **kwargs):
                trace.append((args, kwargs))
                return marker
            for native in (False, True):
                self.assertIs(call(native, environment, context, callback, (marker,), {'value': marker}), marker)
            self.assertEqual(trace[0], trace[1])

    def test_denied_call_never_resolves_context_call(self):
        trace = []
        class Callback:
            unsafe_callable = True
            def __repr__(self): trace.append('repr'); return 'blocked'
            def __call__(self): raise AssertionError('unsafe callback executed')
        class Unreachable:
            def __getattribute__(self, name): raise AssertionError('context accessed')
        for native in (False, True):
            with self.assertRaisesRegex(sandbox.SecurityError, '^blocked is not safely callable$'):
                call(native, sandbox.SandboxedEnvironment(), Unreachable(), Callback())
        self.assertEqual(trace, ['repr', 'repr'])

    def test_denial_resolves_exception_constructor_before_repr_and_keeps_unicode(self):
        def run(native):
            trace = []
            class Text(str):
                def __add__(self, other): raise AssertionError('subclass addition called')
            class Callback:
                unsafe_callable = True
                def __repr__(self):
                    trace.append('repr')
                    sandbox.SecurityError = lambda message: AssertionError('late constructor')
                    return Text('blocked\ud800')
            failure = ValueError('denied')
            def security(message): trace.append(message); return failure
            with patch.object(sandbox, 'SecurityError', security):
                try:
                    call(native, sandbox.SandboxedEnvironment(), object(), Callback())
                except ValueError as error:
                    return error is failure, trace
        self.assertEqual(run(True), run(False))

    def test_policy_overrides_and_bound_receivers(self):
        marker = object()
        environment = sandbox.SandboxedEnvironment()
        environment.is_safe_callable = lambda obj: marker
        self.assertIs(safe(True, environment, object()), marker)
        donor = sandbox.SandboxedEnvironment()
        trace = []
        donor.is_safe_callable = lambda obj: trace.append(obj is marker) or False
        environment.call = donor.call
        for native in (False, True):
            with self.assertRaises(sandbox.SecurityError):
                call(native, environment, object(), marker)
        self.assertEqual(trace, [True, True])
        environment.call = lambda context, callback, *args, **kwargs: marker
        self.assertIs(call(True, environment, object(), object()), marker)

    def test_context_call_lookup_happens_after_policy_and_preserves_bound_receiver(self):
        for native in (False, True):
            environment = sandbox.SandboxedEnvironment()
            context = self.context(environment)
            donor = self.context(jinja2.Environment())
            @jinja2.pass_context
            def callback(value): return value
            def policy(obj):
                context.call = donor.call
                return True
            environment.is_safe_callable = policy
            self.assertIs(call(native, environment, context, callback), donor)

    def test_policy_truth_and_repr_failures_keep_original_exceptions(self):
        for phase in ('truth', 'repr'):
            for error_type in (ValueError, StopIteration, KeyboardInterrupt):
                failure = error_type('original')
                class Flag:
                    def __bool__(self): raise failure
                class Callback:
                    unsafe_callable = True
                    def __repr__(self): raise failure
                environment = sandbox.SandboxedEnvironment()
                if phase == 'truth': environment.is_safe_callable = lambda obj: Flag()
                for native in (False, True):
                    with self.subTest(phase=phase, error=error_type, native=native):
                        with self.assertRaises(error_type) as caught:
                            call(native, environment, object(), Callback())
                        self.assertIs(caught.exception, failure)

    def test_context_call_override_and_stop_iteration_handling(self):
        for native in (False, True):
            environment = sandbox.SandboxedEnvironment()
            context = self.context(environment)
            def stopped(): raise StopIteration('callback stopped')
            result = call(native, environment, context, stopped)
            self.assertIsInstance(result, jinja2.Undefined)
            self.assertEqual(result._undefined_hint, 'value was undefined because a callable raised a StopIteration exception')
            marker = object()
            context.call = lambda callback, *args, **kwargs: marker
            self.assertIs(call(native, environment, context, stopped), marker)

    def test_changed_standard_code_is_a_user_callback(self):
        for name in ('call', 'is_safe_callable'):
            function = getattr(sandbox.SandboxedEnvironment, name)
            original = function.__code__
            def replacement(self, *args, **kwargs): return 'replacement'
            try:
                function.__code__ = replacement.__code__
                environment = sandbox.SandboxedEnvironment()
                if name == 'call':
                    result = call(True, environment, object(), object(), gate=False)
                else:
                    result = safe(True, environment, object(), gate=False)
                self.assertEqual(result, 'replacement')
            finally:
                function.__code__ = original

    def test_override_before_binding_import_is_preserved(self):
        code = textwrap.dedent('''
            import jinja2.sandbox as sandbox
            def call(self, context, callback, *args, **kwargs): return 'override'
            sandbox.SandboxedEnvironment.call = call
            import docxtpl_native
            from docxtpl_native.bridge import BridgeConfig
            result = docxtpl_native._extension().python_sandbox_call(
                sandbox.SandboxedEnvironment(), object(), object(), (), {})
            assert result == 'override'
        ''')
        subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, text=True)

    def test_removed_sandbox_module_keeps_original_function_globals(self):
        environment = sandbox.SandboxedEnvironment()
        context = self.context(environment)
        module = sys.modules.pop('jinja2.sandbox')
        try:
            self.assertEqual(call(True, environment, context, lambda: 'done'), 'done')
            self.assertNotIn('jinja2.sandbox', sys.modules)
        finally:
            sys.modules['jinja2.sandbox'] = module

    def test_complete_document_uses_native_sandbox_dispatch_sync_and_async(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                with native_sandbox_only(), forbid('docxtpl_native.bridge.BridgeConfig.invoke', 'jinja2.Template.render',
                                                   message='Python dispatch or renderer executed'):
                    blob = sandbox_document(docxtpl_native, asynchronous)
                self.assertEqual(observed(blob).observation['text'], '42')

if __name__ == '__main__':
    unittest.main()
