"""Native callback dispatch must retain the caller's object protocols."""
import sys
import unittest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch
import docxtpl_native
import jinja2
from docxtpl_native.bridge import BridgeConfig
from native_audit import forbid
from paritylab.protocol_base import generate, observed


@contextmanager
def native_dispatch_only():
    previous = sys.getprofile()
    code = jinja2.runtime.Context.call.__code__
    def profile(frame, event, argument):
        if event == 'call' and frame.f_code is code:
            raise AssertionError('Python Context.call algorithm executed')
    with forbid('docxtpl_native.bridge.BridgeConfig.invoke', 'jinja2.Template.render',
                message='Python dispatch or template executed'):
        sys.setprofile(profile)
        try:
            yield
        finally:
            sys.setprofile(previous)


def render(native, environment, source, values, *, gate=True):
    if not native:
        return environment.from_string(source).render(values)
    with native_dispatch_only() if gate else ExitStack():
        return docxtpl_native._extension().PythonRenderSession(
            BridgeConfig(environment, False), values).render('case', source)


def dispatch_document(engine):
    """A complete document with a pass_environment callback."""
    @jinja2.pass_environment
    def callback(environment, value):
        return str(value+1)
    return generate(engine,'{{ callback(value) }}',{'callback':callback,'value':41},jinja2.Environment())


class NativeCallAlgorithmTests(unittest.TestCase):
    def test_decorators_arguments_and_callable_objects(self):
        def run(native, kind, object_call):
            trace = []
            environment = jinja2.Environment()
            marker = object()
            def callback(*args, **kwargs):
                if kind == 'context':
                    context, value = args
                    trace.append([isinstance(context, jinja2.runtime.Context),
                                  context.environment is environment, context['local'], value is marker])
                elif kind == 'eval_context':
                    context, value = args
                    trace.append([isinstance(context, jinja2.nodes.EvalContext),
                                  context.environment is environment, context.autoescape, value is marker])
                elif kind == 'environment':
                    context, value = args
                    trace.append([context is environment, value is marker])
                else:
                    trace.append([args[0] is marker])
                trace.append(kwargs)
                return 'done'
            decorator = {'plain':lambda value:value,'context':jinja2.pass_context,
                         'eval_context':jinja2.pass_eval_context,'environment':jinja2.pass_environment}[kind]
            if object_call:
                class Callable:
                    __call__ = staticmethod(decorator(callback))
                target = Callable()
            else:
                target = decorator(callback)
            output = render(native, environment, "{% set local='L' %}{{ callback(value, answer=42) }}",
                            {'callback':target,'value':marker})
            return output, trace
        for kind in ('plain','context','eval_context','environment'):
            for object_call in (False, True):
                with self.subTest(kind=kind, object_call=object_call):
                    self.assertEqual(run(True, kind, object_call), run(False, kind, object_call))

    def test_call_and_decorator_attribute_order(self):
        def run(native):
            trace = []
            class Callable:
                def __getattribute__(self, name):
                    if name in ('__call__','jinja_pass_arg'):
                        trace.append(name)
                    return super().__getattribute__(name)
                @jinja2.pass_context
                def __call__(self, context, value):
                    trace.append(['call',context['local'],value])
                    return 'done'
            output = render(native, jinja2.Environment(), "{% set local='L' %}{{ callback(42) }}",
                            {'callback':Callable()})
            return output,trace
        self.assertEqual(run(True), run(False))

    def test_stop_iteration_and_original_exceptions(self):
        def run(native, exception_type):
            trace=[];failure=exception_type('callback failure')
            class Undefined(jinja2.Undefined):
                def __init__(self,*args,**kwargs):
                    trace.append(['undefined',args,kwargs])
                    super().__init__(*args,**kwargs)
            def callback():
                trace.append('call')
                raise failure
            try:
                output=render(native,jinja2.Environment(undefined=Undefined),'{{ callback() }}',{'callback':callback})
                return ['ok',output,trace]
            except Exception as error:
                return ['error',type(error).__name__,str(error),error is failure,trace]
        for exception_type in (StopIteration,StopAsyncIteration,ValueError):
            with self.subTest(exception_type=exception_type):
                self.assertEqual(run(True, exception_type),run(False, exception_type))

    def test_filter_and_test_callback_dispatch(self):
        def run(native, kind):
            trace=[];environment=jinja2.Environment()
            decorator={'plain':lambda value:value,'context':jinja2.pass_context,
                       'eval_context':jinja2.pass_eval_context,'environment':jinja2.pass_environment}[kind]
            @decorator
            def filter(*args):
                trace.append(['filter',args[-1]])
                return args[-1].upper()
            @decorator
            def test(*args):
                trace.append(['test',args[-1]])
                return True
            environment.filters['custom']=filter;environment.tests['custom']=test
            output=render(native,environment,'{{ value|custom }}{% if value is custom %}Y{% endif %}',{'value':'a'})
            return output,trace
        for kind in ('plain','context','eval_context','environment'):
            with self.subTest(kind=kind):
                self.assertEqual(run(True,kind),run(False,kind))

    def test_context_call_override_remains_a_callback(self):
        def run(native):
            trace = []
            original = jinja2.runtime.Context.call
            def overridden(context, callback, *args, **kwargs):
                trace.append(['override', list(args), kwargs])
                return original(context, callback, *args, **kwargs)
            def callback(value):
                return str(value)
            with patch.object(jinja2.runtime.Context, 'call', overridden):
                output = render(native, jinja2.Environment(), '{{ callback(value) }}',
                                {'callback': callback, 'value': 42}, gate=False)
            return output, trace
        self.assertEqual(run(True), run(False))

    def test_context_call_override_before_binding_import(self):
        import json
        import subprocess
        import textwrap
        code = textwrap.dedent('''
            import json, sys, jinja2, docxtpl_native
            assert 'docxtpl_native.bridge' not in sys.modules
            original = jinja2.runtime.Context.call
            trace = []
            def overridden(context, callback, *args, **kwargs):
                trace.append('override')
                return original(context, callback, *args, **kwargs)
            jinja2.runtime.Context.call = overridden
            from docxtpl_native.bridge import BridgeConfig
            environment = jinja2.Environment()
            values = {'callback': lambda value: str(value), 'value': 42}
            session = docxtpl_native._extension().PythonRenderSession(BridgeConfig(environment, False), values)
            output = session.render('case', '{{ callback(value) }}')
            print(json.dumps([output, trace]))
        ''')
        result = subprocess.run([sys.executable, '-c', code], check=True,
                                capture_output=True, text=True)
        self.assertEqual(json.loads(result.stdout), ['42', ['override']])

    def test_context_call_code_mutation_remains_a_callback(self):
        original = jinja2.runtime.Context.call.__code__
        message = 'value was undefined because a callable raised a StopIteration exception'
        self.assertIn(message, original.co_consts)
        replacement = original.replace(co_consts=tuple(
            'custom stop message' if value == message else value
            for value in original.co_consts))
        def run(native):
            environment = jinja2.Environment()
            environment.filters['hint'] = lambda value: value._undefined_hint
            def callback():
                raise StopIteration('stopped')
            return render(native, environment, '{{ callback()|hint }}',
                          {'callback': callback}, gate=False)
        try:
            jinja2.runtime.Context.call.__code__ = replacement
            self.assertEqual(run(False), 'custom stop message')
            self.assertEqual(run(True), 'custom stop message')
        finally:
            jinja2.runtime.Context.call.__code__ = original

    def test_complete_document_without_python_dispatch(self):
        # Document equivalence with docxtpl: test_audited_documents_reference.
        with native_dispatch_only():
            native=dispatch_document(docxtpl_native)
        self.assertEqual(observed(native).observation['text'],'42')


if __name__=='__main__':
    unittest.main()
